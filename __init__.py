import os
import sys

# Add the directory to Python path
sys.path.append(os.path.dirname(__file__))

# Patch ComfyUI execution validation for DistributedVideoCollector
try:
    import execution
    from .distributed import ImageBatchDivider
    
    # Store original validate function if it exists
    if hasattr(execution, 'validate_outputs'):
        original_validate_outputs = execution.validate_outputs
        
        def patched_validate_outputs(executor, node_id, result, node_class):
            if node_class == ImageBatchDivider:
                return  # Skip validation for our dynamic output node
            return original_validate_outputs(executor, node_id, result, node_class)
        
        execution.validate_outputs = patched_validate_outputs
    else:
        # Fallback: patch the main execute method if validate_outputs doesn't exist
        if hasattr(execution.PromptExecutor, 'execute'):
            original_execute = execution.PromptExecutor.execute
            
            def patched_execute(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
                # This is a more complex patch - for now just call original
                # The ByPassTypeTuple should handle most validation issues
                return original_execute(self, prompt, prompt_id, extra_data, execute_outputs)
            
            execution.PromptExecutor.execute = patched_execute
            
except ImportError:
    pass  # ComfyUI execution module not available during import

# Import everything needed from the main module
from .distributed import (
    NODE_CLASS_MAPPINGS as DISTRIBUTED_CLASS_MAPPINGS, 
    NODE_DISPLAY_NAME_MAPPINGS as DISTRIBUTED_DISPLAY_NAME_MAPPINGS
)

# Import utilities
from .utils.config import ensure_config_exists, CONFIG_FILE
from .utils.logging import debug_log

# Import distributed upscale nodes
from .distributed_upscale import (
    NODE_CLASS_MAPPINGS as UPSCALE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as UPSCALE_DISPLAY_NAME_MAPPINGS
)

# Import Deadline integration nodes
from .deadline_submit import (
    NODE_CLASS_MAPPINGS as DEADLINE_SUBMIT_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as DEADLINE_SUBMIT_DISPLAY_NAME_MAPPINGS
)

from .deadline_seed_node import (
    NODE_CLASS_MAPPINGS as DEADLINE_SEED_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as DEADLINE_SEED_DISPLAY_NAME_MAPPINGS
)

# Initialize Deadline integration API endpoints
from . import deadline_integration

WEB_DIRECTORY = "./web"

ensure_config_exists()

# Merge node mappings
NODE_CLASS_MAPPINGS = {
    **DISTRIBUTED_CLASS_MAPPINGS, 
    **UPSCALE_CLASS_MAPPINGS,
    **DEADLINE_SUBMIT_CLASS_MAPPINGS,
    **DEADLINE_SEED_CLASS_MAPPINGS
}

NODE_DISPLAY_NAME_MAPPINGS = {
    **DISTRIBUTED_DISPLAY_NAME_MAPPINGS, 
    **UPSCALE_DISPLAY_NAME_MAPPINGS,
    **DEADLINE_SUBMIT_DISPLAY_NAME_MAPPINGS,
    **DEADLINE_SEED_DISPLAY_NAME_MAPPINGS
}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']

debug_log("Loaded Distributed + Deadline nodes.")
debug_log(f"Config file: {CONFIG_FILE}")
debug_log(f"Available nodes: {list(NODE_CLASS_MAPPINGS.keys())}")