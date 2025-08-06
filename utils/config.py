"""
Configuration management for ComfyUI-Distributed.
"""
import os
import json

# Handle logging import gracefully
try:
    from .logging import log
except ImportError:
    # Fallback if relative import fails
    def log(message):
        print(f"[Config] {message}")

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gpu_config.json")

def get_default_config():
    """Returns the default configuration dictionary. Single source of truth."""
    return {
        "master": {"host": ""},
        "workers": [],
        "settings": {
            "debug": False,
            "auto_launch_workers": False,
            "stop_workers_on_master_exit": True
        },
        "deadline": {
            "priority": 50,
            "pool": "none",
            "group": "none"
        }
    }

def load_config():
    """Loads the config, falling back to defaults if the file is missing or invalid."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            log(f"Error loading config, using defaults: {e}")
    return get_default_config()

def save_config(config):
    """Saves the configuration to file."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        log(f"Error saving config: {e}")
        return False

def ensure_config_exists():
    """Creates default config file if it doesn't exist. Used by __init__.py"""
    if not os.path.exists(CONFIG_FILE):
        default_config = get_default_config()
        if save_config(default_config):
            from .logging import debug_log
            debug_log("Created default config file")
        else:
            log("Could not create default config file")