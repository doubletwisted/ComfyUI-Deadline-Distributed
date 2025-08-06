"""
Centralized configuration for Deadline distributed processing.
Provides plugin info entry support with environment variable fallback.
"""

import os
from typing import Tuple, Optional, Dict, Any

def get_distributed_config() -> Tuple[bool, bool, bool]:
    """
    Get distributed configuration with priority:
    1. Plugin info entries (when in Deadline context)
    2. Environment variables (fallback)
    3. Defaults
    
    Returns:
        Tuple[worker_mode, distributed_mode, force_new_instance]
    """
    try:
        # Try to get from Deadline plugin context if available
        import Deadline.Plugins.PluginLoader as PluginLoader
        plugin = PluginLoader.GetDeadlinePlugin()
        if plugin:
            worker_mode = plugin.GetBooleanPluginInfoEntryWithDefault("WorkerMode", False)
            distributed_mode = plugin.GetBooleanPluginInfoEntryWithDefault("DistributedMode", False)
            force_new_instance = plugin.GetBooleanPluginInfoEntryWithDefault("ForceNewInstance", False)
            
            # Log for debugging
            plugin.LogInfo(f"Plugin Info - WorkerMode: {worker_mode}, DistributedMode: {distributed_mode}, ForceNewInstance: {force_new_instance}")
            return worker_mode, distributed_mode, force_new_instance
    except Exception:
        # Fallback to environment variables if plugin context not available
        pass
    
    # Environment variable fallback
    worker_mode = os.environ.get('COMFY_WORKER_MODE', '0').lower() in ('1', 'true', 'yes')
    distributed_mode = os.environ.get('DEADLINE_DIST_MODE', '0').lower() in ('1', 'true', 'yes')
    force_new_instance = os.environ.get('COMFY_FORCE_NEW_INSTANCE', '0').lower() in ('1', 'true', 'yes')
    
    # Log for debugging
    if worker_mode or distributed_mode or force_new_instance:
        print(f"[Distributed Config] Environment - WorkerMode: {worker_mode}, DistributedMode: {distributed_mode}, ForceNewInstance: {force_new_instance}")
    
    return worker_mode, distributed_mode, force_new_instance

def get_deadline_context() -> Dict[str, str]:
    """
    Get Deadline-specific context information from environment variables.
    These are always from environment since they're set by Deadline directly.
    
    Returns:
        Dict with deadline worker info
    """
    return {
        'worker_id': os.environ.get('DEADLINE_SLAVE_NAME', 'unknown'),
        'task_id': os.environ.get('DEADLINE_TASK_ID', 'unknown'),
        'job_id': os.environ.get('DEADLINE_JOB_ID', 'unknown'),
        'master_ws': os.environ.get('COMFY_MASTER_WS', 'localhost:8188'),
        'master_host': os.environ.get('COMFY_MASTER_HOST', 'localhost'),
        'master_port': os.environ.get('COMFY_MASTER_PORT', '8188'),
    }

def get_plugin_info_entry_with_default(key: str, default_value: Any) -> Any:
    """
    Get a plugin info entry with fallback to environment and default.
    
    Args:
        key: Plugin info key
        default_value: Default value if not found
        
    Returns:
        Value from plugin info, environment, or default
    """
    try:
        # Try plugin info first
        import Deadline.Plugins.PluginLoader as PluginLoader
        plugin = PluginLoader.GetDeadlinePlugin()
        if plugin:
            if isinstance(default_value, bool):
                return plugin.GetBooleanPluginInfoEntryWithDefault(key, default_value)
            elif isinstance(default_value, int):
                return int(plugin.GetPluginInfoEntryWithDefault(key, str(default_value)))
            else:
                return plugin.GetPluginInfoEntryWithDefault(key, str(default_value))
    except Exception:
        pass
    
    # Fallback to environment variable
    env_key = key.upper().replace(' ', '_')
    env_value = os.environ.get(env_key)
    if env_value is not None:
        if isinstance(default_value, bool):
            return env_value.lower() in ('1', 'true', 'yes')
        elif isinstance(default_value, int):
            try:
                return int(env_value)
            except ValueError:
                pass
        else:
            return env_value
    
    return default_value

def log_distributed_config():
    """Log the current distributed configuration for debugging"""
    worker_mode, distributed_mode, force_new_instance = get_distributed_config()
    deadline_context = get_deadline_context()
    
    print("[Distributed Config] Current configuration:")
    print(f"  WorkerMode: {worker_mode}")
    print(f"  DistributedMode: {distributed_mode}")
    print(f"  ForceNewInstance: {force_new_instance}")
    print(f"  Deadline Context: {deadline_context}")