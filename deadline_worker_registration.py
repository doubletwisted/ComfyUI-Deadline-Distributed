"""
Deadline Worker Registration Node

This node handles automatic registration of Deadline workers with the ComfyUI-Distributed master.
When a Deadline worker processes this node, it registers itself with the master instance.
"""

import os
import socket
import requests
import time
import json
from typing import Dict, Any, Tuple

# Import ComfyUI-Distributed utilities
try:
    from .utils.logging import debug_log, log
except:
    def debug_log(msg): print(f"[DEBUG] {msg}")
    def log(msg): print(f"[LOG] {msg}")

# Import centralized configuration
try:
    from .utils.distributed_config import get_distributed_config, get_deadline_context
except ImportError:
    # Fallback if utils not available
    def get_distributed_config():
        worker_mode = os.environ.get('COMFY_WORKER_MODE', '0') == '1'
        distributed_mode = os.environ.get('DEADLINE_DIST_MODE', '0') == '1'
        force_new_instance = os.environ.get('COMFY_FORCE_NEW_INSTANCE', '0') == '1'
        return worker_mode, distributed_mode, force_new_instance
    
    def get_deadline_context():
        return {
            'worker_id': os.environ.get('DEADLINE_SLAVE_NAME', 'unknown'),
            'task_id': os.environ.get('DEADLINE_TASK_ID', 'unknown'), 
            'job_id': os.environ.get('DEADLINE_JOB_ID', 'unknown'),
            'master_ws': os.environ.get('COMFY_MASTER_WS', 'localhost:8188'),
        }

