bl_info = {
    "name": "Clockify Time Tracker",
    "author": "Kyokaz, Claude",
    "version": (1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Clockify Tracker",
    "description": "Integrates Clockify time tracking into Blender with project status and billing",
    "category": "3D View",
}

import bpy
import requests
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from bpy.props import StringProperty, EnumProperty, BoolProperty, FloatProperty
from queue import Queue, Empty

# --- THREAD-SAFE GLOBAL VARIABLES ---
api_queue = Queue()
_data_lock = threading.RLock()  # Reentrant lock for nested access
_timer_lock = threading.RLock()
_operation_lock = threading.Lock()  # Prevents multiple operations
_cached_projects_full = []  # Full project data cache

# Protected by _data_lock
_cached_projects = []
_cached_clients = []
_cached_client_id = None

# Protected by _timer_lock  
_timer_start_time = None
_last_session_duration = 0

# Protected by _operation_lock
_operation_in_progress = {"start": False, "stop": False, "status": False}

# Flag to prevent double prompts
_reset_prompt_shown = False

# --- PREFERENCES ---
class ClockifyPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    # API Configuration
    api_key: StringProperty(
        name="API Key",
        description="Your Clockify API Key",
        default=""
    )
    
    workspace_id: StringProperty(
        name="Workspace ID",
        description="Your Clockify Workspace ID",
        default=""
    )
    
    user_id: StringProperty(
        name="User ID", 
        description="Your Clockify User ID",
        default=""
    )
    
    hourly_rate: FloatProperty(
        name="Hourly Rate",
        description="Your hourly rate for billing calculations",
        default=25.0,
        min=0.0
    )
    
    # Display Options
    show_billable: BoolProperty(
        name="Show Billable Amount",
        description="Display billable amount in timer and summaries",
        default=True
    )
    
    show_elapsed_time: BoolProperty(
        name="Show Elapsed Time",
        description="Display elapsed time in active timer",
        default=True
    )
    
    show_project_name: BoolProperty(
        name="Show Project Name",
        description="Display project name in timer display",
        default=True
    )
    
    show_task_name: BoolProperty(
        name="Show Task Description",
        description="Display task description in active timer info",
        default=True
    )
    
    show_client_name: BoolProperty(
        name="Show Client Name",
        description="Display client name in active timer info",
        default=True
    )
    
    show_topbar_timer: BoolProperty(
        name="Show Timer in Top Bar",
        description="Display timer in the top right corner of Blender",
        default=True
    )
    
    show_last_session: BoolProperty(
        name="Show Last Session Summary",
        description="Display last session summary after stopping timer",
        default=True
    )

    def draw(self, context):
        layout = self.layout
        
        # API Configuration Section
        box = layout.box()
        box.label(text="API Configuration:", icon='PREFERENCES')
        box.prop(self, "api_key")
        box.prop(self, "workspace_id")
        
        # User ID with check button
        row = box.row(align=True)
        row.prop(self, "user_id")
        row.operator("clockify.check_credentials", text="Grab User ID", icon='CHECKMARK')
        
        box.prop(self, "hourly_rate")
        
        # Display Options Section
        box = layout.box()
        box.label(text="Display Options:", icon='RESTRICT_VIEW_OFF')
        
        col = box.column()
        col.prop(self, "show_topbar_timer")
        col.prop(self, "show_last_session")
        col.prop(self, "show_billable")
        col.prop(self, "show_elapsed_time")
        col.prop(self, "show_project_name")
        col.prop(self, "show_task_name")
        col.prop(self, "show_client_name")

def get_preferences():
    """Get addon preferences"""
    return bpy.context.preferences.addons[__name__].preferences

def get_api_headers():
    """Get API headers with current API key"""
    prefs = get_preferences()
    return {
        "X-Api-Key": prefs.api_key,
        "Content-Type": "application/json"
    }

# --- UTILITY FUNCTIONS ---
def calculate_billing_info(duration_seconds):
    """Calculate billing information for a time duration"""
    prefs = get_preferences()
    hours = duration_seconds / 3600.0
    billable_amount = hours * prefs.hourly_rate
    return {
        'hours': hours,
        'billable_amount': billable_amount,
        'rate': prefs.hourly_rate
    }

def format_duration_detailed(seconds):
    """Format duration with detailed breakdown"""
    if seconds < 0:
        seconds = 0
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
    
    return " ".join(parts)

def parse_iso_duration(duration_str):
    """Parse ISO 8601 duration (PT1H30M45S) to seconds"""
    if not duration_str or duration_str == "PT0S":
        return 0
    
    # Remove PT prefix
    duration_str = duration_str.replace('PT', '')
    
    hours = 0
    minutes = 0
    seconds = 0
    
    # Parse hours
    if 'H' in duration_str:
        hours_part = duration_str.split('H')[0]
        hours = float(hours_part)
        duration_str = duration_str.split('H')[1]
    
    # Parse minutes
    if 'M' in duration_str:
        minutes_part = duration_str.split('M')[0]
        minutes = float(minutes_part)
        duration_str = duration_str.split('M')[1]
    
    # Parse seconds
    if 'S' in duration_str:
        seconds_part = duration_str.split('S')[0]
        seconds = float(seconds_part)
    
    return int(hours * 3600 + minutes * 60 + seconds)

def get_filtered_projects_for_client(client_id):
    """Get projects filtered by client ID"""
    projects = []
    cached_projects_full = get_cached_projects_full()
    
    for p in cached_projects_full:
        project_id, project_name, project_desc, proj_client_id = p
        
        if client_id == "NONE":
            # Show projects with no client assigned
            if not proj_client_id:
                projects.append((project_id, project_name, project_desc))
        elif client_id == "CREATE_NEW":
            # Don't show any projects when creating new client
            continue
        elif client_id and client_id != "NONE":
            # Show projects that belong to the selected client
            if proj_client_id == client_id:
                projects.append((project_id, project_name, project_desc))
    
    return projects

# --- FILE PERSISTENCE ---
def save_task_description_to_file():
    """Save task description to blend file custom properties"""
    try:
        scene = bpy.context.scene
        if hasattr(scene, 'clockify_task_description'):
            # Store in scene custom properties for persistence
            scene["clockify_saved_task"] = scene.clockify_task_description
            print(f"Saved task description: {scene.clockify_task_description}")
    except Exception as e:
        print(f"Error saving task description: {e}")

def load_task_description_from_file():
    """Load task description from blend file custom properties"""
    try:
        scene = bpy.context.scene
        if "clockify_saved_task" in scene:
            saved_task = scene["clockify_saved_task"]
            if hasattr(scene, 'clockify_task_description'):
                scene.clockify_task_description = saved_task
                print(f"Loaded task description: {saved_task}")
                return saved_task
    except Exception as e:
        print(f"Error loading task description: {e}")
    return None

# --- THREAD-SAFE ACCESSORS ---
def get_cached_projects():
    """Thread-safe getter for cached projects"""
    with _data_lock:
        return _cached_projects.copy()

def set_cached_projects(projects):
    """Thread-safe setter for cached projects"""
    with _data_lock:
        global _cached_projects
        _cached_projects = projects

def get_cached_projects_full():
    """Thread-safe getter for cached projects with full data"""
    with _data_lock:
        return _cached_projects_full.copy()

def set_cached_projects_full(projects):
    """Thread-safe setter for cached projects with full data"""
    with _data_lock:
        global _cached_projects_full
        _cached_projects_full = projects

def get_cached_clients():
    """Thread-safe getter for cached clients"""
    with _data_lock:
        return _cached_clients.copy()

def set_cached_clients(clients):
    """Thread-safe setter for cached clients"""
    with _data_lock:
        global _cached_clients
        _cached_clients = clients

def get_cached_client_id():
    """Thread-safe getter for cached client ID"""
    with _data_lock:
        return _cached_client_id

def set_cached_client_id(client_id):
    """Thread-safe setter for cached client ID"""
    with _data_lock:
        global _cached_client_id
        _cached_client_id = client_id

def get_timer_start_time():
    """Thread-safe getter for timer start time"""
    with _timer_lock:
        return _timer_start_time

def set_timer_start_time(start_time):
    """Thread-safe setter for timer start time"""
    with _timer_lock:
        global _timer_start_time
        _timer_start_time = start_time

def get_last_session_duration():
    """Thread-safe getter for last session duration"""
    with _timer_lock:
        return _last_session_duration

def set_last_session_duration(duration):
    """Thread-safe setter for last session duration"""
    with _timer_lock:
        global _last_session_duration
        _last_session_duration = duration

def is_operation_in_progress(operation_type):
    """Check if operation is in progress"""
    with _operation_lock:
        return _operation_in_progress.get(operation_type, False)

def set_operation_in_progress(operation_type, value):
    """Set operation progress state"""
    with _operation_lock:
        _operation_in_progress[operation_type] = value

# --- CONTEXT SAFETY DECORATORS ---
def safe_context_access(func):
    """Decorator to safely access Blender context"""
    def wrapper(*args, **kwargs):
        try:
            if not hasattr(bpy, 'context') or bpy.context is None:
                return None
            return func(*args, **kwargs)
        except Exception as e:
            print(f"Context access error in {func.__name__}: {e}")
            return None
    return wrapper

# --- DYNAMIC ENUM ITEMS ---
def get_client_items(self, context):
    """Dynamic client items for EnumProperty"""
    items = []
    
    # Add "None" option for projects without clients
    items.append(("NONE", "None (No Client)", "Show projects without assigned clients"))
    
    # Add cached Clockify clients
    cached_clients = get_cached_clients()
    if cached_clients:
        for c in cached_clients:
            items.append((c[0], c[1], c[2]))
    
    # Add create new option
    items.append(("CREATE_NEW", "➕ Create New Client...", "Create a new client"))
    
    return items

def get_project_items(self, context):
    """Dynamic project items for EnumProperty - filtered by selected client"""
    items = []
    scene = context.scene
    
    # Get selected client
    selected_client = getattr(scene, 'clockify_client', None)
    
    # Add cached Clockify projects filtered by client
    cached_projects_full = get_cached_projects_full()
    if cached_projects_full:
        for p in cached_projects_full:
            project_id, project_name, project_desc, client_id = p
            
            # Filter based on selected client
            if selected_client == "NONE":
                # Show projects with no client assigned
                if not client_id:
                    items.append((project_id, project_name, project_desc))
            elif selected_client == "CREATE_NEW":
                # Don't show any projects when creating new client
                continue
            elif selected_client and selected_client != "NONE":
                # Show projects that belong to the selected client
                if client_id == selected_client:
                    items.append((project_id, project_name, project_desc))
    
    # Add create new option
    items.append(("CREATE_NEW", "➕ Create New Project...", "Create a new project"))
    
    return items if items else [("CREATE_NEW", "➕ Create New Project...", "Create a new project")]

# --- TIMER DISPLAY FUNCTIONS ---
def format_timer_display(seconds):
    """Format seconds into HH:MM:SS format"""
    if seconds < 0:
        seconds = 0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def get_current_timer_duration():
    """Get current timer duration in seconds - thread-safe"""
    start_time = get_timer_start_time()
    if start_time is None:
        return 0
    return max(0, time.time() - start_time)

@safe_context_access
def draw_clockify_timer(self, context):
    """Draw the Clockify timer in the top bar"""
    layout = self.layout
    region = context.region
    scene = context.scene
    prefs = get_preferences()
    
    # Only show on the right side of the top bar and if enabled in preferences
    if region.alignment == 'RIGHT' and prefs.show_topbar_timer:
        # Check if there's an active timer
        if hasattr(scene, 'clockify_active_timer_id') and scene.clockify_active_timer_id:
            # Calculate elapsed time
            current_duration = get_current_timer_duration()
            time_display = format_timer_display(current_duration)
            
            # Create a row for the timer display
            row = layout.row(align=True)
            row.alert = True  # This makes the text red/highlighted
            row.label(text=f"⏱ {time_display}", icon='TIME')
            
            # Show billing info if enabled
            if prefs.show_billable:
                billing = calculate_billing_info(current_duration)
                if billing['hours'] > 0:
                    row.label(text=f"${billing['billable_amount']:.2f}")
            
            # Show project name if enabled
            if prefs.show_project_name and hasattr(scene, 'clockify_active_project_name') and scene.clockify_active_project_name:
                project_name = scene.clockify_active_project_name
                if len(project_name) > 15:
                    project_name = project_name[:12] + "..."
                row.label(text=f"({project_name})")

@safe_context_access
def update_timer_display():
    """Update the timer display - called by timer"""
    try:
        # Check if we have a valid context
        if not bpy.context:
            return 1.0
            
        scene = bpy.context.scene
        if not scene:
            return 1.0
            
        # Continue updating only if there's an active timer
        if hasattr(scene, 'clockify_active_timer_id') and scene.clockify_active_timer_id:
            # Force redraw of all relevant areas
            try:
                if bpy.context.screen:
                    for area in bpy.context.screen.areas:
                        if area.type in ['TOPBAR', 'VIEW_3D']:
                            area.tag_redraw()
                            
                # Also force window manager update
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type in ['TOPBAR', 'VIEW_3D']:
                            area.tag_redraw()
                            
            except Exception as e:
                print(f"Error in redraw: {e}")
            
            return 1.0  # Update every second
        else:
            return None  # Stop the timer
    except Exception as e:
        print(f"Error updating timer display: {e}")
        return None

# --- TIMER RESET FUNCTIONS ---
def reset_blender_timer():
    """Reset the Blender timer to 0 state"""
    global _reset_prompt_shown
    _reset_prompt_shown = False
    
    set_timer_start_time(None)
    set_last_session_duration(0)
    
    scene = bpy.context.scene
    scene.clockify_active_timer_id = ""
    scene.clockify_active_timer_desc = ""
    scene.clockify_active_project = ""
    scene.clockify_active_project_name = ""
    scene.clockify_active_client_name = ""
    scene.clockify_status = "Timer reset - ready to start a new session"
    
    # Force UI redraw
    if bpy.context.screen:
        for area in bpy.context.screen.areas:
            if area.type in ['TOPBAR', 'VIEW_3D']:
                area.tag_redraw()

# --- API UTILS ---
def fetch_clients_async(callback=None):
    """Fetch all clients in a separate thread"""
    def _fetch():
        try:
            prefs = get_preferences()
            headers = get_api_headers()
            
            url = f"https://api.clockify.me/api/v1/workspaces/{prefs.workspace_id}/clients"
            res = requests.get(url, headers=headers, timeout=10)
            
            if res.status_code == 200:
                clients_data = res.json()
                clients = [(c['id'], c['name'], c['name']) for c in clients_data]
                api_queue.put(('clients_fetched', clients, callback))
            else:
                api_queue.put(('error', f"Failed to fetch clients: {res.status_code}", callback))
        except Exception as e:
            api_queue.put(('error', f"Network error: {str(e)}", callback))
    
    thread = threading.Thread(target=_fetch, daemon=True)
    thread.start()

def create_client_async(name, callback=None):
    """Create client in a separate thread"""
    def _create():
        try:
            prefs = get_preferences()
            headers = get_api_headers()
            
            url = f"https://api.clockify.me/api/v1/workspaces/{prefs.workspace_id}/clients"
            payload = {
                "name": name,
                "address": "",
                "note": "Auto-created by Blender Clockify plugin"
            }
            res = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
            
            if res.status_code == 201:
                client_data = res.json()
                api_queue.put(('client_created_new', client_data, callback))
            else:
                api_queue.put(('error', f"Failed to create client: {res.status_code} - {res.text}", callback))
        except Exception as e:
            api_queue.put(('error', f"Network error: {str(e)}", callback))
    
    thread = threading.Thread(target=_create, daemon=True)
    thread.start()

def fetch_projects_async(callback=None):
    """Fetch projects in a separate thread with client information"""
    def _fetch():
        try:
            prefs = get_preferences()
            headers = get_api_headers()
            
            url = f"https://api.clockify.me/api/v1/workspaces/{prefs.workspace_id}/projects"
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                projects_data = res.json()
                # Store full project data including client info
                projects_full = []
                projects_simple = []
                
                for p in projects_data:
                    project_id = p['id']
                    project_name = p['name']
                    client_id = p.get('clientId', None)  # May be None for projects without clients
                    
                    # Store full data for filtering
                    projects_full.append((project_id, project_name, project_name, client_id))
                    # Store simple data for backward compatibility
                    projects_simple.append((project_id, project_name, project_name))
                
                api_queue.put(('projects_fetched_full', {'full': projects_full, 'simple': projects_simple}, callback))
            else:
                api_queue.put(('error', f"Failed to fetch projects: {res.status_code}", callback))
        except Exception as e:
            api_queue.put(('error', f"Network error: {str(e)}", callback))
    
    thread = threading.Thread(target=_fetch, daemon=True)
    thread.start()

def get_project_summary_async(project_id, callback=None):
    """Get project time summary for the current month"""
    def _fetch():
        try:
            prefs = get_preferences()
            headers = get_api_headers()
            
            # Calculate current month start and end
            today = datetime.now(timezone.utc)
            month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            
            # Calculate next month start (end of current month)
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1)
            
            # Get time entries for this project this month
            url = f"https://api.clockify.me/api/v1/workspaces/{prefs.workspace_id}/user/{prefs.user_id}/time-entries"
            params = {
                "start": month_start.isoformat(),
                "end": month_end.isoformat(),
                "project": project_id
            }
            
            res = requests.get(url, headers=headers, params=params, timeout=10)
            
            if res.status_code == 200:
                time_entries = res.json()
                
                # Calculate total duration
                total_seconds = 0
                for entry in time_entries:
                    if entry.get('timeInterval', {}).get('duration'):
                        duration_str = entry['timeInterval']['duration']
                        total_seconds += parse_iso_duration(duration_str)
                
                summary_data = {
                    'total_seconds': total_seconds,
                    'entries_count': len(time_entries),
                    'month_start': month_start,
                    'month_end': month_end
                }
                
                api_queue.put(('project_summary', summary_data, callback))
            else:
                api_queue.put(('error', f"Failed to fetch project summary: {res.status_code}", callback))
        except Exception as e:
            api_queue.put(('error', f"Network error: {str(e)}", callback))
    
    thread = threading.Thread(target=_fetch, daemon=True)
    thread.start()

