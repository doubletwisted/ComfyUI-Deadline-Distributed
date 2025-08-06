"""
Helper functions for Deadline plugin integration.
This file contains utility functions for the ComfyUI.py Deadline plugin.
"""

import os
from typing import Tuple

def get_distributed_config_for_plugin(plugin) -> Tuple[bool, bool, bool]:
    """
    Get distributed configuration for Deadline plugin with proper fallbacks.
    
    Args:
        plugin: Deadline plugin instance with GetPluginInfoEntryWithDefault methods
        
    Returns:
        Tuple[worker_mode, distributed_mode, force_new_instance]
    """
    # Priority 1: Plugin info entries (preferred)
    worker_mode = plugin.GetBooleanPluginInfoEntryWithDefault("WorkerMode", False)
    distributed_mode = plugin.GetBooleanPluginInfoEntryWithDefault("DistributedMode", False) 
    force_new_instance = plugin.GetBooleanPluginInfoEntryWithDefault("ForceNewInstance", False)
    
    # Priority 2: Environment variables (fallback for backwards compatibility)
    if not worker_mode and not distributed_mode and not force_new_instance:
        worker_mode = os.environ.get('COMFY_WORKER_MODE', '0').lower() in ('1', 'true', 'yes')
        distributed_mode = os.environ.get('DEADLINE_DIST_MODE', '0').lower() in ('1', 'true', 'yes')
        force_new_instance = os.environ.get('COMFY_FORCE_NEW_INSTANCE', '0').lower() in ('1', 'true', 'yes')
        
        if worker_mode or distributed_mode or force_new_instance:
            plugin.LogWarning("Using environment variables for distributed config. Consider updating to plugin info entries.")
    
    # Log the configuration
    plugin.LogInfo(f"Distributed config - WorkerMode: {worker_mode}, DistributedMode: {distributed_mode}, ForceNewInstance: {force_new_instance}")
    
    return worker_mode, distributed_mode, force_new_instance

def log_config_source(plugin, worker_mode: bool, distributed_mode: bool, force_new_instance: bool):
    """Log which configuration source was used"""
    if worker_mode or distributed_mode or force_new_instance:
        # Check if values came from plugin info or environment
        plugin_worker = plugin.GetBooleanPluginInfoEntryWithDefault("WorkerMode", False)
        plugin_dist = plugin.GetBooleanPluginInfoEntryWithDefault("DistributedMode", False)
        plugin_force = plugin.GetBooleanPluginInfoEntryWithDefault("ForceNewInstance", False)
        
        if plugin_worker or plugin_dist or plugin_force:
            plugin.LogInfo("✅ Using plugin info entries for distributed configuration")
        else:
            plugin.LogInfo("⚠️ Using environment variables for distributed configuration")

def get_task_specific_config(plugin) -> dict:
    """Get task-specific configuration that doesn't change the plugin info pattern"""
    return {
        'task_id': int(os.environ.get('DEADLINE_TASK_ID', '1')),
        'worker_name': os.environ.get('DEADLINE_SLAVE_NAME', 'unknown'),
        'job_id': os.environ.get('DEADLINE_JOB_ID', 'unknown'),
        'master_ws': os.environ.get('COMFY_MASTER_WS', 'localhost:8188'),
        'master_host': os.environ.get('COMFY_MASTER_HOST', 'localhost'),
        'master_port': os.environ.get('COMFY_MASTER_PORT', '8188'),
    }