import os
import sys

# Add the directory to Python path
sys.path.append(os.path.dirname(__file__))

# Patch ComfyUI execution validation for DistributedVideoCollector
try:
    import execution
    from .distributed import ImageBatchDivider
    
    if hasattr(execution, 'validate_outputs'):
        original_validate_outputs = execution.validate_outputs
        
        def patched_validate_outputs(executor, node_id, result, node_class):
            if node_class == ImageBatchDivider:
                return  # Skip validation for our dynamic output node
            return original_validate_outputs(executor, node_id, result, node_class)
        
        execution.validate_outputs = patched_validate_outputs
            
except ImportError:
    pass  # ComfyUI execution module not available during import

# Import everything needed from the main module
from .distributed import (
    NODE_CLASS_MAPPINGS as DISTRIBUTED_CLASS_MAPPINGS, 
    NODE_DISPLAY_NAME_MAPPINGS as DISTRIBUTED_DISPLAY_NAME_MAPPINGS
)

# Import distributed upscale nodes
from .distributed_upscale import (
    NODE_CLASS_MAPPINGS as UPSCALE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as UPSCALE_DISPLAY_NAME_MAPPINGS
)

# Import Deadline integration nodes from ComfyUI-Deadline-Plugin
try:
    # Try different possible import paths for ComfyUI-Deadline-Plugin
    try:
        from ComfyUI_Deadline_Plugin.deadline_submit import (
            NODE_CLASS_MAPPINGS as SUBMIT_CLASS_MAPPINGS,
            NODE_DISPLAY_NAME_MAPPINGS as SUBMIT_DISPLAY_NAME_MAPPINGS
        )
    except ImportError:
        # Alternative import path if installed differently
        import sys
        import os
        deadline_plugin_path = os.path.join(os.path.dirname(__file__), '..', 'ComfyUI-Deadline-Plugin')
        if os.path.exists(deadline_plugin_path):
            sys.path.insert(0, deadline_plugin_path)
            from deadline_submit import (
                NODE_CLASS_MAPPINGS as SUBMIT_CLASS_MAPPINGS,
                NODE_DISPLAY_NAME_MAPPINGS as SUBMIT_DISPLAY_NAME_MAPPINGS
            )
        else:
            raise ImportError("ComfyUI-Deadline-Plugin not found")
except ImportError:
    # Fallback if ComfyUI-Deadline-Plugin is not available
    print("Warning: ComfyUI-Deadline-Plugin not found. Please install it from:")
    print("  https://github.com/doubletwisted/ComfyUI-Deadline-Plugin")
    print("DeadlineSubmit node will not be available.")
    SUBMIT_CLASS_MAPPINGS = {}
    SUBMIT_DISPLAY_NAME_MAPPINGS = {}

# deadline_seed_node.py removed - DeadlineDistributedSeed is now an alias in distributed.py

from .deadline_worker_registration import (
    NODE_CLASS_MAPPINGS as WORKER_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as WORKER_DISPLAY_NAME_MAPPINGS
)

# Import utilities
from .utils.config import ensure_config_exists, CONFIG_FILE
from .utils.logging import debug_log

# Initialize Deadline integration API endpoints
from . import deadline_integration_simple as deadline_integration

WEB_DIRECTORY = "./web"

ensure_config_exists()

# Merge node mappings
NODE_CLASS_MAPPINGS = {
    **DISTRIBUTED_CLASS_MAPPINGS, 
    **UPSCALE_CLASS_MAPPINGS,
    **SUBMIT_CLASS_MAPPINGS,
    **WORKER_CLASS_MAPPINGS
}

NODE_DISPLAY_NAME_MAPPINGS = {
    **DISTRIBUTED_DISPLAY_NAME_MAPPINGS, 
    **UPSCALE_DISPLAY_NAME_MAPPINGS,
    **SUBMIT_DISPLAY_NAME_MAPPINGS,
    **WORKER_DISPLAY_NAME_MAPPINGS
}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']

debug_log(f"ComfyUI-Deadline-Distributed loaded: {len(NODE_CLASS_MAPPINGS)} nodes available")
debug_log("Loaded Distributed nodes.")
debug_log(f"Config file: {CONFIG_FILE}")
debug_log(f"Available nodes: {list(NODE_CLASS_MAPPINGS.keys())}")