def start_timer_async(description, project_id, callback=None):
    """Start timer in a separate thread"""
    def _start():
        try:
            prefs = get_preferences()
            headers = get_api_headers()
            
            url = f"https://api.clockify.me/api/v1/workspaces/{prefs.workspace_id}/time-entries"
            payload = {
                "start": None,  # Auto-start time
                "description": description,
                "projectId": project_id
            }
            res = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
            if res.status_code == 201:
                timer_data = res.json()
                api_queue.put(('timer_started', timer_data, callback))
            else:
                api_queue.put(('error', f"Failed to start timer: {res.status_code} - {res.text}", callback))
        except Exception as e:
            api_queue.put(('error', f"Network error: {str(e)}", callback))
    
    thread = threading.Thread(target=_start, daemon=True)
    thread.start()

def stop_timer_async(callback=None):
    """Stop timer in a separate thread"""
    def _stop():
        try:
            prefs = get_preferences()
            headers = get_api_headers()
            
            url = f"https://api.clockify.me/api/v1/workspaces/{prefs.workspace_id}/user/{prefs.user_id}/time-entries?in-progress=true"
            res = requests.get(url, headers=headers, timeout=10)
            
            if res.status_code != 200:
                api_queue.put(('error', f"Failed to get current timer: {res.status_code}", callback))
                return
            
            timer_list = res.json()
            if not timer_list:
                api_queue.put(('no_active_timer', None, callback))
                return
            
            current_timer = timer_list[0]
            timer_id = current_timer['id']
            
            # Calculate duration before stopping
            start_time = get_timer_start_time()
            if start_time:
                duration = time.time() - start_time
                set_last_session_duration(duration)
            
            url = f"https://api.clockify.me/api/v1/workspaces/{prefs.workspace_id}/time-entries/{timer_id}"
            current_time = datetime.now(timezone.utc).isoformat()
            
            payload = {
                "start": current_timer['timeInterval']['start'],
                "end": current_time,
                "billable": current_timer.get('billable', False),
                "description": current_timer.get('description', ''),
                "projectId": current_timer.get('projectId'),
                "taskId": current_timer.get('taskId'),
                "tagIds": current_timer.get('tagIds', [])
            }
            
            res = requests.put(url, headers=headers, data=json.dumps(payload), timeout=10)
            
            if res.status_code == 200:
                # Pass the current timer data so we can extract session info
                api_queue.put(('timer_stopped', current_timer, callback))
            else:
                api_queue.put(('error', f"Failed to stop timer: {res.status_code} - {res.text}", callback))
                
        except Exception as e:
            api_queue.put(('error', f"Network error: {str(e)}", callback))
    
    thread = threading.Thread(target=_stop, daemon=True)
    thread.start()