class DeadlineWorkerRegistration:
    """Node for registering Deadline workers with ComfyUI-Distributed master"""
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                "trigger": ("*", {"tooltip": "Connect any input to trigger registration"})
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    FUNCTION = "register_worker"
    CATEGORY = "deadline/worker"
    OUTPUT_NODE = True
    DESCRIPTION = "Registers Deadline worker with ComfyUI-Distributed master"

    def register_worker(self, trigger=None):
        """Register this Deadline worker with the master ComfyUI instance"""
        
        debug_log("🔄 DeadlineWorkerRegistration.register_worker() called")
        # Get distributed configuration (plugin info with environment fallback)
        worker_mode, distributed_mode, force_new_instance = get_distributed_config()
        deadline_context = get_deadline_context()
        
        debug_log(f"Configuration:")
        debug_log(f"  WorkerMode: {worker_mode}")
        debug_log(f"  DistributedMode: {distributed_mode}")
        debug_log(f"  ForceNewInstance: {force_new_instance}")
        debug_log(f"  Deadline Context: {deadline_context}")
        
        # Each Deadline worker process should register independently
        # This supports multi-GPU machines with multiple workers per machine
        job_id = deadline_context['job_id']
        task_id = deadline_context['task_id']
        worker_id = self._get_worker_id()
        
        import socket
        hostname = socket.gethostname()
        
        debug_log(f"🔄 Task {task_id} of job {job_id} registering worker {worker_id} on machine {hostname}")
        debug_log(f"📍 Each Deadline worker process registers independently (supports multi-GPU machines)")
        
        # Check if we're in Deadline distributed mode
        if not distributed_mode:
            message = "❌ Not in Deadline distributed mode - registration skipped"
            debug_log(message)
            return {"ui": {"status": [message]}, "result": (message,)}
        
        master_ws = deadline_context['master_ws']
        master_host = deadline_context['master_host']
        master_port = int(deadline_context['master_port'])
        
        # Get worker information
        worker_id = self._get_worker_id()
        worker_ip = self._get_worker_ip()
        worker_port = self._get_worker_port()
        job_id = os.environ.get('DEADLINE_JOB_ID', '')
        
        debug_log(f"Registering Deadline worker: {worker_id} at {worker_ip}:{worker_port}")
        debug_log(f"Master: {master_host}:{master_port}")
        
        try:
            # Register with master
            registration_data = {
                'worker_id': worker_id,
                'worker_ip': worker_ip,
                'worker_port': worker_port,
                'job_id': job_id
            }
            
            response = requests.post(
                f"http://{master_host}:{master_port}/deadline/register_worker",
                json=registration_data,
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    log(f"✅ Successfully registered Deadline worker {worker_id} with master")
                    
                    # Start heartbeat in background
                    self._start_heartbeat(master_host, master_port, worker_id)
                    
                    message = f"✅ Registered worker {worker_id} - Registration complete"
                    debug_log(f"Registration workflow completed. Deadline plugin will handle keep-alive.")
                    
                    return {"ui": {"status": [message]}, "result": (message,)}
                else:
                    error_msg = f"❌ Registration failed: {result.get('error', 'Unknown error')}"
                    log(error_msg)
                    return {"ui": {"status": [error_msg]}, "result": (error_msg,)}
            else:
                error_msg = f"❌ Registration HTTP error: {response.status_code}"
                log(error_msg)
                return {"ui": {"status": [error_msg]}, "result": (error_msg,)}
                
        except Exception as e:
            error_msg = f"❌ Registration error: {str(e)}"
            log(error_msg)
            return {"ui": {"status": [error_msg]}, "result": (error_msg,)}
    
    def _get_worker_id(self) -> str:
        """Generate unique worker ID"""
        hostname = socket.gethostname()
        # Get task ID from Deadline - this might be in DEADLINE_WORKER_ID or DEADLINE_SLAVE_ID
        task_id = (os.environ.get('DEADLINE_TASK_ID') or 
                  os.environ.get('DEADLINE_WORKER_ID') or 
                  os.environ.get('DEADLINE_SLAVE_ID') or 
                  'w1')
        job_id = (os.environ.get('DEADLINE_JOB_ID') or 
                 os.environ.get('DEADLINE_JOBID') or 
                 'unknown')
        # Truncate job_id to last 8 characters for readability
        job_short = job_id[-8:] if len(job_id) > 8 else job_id
        return f"deadline-{hostname}-{job_short}-{task_id}"
    
    def _get_worker_ip(self) -> str:
        """Get worker's IP address"""
        try:
            # Try to get IP that can reach the master
            master_host = os.environ.get('COMFY_MASTER_HOST', 'localhost')
            
            # If master is on localhost/127.0.0.1, we're on same machine
            if master_host in ['localhost', '127.0.0.1']:
                return '127.0.0.1'
            
            # Otherwise, find our IP that can reach the master
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((master_host, 80))
                return s.getsockname()[0]
        except:
            # Ultimate fallback
            return '127.0.0.1'
    
    def _get_worker_port(self) -> int:
        """Get worker's ComfyUI port"""
        # For Deadline workers, get the actual worker port from environment
        # The Deadline plugin sets this based on task ID
        try:
            # Try to detect from current ComfyUI process
            import sys
            for arg in sys.argv:
                if arg.startswith('--port'):
                    next_idx = sys.argv.index(arg) + 1
                    if next_idx < len(sys.argv):
                        return int(sys.argv[next_idx])
                elif '=' in arg and arg.startswith('--port='):
                    return int(arg.split('=')[1])
        except:
            pass
        
        # Fallback: calculate from task ID like the Deadline plugin does
        try:
            task_id = int(os.environ.get('DEADLINE_TASK_ID', '1'))
            base_port = 8188
            return base_port + 100 + task_id  # Same logic as ComfyUI.py
        except:
            return 8289  # Default worker port
    
    def _start_heartbeat(self, master_host: str, master_port: int, worker_id: str):
        """Start heartbeat thread to keep worker registered"""
        import threading
        import time
        
        def heartbeat_thread():
            while True:
                try:
                    time.sleep(30)  # Heartbeat every 30 seconds
                    
                    response = requests.post(
                        f"http://{master_host}:{master_port}/deadline/worker_heartbeat",
                        json={'worker_id': worker_id},
                        timeout=5
                    )
                    
                    if response.status_code == 200:
                        debug_log(f"Heartbeat sent for worker {worker_id}")
                    else:
                        debug_log(f"Heartbeat failed for worker {worker_id}: {response.status_code}")
                        
                except Exception as e:
                    debug_log(f"Heartbeat error for worker {worker_id}: {e}")
                    # Continue trying - master might be temporarily unavailable
        
        # Start heartbeat thread
        thread = threading.Thread(target=heartbeat_thread, daemon=True)
        thread.start()
        debug_log(f"Started heartbeat thread for worker {worker_id}")
    
    def _keep_worker_alive(self, worker_id: str):
        """Keep the worker alive indefinitely until manually stopped"""
        import time
        import threading
        
        debug_log(f"🔄 Worker {worker_id} entering keep-alive mode...")
        
        # Start a daemon thread to keep the process alive
        def keep_alive_loop():
            try:
                # This loop will keep running, preventing the task from completing
                while True:
                    debug_log(f"🔄 Worker {worker_id} is alive and waiting for work...")
                    time.sleep(60)  # Check every minute
            except KeyboardInterrupt:
                debug_log(f"🛑 Worker {worker_id} keep-alive interrupted")
            except Exception as e:
                debug_log(f"❌ Worker {worker_id} keep-alive error: {e}")
        
        # Start keep-alive in daemon thread
        keep_alive_thread = threading.Thread(target=keep_alive_loop, daemon=True)
        keep_alive_thread.start()
        
        # Also block the main thread to prevent task completion
        try:
            debug_log(f"🔄 Main thread blocking to keep task alive...")
            while True:
                time.sleep(30)  # Keep main thread alive
        except KeyboardInterrupt:
            debug_log(f"🛑 Worker {worker_id} main thread interrupted")
        except Exception as e:
            debug_log(f"❌ Worker {worker_id} main thread error: {e}")

# Node registration
NODE_CLASS_MAPPINGS = {
    "DeadlineWorkerRegistration": DeadlineWorkerRegistration
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DeadlineWorkerRegistration": "Deadline Worker Registration"
}