import os
import sys

# Add the directory to Python path
sys.path.append(os.path.dirname(__file__))

# Patch ComfyUI execution validation for DistributedVideoCollector
try:
    import execution
    from .distributed import ImageBatchDivider
    
    if hasattr(execution, 'validate_outputs'):
        current_validate_outputs = execution.validate_outputs
        if not getattr(current_validate_outputs, "_deadline_distributed_patched", False):
            original_validate_outputs = current_validate_outputs

            def patched_validate_outputs(executor, node_id, result, node_class, *args, **kwargs):
                if node_class == ImageBatchDivider:
                    return  # Skip validation for our dynamic output node
                return original_validate_outputs(executor, node_id, result, node_class, *args, **kwargs)

            patched_validate_outputs._deadline_distributed_patched = True
            patched_validate_outputs._deadline_distributed_original = original_validate_outputs
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

def _install_windows_asyncio_connection_reset_filter():
    """Suppress noisy Windows Proactor cleanup logs for closed worker connections."""
    if os.name != "nt":
        return

    try:
        import server

        loop = getattr(server.PromptServer.instance, "loop", None)
        if loop is None or getattr(loop, "_deadline_distributed_connection_reset_filter", False):
            return

        previous_handler = loop.get_exception_handler()

        def connection_reset_filter(event_loop, context):
            exception = context.get("exception")
            message = context.get("message", "")
            handle = repr(context.get("handle", ""))
            is_proactor_pipe_cleanup = (
                "_ProactorBasePipeTransport._call_connection_lost" in message
                or "_ProactorBasePipeTransport._call_connection_lost" in handle
            )
            is_connection_reset = isinstance(exception, ConnectionResetError)
            error_code = getattr(exception, "winerror", None) or getattr(exception, "errno", None)
            is_windows_remote_close = error_code == 10054

            if is_proactor_pipe_cleanup and is_connection_reset and is_windows_remote_close:
                debug_log("Suppressed Windows connection reset during worker pipe cleanup")
                return

            if previous_handler:
                previous_handler(event_loop, context)
            else:
                event_loop.default_exception_handler(context)

        loop.set_exception_handler(connection_reset_filter)
        loop._deadline_distributed_connection_reset_filter = True
    except Exception as e:
        debug_log(f"Could not install Windows asyncio connection reset filter: {e}")

# Initialize Deadline integration API endpoints
from . import deadline_integration_simple as deadline_integration

WEB_DIRECTORY = "./web"

ensure_config_exists()
_install_windows_asyncio_connection_reset_filter()

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