def create_project_async(name, callback=None):
    """Create project in a separate thread"""
    def _create():
        try:
            prefs = get_preferences()
            headers = get_api_headers()
            
            client_id = get_cached_client_id()
            url = f"https://api.clockify.me/api/v1/workspaces/{prefs.workspace_id}/projects"
            payload = {
                "name": name,
                "clientId": client_id,
                "isPublic": False,
                "color": "#3498db"
            }
            res = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
            
            if res.status_code == 201:
                project_data = res.json()
                api_queue.put(('project_created', project_data, callback))
            else:
                api_queue.put(('error', f"Failed to create project: {res.status_code} - {res.text}", callback))
        except Exception as e:
            api_queue.put(('error', f"Network error: {str(e)}", callback))
    
    thread = threading.Thread(target=_create, daemon=True)
    thread.start()

def get_user_info_async(callback=None):
    """Get user info from API to auto-fill user ID"""
    def _get():
        try:
            prefs = get_preferences()
            headers = get_api_headers()
            
            url = "https://api.clockify.me/api/v1/user"
            res = requests.get(url, headers=headers, timeout=10)
            
            if res.status_code == 200:
                user_data = res.json()
                api_queue.put(('user_info', user_data, callback))
            else:
                api_queue.put(('error', f"Failed to get user info: {res.status_code}", callback))
        except Exception as e:
            api_queue.put(('error', f"Network error: {str(e)}", callback))
    
    thread = threading.Thread(target=_get, daemon=True)
    thread.start()

