"""
Shared constants for ComfyUI-Distributed.
"""
import os

# Defaults for runtime-configurable settings
DEFAULT_WORKER_RESULT_WAIT_TIMEOUT = 60.0
DEFAULT_MAX_BATCH = int(os.environ.get('COMFYUI_MAX_BATCH', '20'))
DEFAULT_WORKER_HEARTBEAT_GRACE_TIMEOUT = int(os.environ.get('COMFYUI_HEARTBEAT_TIMEOUT', '60'))

# Timeouts (in seconds)
WORKER_RESULT_WAIT_TIMEOUT = DEFAULT_WORKER_RESULT_WAIT_TIMEOUT
FIRST_RESULT_TIMEOUT_MULTIPLIER = 3.0
TILE_COLLECTION_TIMEOUT = 30.0
TILE_WAIT_TIMEOUT = 30.0
PROCESS_TERMINATION_TIMEOUT = 5.0

# Process monitoring
WORKER_CHECK_INTERVAL = 2.0
STATUS_CHECK_INTERVAL = 5.0

# Network
CHUNK_SIZE = 8192
LOG_TAIL_BYTES = 65536  # 64KB

# File paths
WORKER_LOG_PATTERN = "distributed_worker_*.log"

# Worker management
WORKER_STARTUP_DELAY = 2.0

# Tile transfer
TILE_TRANSFER_TIMEOUT = 30.0

# Process cleanup
PROCESS_WAIT_TIMEOUT = 3.0
QUEUE_INIT_TIMEOUT = 5.0
TILE_SEND_TIMEOUT = 60.0

# Memory operations  
MEMORY_CLEAR_DELAY = 0.5

# Batch processing
MAX_BATCH = DEFAULT_MAX_BATCH  # Maximum items per batch to prevent timeouts/OOM (~100MB chunks for 512x512 PNGs)

# Heartbeat monitoring
WORKER_HEARTBEAT_GRACE_TIMEOUT = DEFAULT_WORKER_HEARTBEAT_GRACE_TIMEOUT

# Backward-compatible names for existing imports/config files.
DEFAULT_WORKER_JOB_TIMEOUT = DEFAULT_WORKER_RESULT_WAIT_TIMEOUT
DEFAULT_HEARTBEAT_TIMEOUT = DEFAULT_WORKER_HEARTBEAT_GRACE_TIMEOUT
WORKER_JOB_TIMEOUT = WORKER_RESULT_WAIT_TIMEOUT
HEARTBEAT_TIMEOUT = WORKER_HEARTBEAT_GRACE_TIMEOUT

def _get_setting(settings, key, legacy_key, default):
    """Read a setting using the clearer key first, then the legacy key."""
    return settings.get(key, settings.get(legacy_key, default))

def reload_constants():
    """Reload key constants from gpu_config.json settings."""
    try:
        from .config import load_config
        config = load_config()
        settings = config.get('settings', {})

        global WORKER_RESULT_WAIT_TIMEOUT, WORKER_JOB_TIMEOUT
        global MAX_BATCH, WORKER_HEARTBEAT_GRACE_TIMEOUT, HEARTBEAT_TIMEOUT

        WORKER_RESULT_WAIT_TIMEOUT = float(_get_setting(
            settings,
            'worker_result_wait_timeout',
            'worker_job_timeout',
            DEFAULT_WORKER_RESULT_WAIT_TIMEOUT
        ))
        WORKER_JOB_TIMEOUT = WORKER_RESULT_WAIT_TIMEOUT

        MAX_BATCH = int(settings.get('max_batch', DEFAULT_MAX_BATCH))

        WORKER_HEARTBEAT_GRACE_TIMEOUT = int(_get_setting(
            settings,
            'worker_heartbeat_grace_timeout',
            'heartbeat_timeout',
            DEFAULT_WORKER_HEARTBEAT_GRACE_TIMEOUT
        ))
        HEARTBEAT_TIMEOUT = WORKER_HEARTBEAT_GRACE_TIMEOUT
    except Exception:
        # Keep existing values on error
        pass

def get_worker_result_wait_timeout():
    reload_constants()
    return WORKER_RESULT_WAIT_TIMEOUT

def get_worker_job_timeout():
    return get_worker_result_wait_timeout()

def get_max_batch():
    reload_constants()
    return MAX_BATCH

def get_worker_heartbeat_grace_timeout():
    reload_constants()
    return WORKER_HEARTBEAT_GRACE_TIMEOUT

def get_heartbeat_timeout():
    return get_worker_heartbeat_grace_timeout()

# Load settings on import
reload_constants()
