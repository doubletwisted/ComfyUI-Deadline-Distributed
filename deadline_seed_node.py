"""
Deadline Seed Nodes for ComfyUI
Provides seed management for distributed rendering on Deadline
"""

import os
import random
import math
from typing import Any, Dict, List, Tuple, Optional

class DeadlineDistributedSeed:
    """
    Basic seed node for Deadline distributed workflows.
    Automatically generates different seeds for each worker/task based on Deadline environment variables.
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_seed": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 0xffffffffffffffff,
                    "step": 1,
                    "display": "number"
                }),
                "mode": (["increment", "random", "fibonacci"], {
                    "default": "increment"
                }),
            },
            "optional": {
                "seed_offset": ("INT", {
                    "default": 0,
                    "min": -0xffffffffffffffff,
                    "max": 0xffffffffffffffff,
                    "step": 1
                }),
            }
        }
    
    RETURN_TYPES = ("INT", "STRING",)
    RETURN_NAMES = ("seed", "info",)
    FUNCTION = "generate_seed"
    CATEGORY = "deadline"
    
    def generate_seed(self, base_seed: int, mode: str = "increment", seed_offset: int = 0) -> Tuple[int, str]:
        """Generate a seed based on Deadline environment variables"""
        # Check if we're running in Deadline
        worker_id = os.environ.get("DEADLINE_SLAVE_NAME", "")
        task_id = os.environ.get("DEADLINE_TASK_ID", "")
        job_id = os.environ.get("DEADLINE_JOB_ID", "")
        
        if worker_id and task_id:
            # Running in Deadline - generate unique seed per task
            try:
                task_num = int(task_id)
            except:
                task_num = 0
                
            if mode == "increment":
                # Simple increment based on task number
                final_seed = base_seed + task_num + seed_offset
                info = f"Deadline increment: Worker={worker_id}, Task={task_num}, Seed={final_seed}"
                
            elif mode == "random":
                # Use job ID and task ID as random seed
                random.seed(f"{job_id}_{task_id}_{base_seed}")
                final_seed = random.randint(0, 0xffffffffffffffff)
                info = f"Deadline random: Worker={worker_id}, Task={task_num}, Seed={final_seed}"
                
            elif mode == "fibonacci":
                # Fibonacci sequence based on task number
                fib_n = self._fibonacci(task_num + seed_offset)
                final_seed = (base_seed + fib_n) % 0xffffffffffffffff
                info = f"Deadline fibonacci: Worker={worker_id}, Task={task_num}, Fib={fib_n}, Seed={final_seed}"
        else:
            # Not running in Deadline - use base seed
            final_seed = base_seed + seed_offset
            info = f"Local mode (no Deadline): Seed={final_seed}"
            
        return (final_seed, info)
    
    def _fibonacci(self, n: int) -> int:
        """Calculate nth fibonacci number"""
        if n <= 1:
            return n
        a, b = 0, 1
        for _ in range(2, n + 1):
            a, b = b, a + b
        return b
    
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        """Force re-execution when Deadline environment changes"""
        # Include Deadline env vars to ensure node updates per task
        return f"{os.environ.get('DEADLINE_TASK_ID', '')}_{os.environ.get('DEADLINE_SLAVE_NAME', '')}"


class DeadlineSeedControl:
    """
    Advanced seed control for Deadline distributed workflows.
    Provides more control over seed generation including batch mode and custom formulas.
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "seed": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 0xffffffffffffffff,
                    "step": 1,
                    "display": "number"
                }),
                "mode": (["direct", "task_increment", "worker_offset", "batch_random", "custom_formula"], {
                    "default": "task_increment"
                }),
                "batch_size": ("INT", {
                    "default": 1,
                    "min": 1,
                    "max": 100,
                    "step": 1
                }),
            },
            "optional": {
                "multiplier": ("FLOAT", {
                    "default": 1.0,
                    "min": 0.1,
                    "max": 100.0,
                    "step": 0.1
                }),
                "offset": ("INT", {
                    "default": 0,
                    "min": -0xffffffffffffffff,
                    "max": 0xffffffffffffffff,
                    "step": 1
                }),
                "custom_formula": ("STRING", {
                    "default": "seed + task_id * 1000",
                    "multiline": False,
                    "placeholder": "Python expression using: seed, task_id, worker_id, batch_id"
                }),
            }
        }
    
    RETURN_TYPES = ("INT", "STRING",)
    RETURN_NAMES = ("seed", "info",)
    FUNCTION = "control_seed"
    CATEGORY = "deadline"
    
    def control_seed(self, seed: int, mode: str, batch_size: int, 
                    multiplier: float = 1.0, offset: int = 0, 
                    custom_formula: str = "") -> Tuple[int, str]:
        """Advanced seed control based on Deadline context"""
        # Get Deadline environment
        worker_name = os.environ.get("DEADLINE_SLAVE_NAME", "local")
        task_id_str = os.environ.get("DEADLINE_TASK_ID", "0")
        job_id = os.environ.get("DEADLINE_JOB_ID", "local")
        
        try:
            task_id = int(task_id_str)
        except:
            task_id = 0
            
        # Calculate batch info
        batch_id = task_id // batch_size
        batch_task = task_id % batch_size
        
        # Generate worker ID from name
        worker_id = hash(worker_name) % 1000
        
        if mode == "direct":
            final_seed = seed
            info = f"Direct seed: {seed}"
            
        elif mode == "task_increment":
            final_seed = int(seed + (task_id * multiplier) + offset)
            info = f"Task increment: Base={seed}, Task={task_id}, Final={final_seed}"
            
        elif mode == "worker_offset":
            final_seed = int(seed + (worker_id * 10000) + task_id + offset)
            info = f"Worker offset: Worker={worker_name}({worker_id}), Task={task_id}, Final={final_seed}"
            
        elif mode == "batch_random":
            # Same seed for all tasks in a batch
            random.seed(f"{job_id}_batch_{batch_id}_{seed}")
            final_seed = random.randint(0, 0xffffffffffffffff)
            info = f"Batch random: Batch={batch_id}, Task={batch_task}/{batch_size}, Seed={final_seed}"
            
        elif mode == "custom_formula" and custom_formula:
            try:
                # Safe evaluation context
                eval_context = {
                    "seed": seed,
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "batch_id": batch_id,
                    "batch_task": batch_task,
                    "offset": offset,
                    "multiplier": multiplier,
                    "math": math,
                    "random": random,
                }
                final_seed = int(eval(custom_formula, {"__builtins__": {}}, eval_context))
                info = f"Custom formula: {custom_formula} = {final_seed}"
            except Exception as e:
                final_seed = seed
                info = f"Formula error: {str(e)}, using base seed"
        else:
            final_seed = seed
            info = f"Default: {seed}"
            
        # Ensure seed is within valid range
        final_seed = final_seed % 0xffffffffffffffff
        
        return (final_seed, info)
    
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        """Force re-execution when Deadline environment changes"""
        return f"{os.environ.get('DEADLINE_TASK_ID', '')}_{os.environ.get('DEADLINE_SLAVE_NAME', '')}"


# Node registration
NODE_CLASS_MAPPINGS = {
    "DeadlineDistributedSeed": DeadlineDistributedSeed,
    "DeadlineSeedControl": DeadlineSeedControl,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DeadlineDistributedSeed": "Deadline Distributed Seed",
    "DeadlineSeedControl": "Deadline Seed Control (Advanced)",
}