def get_current_timer_async(callback=None):
    """Get current timer in a separate thread"""
    def _get():
        try:
            prefs = get_preferences()
            headers = get_api_headers()
            
            url = f"https://api.clockify.me/api/v1/workspaces/{prefs.workspace_id}/user/{prefs.user_id}/time-entries?in-progress=true"
            res = requests.get(url, headers=headers, timeout=10)
            
            if res.status_code == 200:
                timer_list = res.json()
                current_timer = timer_list[0] if timer_list else None
                api_queue.put(('current_timer', current_timer, callback))
            else:
                api_queue.put(('error', f"Failed to get current timer: {res.status_code}", callback))
        except Exception as e:
            api_queue.put(('error', f"Network error: {str(e)}", callback))
    
    thread = threading.Thread(target=_get, daemon=True)
    thread.start()

# --- MAIN THREAD CALLBACKS ---
@safe_context_access  
def handle_clients_response(action, data):
    """Handle clients response in main thread"""
    if action == 'clients_fetched':
        set_cached_clients(data)
        scene = bpy.context.scene
        if hasattr(scene, 'clockify_client'):
            current_selection = scene.clockify_client
            cached_clients = get_cached_clients()
            if current_selection in [c[0] for c in cached_clients]:
                scene.clockify_client = current_selection
            else:
                # Set to first client or CREATE_NEW if no clients
                scene.clockify_client = cached_clients[0][0] if cached_clients else "CREATE_NEW"
                
            if bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()

@safe_context_access
def handle_client_created_new(action, data):
    """Handle new client creation response"""
    if action == 'client_created_new':
        scene = bpy.context.scene
        client_id = data['id']
        client_name = data['name']
        
        # Update cached client ID
        set_cached_client_id(client_id)
        
        # Refresh clients list and set selection to the new client
        def refresh_clients_callback(refresh_action, refresh_data):
            if refresh_action == 'clients_fetched':
                scene.clockify_client = client_id
                scene.clockify_show_new_client_field = False
                scene.clockify_new_client_name = ""
                scene.clockify_status = f"✅ Client '{client_name}' created successfully!"
        
        fetch_clients_async(refresh_clients_callback)

@safe_context_access  
def handle_projects_response(action, data):
    """Handle projects response in main thread"""
    if action == 'projects_fetched':
        set_cached_projects(data)
        scene = bpy.context.scene
        if hasattr(scene, 'clockify_project'):
            current_selection = scene.clockify_project
            cached_projects = get_cached_projects()
            if current_selection in [p[0] for p in cached_projects]:
                scene.clockify_project = current_selection
            else:
                scene.clockify_project = cached_projects[0][0] if cached_projects else "CREATE_NEW"
                
            if bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()

@safe_context_access  
def handle_projects_response_full(action, data):
    """Handle projects response in main thread with full data"""
    if action == 'projects_fetched_full':
        projects_full = data['full']
        projects_simple = data['simple']
        
        # Cache both versions
        set_cached_projects_full(projects_full)
        set_cached_projects(projects_simple)  # Keep existing cache updated too
        
        scene = bpy.context.scene
        if hasattr(scene, 'clockify_project'):
            current_selection = scene.clockify_project
            # Check if current selection is still valid for the selected client
            valid_projects = [p[0] for p in get_filtered_projects_for_client(scene.clockify_client)]
            
            if current_selection in valid_projects:
                scene.clockify_project = current_selection
            else:
                # Reset to first available project or CREATE_NEW
                if valid_projects:
                    scene.clockify_project = valid_projects[0]
                else:
                    scene.clockify_project = "CREATE_NEW"
                
            if bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()

@safe_context_access
def handle_timer_started(action, timer_data, project_name=None, client_name=None):
    """Handle timer started response in main thread"""
    if action == 'timer_started':
        set_timer_start_time(time.time())
        
        scene = bpy.context.scene
        scene.clockify_active_timer_id = timer_data['id']
        scene.clockify_active_timer_desc = timer_data['description']
        scene.clockify_active_project = timer_data['projectId']
        scene.clockify_active_project_name = project_name or "Unknown Project"
        scene.clockify_active_client_name = client_name or ""
        scene.clockify_status = "Timer started successfully!"
        scene.clockify_show_new_project_field = False
        scene.clockify_new_project_name = ""
        scene.clockify_show_new_client_field = False
        scene.clockify_new_client_name = ""
        
        # Update project selection to the newly created/selected project
        if timer_data['projectId']:
            scene.clockify_project = timer_data['projectId']
        
        # Ensure timer display updates are running
        if not bpy.app.timers.is_registered(update_timer_display):
            bpy.app.timers.register(update_timer_display, first_interval=1.0)
        
        # Force immediate UI update
        try:
            if bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type in ['TOPBAR', 'VIEW_3D']:
                        area.tag_redraw()
        except Exception as e:
            print(f"Error forcing UI update: {e}")

@safe_context_access
def handle_timer_stopped(action, timer_data):
    """Handle timer stopped response in main thread with billing summary"""
    if action == 'timer_stopped':
        # Get the duration before clearing
        duration = get_last_session_duration()
        billing = calculate_billing_info(duration)
        prefs = get_preferences()
        
        set_timer_start_time(None)
        
        scene = bpy.context.scene
        
        # Get session info from the timer_data (Clockify response) instead of scene
        task_desc = timer_data.get('description', 'No description') if timer_data else 'No description'
        project_id = timer_data.get('projectId', '') if timer_data else ''
        
        # Find project name from cached projects
        project_name = "Unknown Project"
        if project_id:
            cached_projects = get_cached_projects()
            for p in cached_projects:
                if p[0] == project_id:
                    project_name = p[1]
                    break
        
        # Get client name from scene (this should still be available)
        client_name = scene.clockify_active_client_name if hasattr(scene, 'clockify_active_client_name') and scene.clockify_active_client_name else ""
        
        # Show billing summary
        duration_str = format_duration_detailed(duration)
        billing_str = f"${billing['billable_amount']:.2f}"
        hours_str = f"{billing['hours']:.2f}h"
        
        # Create status message based on preferences
        status_parts = [f"✅ Session complete: {duration_str}"]
        if prefs.show_elapsed_time:
            status_parts.append(hours_str)
        if prefs.show_billable:
            status_parts.append(f"{billing_str} @ ${billing['rate']}/hr")
        
        scene.clockify_status = " • ".join(status_parts)
        
        # Create comprehensive session summary if enabled
        if prefs.show_last_session:
            summary_parts = []
            if prefs.show_project_name:
                summary_parts.append(f"Project: {project_name}")
            if prefs.show_task_name:
                summary_parts.append(f"Task: {task_desc}")
            if prefs.show_client_name and client_name:
                summary_parts.append(f"Client: {client_name}")
            if prefs.show_elapsed_time:
                summary_parts.append(f"Duration: {duration_str}")
            if prefs.show_billable:
                summary_parts.append(f"Billable: {billing_str} ({hours_str} @ ${billing['rate']}/hr)")
            
            scene.clockify_last_session_summary = "\n".join(summary_parts)
        else:
            scene.clockify_last_session_summary = ""
        
        # Clear active timer info AFTER capturing the data
        scene.clockify_active_timer_id = ""
        scene.clockify_active_timer_desc = ""
        scene.clockify_active_project = ""
        scene.clockify_active_project_name = ""
        scene.clockify_active_client_name = ""
        
        # Force redraw to show the summary in the panel
        if bpy.context.screen:
            for area in bpy.context.screen.areas:
                if area.type in ['TOPBAR', 'VIEW_3D']:
                    area.tag_redraw()

@safe_context_access
def handle_no_active_timer():
    """Handle the case when no active timer is found in Clockify"""
    global _reset_prompt_shown
    
    # Prevent double prompts
    if not _reset_prompt_shown:
        _reset_prompt_shown = True
        bpy.ops.clockify.reset_timer_prompt('INVOKE_DEFAULT')

@safe_context_access
def handle_current_timer(action, data):
    """Handle current timer response in main thread"""
    if action == 'current_timer':
        scene = bpy.context.scene
        if data:
            desc = data.get('description', 'No description')
            project_id = data.get('projectId', '')
            
            start_time_str = data['timeInterval']['start']
            try:
                start_time_dt = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                set_timer_start_time(start_time_dt.timestamp())
            except Exception as e:
                print(f"Error parsing start time: {e}")
                set_timer_start_time(time.time())
            
            scene.clockify_active_timer_id = data['id']
            scene.clockify_active_timer_desc = desc
            scene.clockify_active_project = project_id
            
            project_name = "Unknown Project"
            cached_projects = get_cached_projects()
            for p in cached_projects:
                if p[0] == project_id:
                    project_name = p[1]
                    break
            scene.clockify_active_project_name = project_name
            
            # Try to find client name from project (this would require additional API call)
            scene.clockify_active_client_name = ""
            
            scene.clockify_status = f"Timer running: {desc}"
            
            if not bpy.app.timers.is_registered(update_timer_display):
                bpy.app.timers.register(update_timer_display, first_interval=1.0)
        else:
            set_timer_start_time(None)
            
            scene.clockify_status = "No timer currently running"
            scene.clockify_active_timer_id = ""
            scene.clockify_active_timer_desc = ""
            scene.clockify_active_project = ""
            scene.clockify_active_project_name = ""
            scene.clockify_active_client_name = ""
        
        # Force UI redraw after updating timer status
        if bpy.context.screen:
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

@safe_context_access
def handle_project_summary(action, data):
    """Handle project summary response in main thread"""
    if action == 'project_summary':
        scene = bpy.context.scene
        prefs = get_preferences()
        
        total_seconds = data['total_seconds']
        entries_count = data['entries_count']
        
        duration_str = format_duration_detailed(total_seconds)
        billing = calculate_billing_info(total_seconds)
        
        scene.clockify_project_summary = f"This Month: {duration_str} ({entries_count} sessions)\nBillable: ${billing['billable_amount']:.2f} ({billing['hours']:.2f}h @ ${prefs.hourly_rate}/hr)"
        scene.clockify_status = f"Project status updated: {duration_str} this month"
        
        # Force UI redraw after updating project summary
        if bpy.context.screen:
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

@safe_context_access
def handle_user_info(action, data):
    """Handle user info response in main thread"""
    if action == 'user_info':
        prefs = get_preferences()
        prefs.user_id = data['id']
        
        # Also save preferences
        context = bpy.context
        context.preferences.use_preferences_save = True
        
        print(f"Auto-filled User ID: {data['id']}")
        
        # Show success message
        def show_message():
            bpy.context.scene.clockify_status = f"✅ Credentials verified! User: {data.get('name', 'Unknown')}"
            return None
        bpy.app.timers.register(show_message, first_interval=0.01)

# --- BACKGROUND TASK PROCESSOR ---
@safe_context_access
def process_api_queue():
    """Process API responses in the main thread"""
    processed_items = 0
    max_items_per_call = 10
    ui_needs_redraw = False
    
    while not api_queue.empty() and processed_items < max_items_per_call:
        try:
            action, data, callback = api_queue.get_nowait()
            
            if action == 'clients_fetched':
                handle_clients_response(action, data)
                ui_needs_redraw = True
            elif action == 'projects_fetched_full':
                handle_projects_response_full(action, data)
                ui_needs_redraw = True
            elif action == 'client_created_new':
                handle_client_created_new(action, data)
                ui_needs_redraw = True
            elif action == 'projects_fetched':
                handle_projects_response(action, data)
                ui_needs_redraw = True
            elif action == 'project_summary':
                handle_project_summary(action, data)
                ui_needs_redraw = True
            elif action == 'current_timer':
                handle_current_timer(action, data)
                ui_needs_redraw = True
            elif action == 'timer_started':
                handle_timer_started(action, data)
                ui_needs_redraw = True
            elif action == 'timer_stopped':
                handle_timer_stopped(action, data)
                ui_needs_redraw = True
            elif action == 'no_active_timer':
                handle_no_active_timer()
                ui_needs_redraw = True
            elif action == 'user_info':
                handle_user_info(action, data)
                ui_needs_redraw = True
            elif action == 'error':
                # Handle error messages
                def show_error():
                    if bpy.context and bpy.context.scene:
                        bpy.context.scene.clockify_status = f"Error: {data}"
                    return None
                bpy.app.timers.register(show_error, first_interval=0.01)
                ui_needs_redraw = True
            
            if callback:
                try:
                    callback(action, data)
                except Exception as e:
                    print(f"Error in callback: {e}")
            
            processed_items += 1
                
        except Empty:
            break
        except Exception as e:
            print(f"Error processing API queue item: {e}")
            processed_items += 1
            continue
    
    # Force comprehensive UI redraw
    try:
        if bpy.context and bpy.context.window_manager:
            for window in bpy.context.window_manager.windows:
                if window.screen:
                    for area in window.screen.areas:
                        if area.type in ['VIEW_3D', 'TOPBAR']:
                            area.tag_redraw()
        
        # Fallback to context screen
        if bpy.context and bpy.context.screen:
            for area in bpy.context.screen.areas:
                if area.type in ['VIEW_3D', 'TOPBAR']:
                    area.tag_redraw()
                    
    except Exception as e:
        print(f"Error redrawing UI: {e}")
            
    return 0.1
    
    # Force UI redraw for all relevant areas if any significant updates occurred
    if ui_needs_redraw:
        try:
            if bpy.context and bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type in ['VIEW_3D', 'TOPBAR']:
                        area.tag_redraw()
        except Exception as e:
            print(f"Error redrawing UI: {e}")
    else:
        # Always redraw top bar to ensure timer display updates
        try:
            if bpy.context and bpy.context.screen:
                for area in bpy.context.screen.areas:
                    if area.type == 'TOPBAR':
                        area.tag_redraw()
        except Exception as e:
            print(f"Error redrawing topbar: {e}")
            
    return 0.1

# --- EVENT HANDLERS ---
@bpy.app.handlers.persistent
def save_pre_handler(dummy):
    """Handler called before saving blend file"""
    save_task_description_to_file()

@bpy.app.handlers.persistent  
def load_post_handler(dummy):
    """Handler called after loading blend file"""
    # Small delay to ensure scene is fully loaded
    def delayed_load():
        load_task_description_from_file()
        return None
    bpy.app.timers.register(delayed_load, first_interval=0.5)

# --- UPDATE FUNCTIONS ---
def client_selection_update(self, context):
    """Called when client selection changes"""
    if self.clockify_client == "CREATE_NEW":
        self.clockify_show_new_client_field = True
    else:
        self.clockify_show_new_client_field = False
        
        # Update the cached client ID when selection changes
        if self.clockify_client != "NONE":
            cached_clients = get_cached_clients()
            for c in cached_clients:
                if c[0] == self.clockify_client:
                    set_cached_client_id(self.clockify_client)
                    break
        else:
            # Set client ID to None for "NONE" selection
            set_cached_client_id(None)
        
        # Filter projects based on selected client
        filtered_projects = get_filtered_projects_for_client(self.clockify_client)
        
        # Update project selection to first available project or CREATE_NEW
        if hasattr(self, 'clockify_project'):
            current_project = self.clockify_project
            valid_project_ids = [p[0] for p in filtered_projects]
            
            if current_project not in valid_project_ids and current_project != "CREATE_NEW":
                # Reset to first available project or CREATE_NEW
                self.clockify_project = valid_project_ids[0] if valid_project_ids else "CREATE_NEW"
        
        # Force UI refresh to update project dropdown
        if hasattr(bpy, 'context') and bpy.context and bpy.context.screen:
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

def project_selection_update(self, context):
    """Called when project selection changes"""
    if self.clockify_project == "CREATE_NEW":
        self.clockify_show_new_project_field = True
    else:
        self.clockify_show_new_project_field = False

# --- OPERATORS ---
class CLOCKIFY_OT_StartTimer(bpy.types.Operator):
    bl_idname = "clockify.start_timer"
    bl_label = "Start Timer"
    
    def execute(self, context):
        if is_operation_in_progress("start"):
            self.report({'WARNING'}, "Timer is already starting, please wait...")
            return {'CANCELLED'}
        
        scene = context.scene
        desc = scene.clockify_task_description
        project = scene.clockify_project
        client = scene.clockify_client
        
        # Handle client creation if needed
        if client == "CREATE_NEW":
            new_client_name = scene.clockify_new_client_name.strip()
            if not new_client_name:
                self.report({'ERROR'}, "Please enter a client name")
                return {'CANCELLED'}
            
            scene.clockify_status = "Creating client..."
            
            def client_created_callback(action, data):
                if action == 'client_created_new':
                    # Client created, now handle project creation/selection
                    # Use a timer to call the method in the main thread context
                    def continue_with_project():
                        self.handle_project_and_start_timer_fixed(desc, project)
                        return None
                    bpy.app.timers.register(continue_with_project, first_interval=0.01)
                elif action == 'error':
                    def main_thread_error():
                        scene.clockify_status = f"Error creating client: {data}"
                        return None
                    bpy.app.timers.register(main_thread_error, first_interval=0.01)
            
            create_client_async(new_client_name, client_created_callback)
        else:
            # Set the selected client ID
            if client != "NONE":
                cached_clients = get_cached_clients()
                for c in cached_clients:
                    if c[0] == client:
                        set_cached_client_id(client)
                        break
            else:
                set_cached_client_id(None)
            
            # Handle project creation/selection
            self.handle_project_and_start_timer_fixed(desc, project)
        
        return {'FINISHED'}
    
    def handle_project_and_start_timer_fixed(self, desc, project):
        """Fixed version that works in timer callback context"""
        scene = bpy.context.scene
        
        # Get client name for timer display
        client_name = ""
        if hasattr(scene, 'clockify_client') and scene.clockify_client not in ["CREATE_NEW", "NONE"]:
            cached_clients = get_cached_clients()
            for c in cached_clients:
                if c[0] == scene.clockify_client:
                    client_name = c[1]
                    break
        
        if project == "CREATE_NEW":
            new_project_name = scene.clockify_new_project_name.strip()
            if not new_project_name:
                scene.clockify_status = "Error: Please enter a project name"
                return
            
            set_operation_in_progress("start", True)
            scene.clockify_status = "Creating project..."
            
            def project_created_callback(action, data):
                if action == 'project_created':
                    project_id = data['id']
                    project_name = data['name']
                    scene.clockify_status = "Starting timer..."
                    
                    def timer_started_callback(action, data):
                        try:
                            if action == 'timer_started':
                                def main_thread_update():
                                    handle_timer_started(action, data, project_name, client_name)
                                    # Refresh projects list to include the new project
                                    def refresh_projects_callback(refresh_action, refresh_data):
                                        if refresh_action == 'projects_fetched_full':
                                            scene.clockify_project = project_id
                                    fetch_projects_async(refresh_projects_callback)
                                    return None
                                bpy.app.timers.register(main_thread_update, first_interval=0.01)
                            elif action == 'error':
                                def main_thread_error():
                                    scene.clockify_status = f"Error starting timer: {data}"
                                    return None
                                bpy.app.timers.register(main_thread_error, first_interval=0.01)
                        finally:
                            set_operation_in_progress("start", False)
                    
                    start_timer_async(desc, project_id, timer_started_callback)
                    
                elif action == 'error':
                    def main_thread_error():
                        scene.clockify_status = f"Error creating project: {data}"
                        return None
                    bpy.app.timers.register(main_thread_error, first_interval=0.01)
                    set_operation_in_progress("start", False)
            
            create_project_async(new_project_name, project_created_callback)
            
        else:
            set_operation_in_progress("start", True)
            scene.clockify_status = "Starting timer..."
            
            # Get project name
            project_name = "Unknown Project"
            cached_projects = get_cached_projects()
            for p in cached_projects:
                if p[0] == project:
                    project_name = p[1]
                    break
            
            def timer_started_callback(action, data):
                try:
                    if action == 'timer_started':
                        def main_thread_update():
                            handle_timer_started(action, data, project_name, client_name)
                            return None
                        bpy.app.timers.register(main_thread_update, first_interval=0.01)
                    elif action == 'error':
                        def main_thread_error():
                            scene.clockify_status = f"Error: {data}"
                            return None
                        bpy.app.timers.register(main_thread_error, first_interval=0.01)
                finally:
                    set_operation_in_progress("start", False)
            
            start_timer_async(desc, project, timer_started_callback)

class CLOCKIFY_OT_StopTimer(bpy.types.Operator):
    bl_idname = "clockify.stop_timer"
    bl_label = "Stop Timer"
    
    def execute(self, context):
        if is_operation_in_progress("stop"):
            self.report({'WARNING'}, "Timer is already stopping, please wait...")
            return {'CANCELLED'}
        
        scene = context.scene
        set_operation_in_progress("stop", True)
        scene.clockify_status = "Stopping timer..."
        
        def timer_stopped_callback(action, data):
            try:
                if action == 'timer_stopped':
                    def main_thread_update():
                        handle_timer_stopped(action, data)
                        return None
                    bpy.app.timers.register(main_thread_update, first_interval=0.01)
                elif action == 'no_active_timer':
                    def main_thread_no_timer():
                        handle_no_active_timer()
                        return None
                    bpy.app.timers.register(main_thread_no_timer, first_interval=0.01)
                elif action == 'error':
                    def main_thread_error():
                        scene.clockify_status = f"Error stopping timer: {data}"
                        return None
                    bpy.app.timers.register(main_thread_error, first_interval=0.01)
            finally:
                set_operation_in_progress("stop", False)
        
        stop_timer_async(timer_stopped_callback)
        return {'FINISHED'}

class CLOCKIFY_OT_ResetTimerPrompt(bpy.types.Operator):
    bl_idname = "clockify.reset_timer_prompt"
    bl_label = "Reset Blender Timer"
    bl_description = "No active Clockify timer found, reset Blender timer?"
    
    def execute(self, context):
        reset_blender_timer()
        self.report({'INFO'}, "Blender timer has been reset")
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)
    
    def draw(self, context):
        layout = self.layout
        layout.label(text="No active Clockify timer found.", icon='ERROR')
        layout.label(text="Reset Blender timer?")
        
    @classmethod
    def description(cls, context, properties):
        return "No active Clockify timer found, reset Blender timer?"

class CLOCKIFY_OT_ProjectStatus(bpy.types.Operator):
    bl_idname = "clockify.project_status"
    bl_label = "Current Project Status"
    bl_description = "Get total time and billing info for the selected project this month"

    def execute(self, context):
        if is_operation_in_progress("status"):
            self.report({'WARNING'}, "Status check already in progress...")
            return {'CANCELLED'}
        
        scene = context.scene
        project = scene.clockify_project
        
        if project == "CREATE_NEW":
            self.report({'ERROR'}, "Please select a project first")
            return {'CANCELLED'}
        
        set_operation_in_progress("status", True)
        scene.clockify_status = "Getting project status..."
        
        def status_callback(action, data):
            try:
                if action == 'error':
                    def main_thread_error():
                        scene.clockify_status = f"Error getting project status: {data}"
                        return None
                    bpy.app.timers.register(main_thread_error, first_interval=0.01)
            finally:
                set_operation_in_progress("status", False)
        
        get_project_summary_async(project, status_callback)
        return {'FINISHED'}

class CLOCKIFY_OT_CheckTimer(bpy.types.Operator):
    bl_idname = "clockify.check_timer"
    bl_label = "Check Current Timer"

    def execute(self, context):
        scene = context.scene
        scene.clockify_status = "Checking timer..."
        
        def timer_checked_callback(action, data):
            def main_thread_update():
                handle_current_timer(action, data)
                return None
            bpy.app.timers.register(main_thread_update, first_interval=0.01)
        
        get_current_timer_async(timer_checked_callback)
        return {'FINISHED'}

class CLOCKIFY_OT_CheckCredentials(bpy.types.Operator):
    bl_idname = "clockify.check_credentials"
    bl_label = "Check Credentials"
    bl_description = "Verify API credentials and auto-fill User ID"

    def execute(self, context):
        prefs = get_preferences()
        
        if not prefs.api_key or not prefs.workspace_id:
            self.report({'ERROR'}, "Please enter API Key and Workspace ID first")
            return {'CANCELLED'}
        
        context.scene.clockify_status = "Checking credentials..."
        
        def credentials_checked_callback(action, data):
            if action == 'error':
                def main_thread_error():
                    context.scene.clockify_status = f"Error: {data}"
                    return None
                bpy.app.timers.register(main_thread_error, first_interval=0.01)
        
        get_user_info_async(credentials_checked_callback)
        return {'FINISHED'}

class CLOCKIFY_OT_RefreshClients(bpy.types.Operator):
    bl_idname = "clockify.refresh_clients"
    bl_label = "Refresh Clients"
    bl_description = "Refresh the client list from Clockify"

    def execute(self, context):
        scene = context.scene
        scene.clockify_status = "Refreshing clients..."
        
        def clients_refreshed_callback(action, data):
            if action == 'clients_fetched':
                def main_thread_update():
                    scene.clockify_status = "Clients refreshed successfully!"
                    return None
                bpy.app.timers.register(main_thread_update, first_interval=0.01)
            elif action == 'error':
                def main_thread_error():
                    scene.clockify_status = f"Error refreshing clients: {data}"
                    return None
                bpy.app.timers.register(main_thread_error, first_interval=0.01)
        
        fetch_clients_async(clients_refreshed_callback)
        return {'FINISHED'}

class CLOCKIFY_OT_RefreshProjects(bpy.types.Operator):
    bl_idname = "clockify.refresh_projects"
    bl_label = "Refresh Projects"
    bl_description = "Refresh the project list from Clockify"

    def execute(self, context):
        scene = context.scene
        scene.clockify_status = "Refreshing projects..."
        
        def projects_refreshed_callback(action, data):
            if action == 'projects_fetched':
                def main_thread_update():
                    scene.clockify_status = "Projects refreshed successfully!"
                    return None
                bpy.app.timers.register(main_thread_update, first_interval=0.01)
            elif action == 'error':
                def main_thread_error():
                    scene.clockify_status = f"Error refreshing projects: {data}"
                    return None
                bpy.app.timers.register(main_thread_error, first_interval=0.01)
        
        fetch_projects_async(projects_refreshed_callback)
        return {'FINISHED'}

# --- PANEL ---
class CLOCKIFY_PT_TrackerPanel(bpy.types.Panel):
    bl_label = "Clockify Tracker"
    bl_idname = "CLOCKIFY_PT_TrackerPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Clockify'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prefs = get_preferences()
        
        # Client selection dropdown
        row = layout.row()
        row.prop(scene, "clockify_client", text="Client")
        row.operator("clockify.refresh_clients", text="", icon='FILE_REFRESH')
        
        # New client name input
        if scene.clockify_show_new_client_field:
            layout.prop(scene, "clockify_new_client_name", text="New Client Name")
        
        # Task description (will be saved with file)
        row = layout.row()
        row.prop(scene, "clockify_task_description")
        
        # Project selection
        row = layout.row()
        row.prop(scene, "clockify_project", text="Project")
        row.operator("clockify.refresh_projects", text="", icon='FILE_REFRESH')
        
        # New project name input
        if scene.clockify_show_new_project_field:
            layout.prop(scene, "clockify_new_project_name", text="New Project Name")
        
        # Main control buttons
        row = layout.row(align=True)
        start_col = row.column()
        start_col.operator("clockify.start_timer", text="Start Timer", icon='PLAY')
        if is_operation_in_progress("start"):
            start_col.enabled = False
        
        stop_col = row.column()
        stop_col.operator("clockify.stop_timer", text="Stop Timer", icon='SNAP_FACE')
        if is_operation_in_progress("stop"):
            stop_col.enabled = False
        
        # Project status button
        row = layout.row()
        status_col = row.column()
        status_col.operator("clockify.project_status", text="Project Status", icon='PRESET')
        if is_operation_in_progress("status"):
            status_col.enabled = False

        # Status display
        if hasattr(scene, 'clockify_status') and scene.clockify_status:
            box = layout.box()
            for line in scene.clockify_status.split('\n'):
                box.label(text=line, icon='INFO')

        # Project summary (month total) - show regardless of billable setting
        if hasattr(scene, 'clockify_project_summary') and scene.clockify_project_summary:
            box = layout.box()
            box.label(text="📊 Project Summary:", icon='PRESET')
            for line in scene.clockify_project_summary.split('\n'):
                if line.strip():
                    # Only filter out billable info if billable display is disabled
                    if not prefs.show_billable and 'Billable:' in line:
                        continue
                    box.label(text=f"  {line}")

        # Active timer info
        if scene.clockify_active_timer_id:
            box = layout.box()
            box.label(text="⏱ Active Timer:", icon='TIME')
            
            # Show task description if enabled
            if prefs.show_task_name:
                box.label(text=f"Task: {scene.clockify_active_timer_desc}")
            
            # Show project name if enabled    
            if prefs.show_project_name and scene.clockify_active_project_name:
                box.label(text=f"Project: {scene.clockify_active_project_name}")
            
            # Show client name if enabled and available
            if prefs.show_client_name and hasattr(scene, 'clockify_active_client_name') and scene.clockify_active_client_name:
                box.label(text=f"Client: {scene.clockify_active_client_name}")
            
            # Show elapsed time if enabled
            if prefs.show_elapsed_time:
                current_duration = get_current_timer_duration()
                time_display = format_duration_detailed(current_duration)
                box.label(text=f"Elapsed: {time_display}")
            
            # Show billing info if enabled
            if prefs.show_billable:
                current_duration = get_current_timer_duration()
                billing = calculate_billing_info(current_duration)
                if billing['hours'] > 0:
                    box.label(text=f"Billable: ${billing['billable_amount']:.2f} @ ${prefs.hourly_rate}/hr", icon='SOLO_ON')
        
        # Last session summary - only show if enabled and has content
        if (prefs.show_last_session and 
            hasattr(scene, 'clockify_last_session_summary') and 
            scene.clockify_last_session_summary):
            box = layout.box()
            box.label(text="📊 Last Session:", icon='CHECKMARK')
            for line in scene.clockify_last_session_summary.split('\n'):
                if line.strip():
                    box.label(text=f"  {line}")

# --- REGISTER ---
classes = (
    ClockifyPreferences,
    CLOCKIFY_OT_StartTimer,
    CLOCKIFY_OT_StopTimer,
    CLOCKIFY_OT_ResetTimerPrompt,
    CLOCKIFY_OT_ProjectStatus,
    CLOCKIFY_OT_CheckCredentials,
    CLOCKIFY_OT_RefreshClients,
    CLOCKIFY_OT_RefreshProjects,
    CLOCKIFY_PT_TrackerPanel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Client selection dropdown
    bpy.types.Scene.clockify_client = EnumProperty(
        name="Client",
        items=get_client_items,
        update=client_selection_update
    )
    
    bpy.types.Scene.clockify_new_client_name = StringProperty(
        name="New Client Name",
        default="",
        description="Enter the name for the new client"
    )
    
    bpy.types.Scene.clockify_show_new_client_field = BoolProperty(
        name="Show New Client Field",
        default=False
    )

    bpy.types.Scene.clockify_task_description = StringProperty(
        name="Task Description",
        default="Untitled",
        description="Task description (saved with blend file)"
    )
    
    bpy.types.Scene.clockify_project = EnumProperty(
        name="Project",
        items=get_project_items,
        update=project_selection_update
    )
    
    bpy.types.Scene.clockify_new_project_name = StringProperty(
        name="New Project Name",
        default="",
        description="Enter the name for the new project"
    )
    
    bpy.types.Scene.clockify_show_new_project_field = BoolProperty(
        name="Show New Project Field",
        default=False
    )
    
    bpy.types.Scene.clockify_status = StringProperty(
        name="Status",
        default=""
    )
    
    bpy.types.Scene.clockify_last_session_summary = StringProperty(
        name="Last Session Summary",
        default=""
    )
    
    bpy.types.Scene.clockify_project_summary = StringProperty(
        name="Project Summary",
        default=""
    )

    bpy.types.Scene.clockify_active_timer_id = StringProperty()
    bpy.types.Scene.clockify_active_timer_desc = StringProperty()
    bpy.types.Scene.clockify_active_project = StringProperty()
    bpy.types.Scene.clockify_active_project_name = StringProperty()
    bpy.types.Scene.clockify_active_client_name = StringProperty()
    
    # Add the timer display to the top bar
    bpy.types.TOPBAR_HT_upper_bar.append(draw_clockify_timer)
    
    # Add file save/load handlers
    bpy.app.handlers.save_pre.append(save_pre_handler)
    bpy.app.handlers.load_post.append(load_post_handler)
    
    # Start the background task processor
    bpy.app.timers.register(process_api_queue, persistent=True)
    
    # Initialize clients and projects on startup
    fetch_clients_async()
    fetch_projects_async()
    
    # Load task description if file already has one
    load_task_description_from_file()
    
    # Check for existing timer on startup
    def startup_check_callback(action, data):
        if action == 'current_timer' and data:
            start_time_str = data['timeInterval']['start']
            try:
                start_time_dt = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                set_timer_start_time(start_time_dt.timestamp())
                
                if not bpy.app.timers.is_registered(update_timer_display):
                    bpy.app.timers.register(update_timer_display, first_interval=1.0)
            except Exception as e:
                print(f"Error parsing startup timer: {e}")
                set_timer_start_time(time.time())
    
    def delayed_timer_check():
        get_current_timer_async(startup_check_callback)
        return None
    
    bpy.app.timers.register(delayed_timer_check, first_interval=2.0)

def unregister():
    # Stop all operations in progress
    with _operation_lock:
        for key in _operation_in_progress:
            _operation_in_progress[key] = False
    
    for cls in classes:
        bpy.utils.unregister_class(cls)

    # Remove the timer display from the top bar
    try:
        bpy.types.TOPBAR_HT_upper_bar.remove(draw_clockify_timer)
    except Exception as e:
        print(f"Error removing timer display: {e}")
    
    # Remove file handlers
    try:
        bpy.app.handlers.save_pre.remove(save_pre_handler)
        bpy.app.handlers.load_post.remove(load_post_handler)
    except ValueError:
        pass  # Handler wasn't registered
    
    # Unregister all timers
    timers_to_unregister = [process_api_queue, update_timer_display]
    for timer_func in timers_to_unregister:
        if bpy.app.timers.is_registered(timer_func):
            bpy.app.timers.unregister(timer_func)

    # Clean up scene properties
    properties_to_remove = [
        'clockify_client',
        'clockify_new_client_name',
        'clockify_show_new_client_field',
        'clockify_task_description',
        'clockify_project', 
        'clockify_new_project_name',
        'clockify_show_new_project_field',
        'clockify_status',
        'clockify_last_session_summary',
        'clockify_project_summary',
        'clockify_active_timer_id',
        'clockify_active_timer_desc',
        'clockify_active_project',
        'clockify_active_project_name',
        'clockify_active_client_name'
    ]
    
    for prop in properties_to_remove:
        try:
            delattr(bpy.types.Scene, prop)
        except AttributeError:
            pass

if __name__ == "__main__":
    register()