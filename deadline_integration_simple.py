"""
Simple Deadline Integration Module for ComfyUI-Deadline-Distributed

This module provides basic Deadline integration without server dependencies.
"""

import subprocess
import json
import os
import tempfile
import time
import socket
from typing import Dict, List, Optional, Any

# Import ComfyUI-Distributed utilities with fallback
try:
    from utils.logging import debug_log, log
except ImportError:
    def debug_log(msg): print(f"[DEBUG] {msg}")
    def log(msg): print(f"[LOG] {msg}")

class DeadlineIntegration:
    """Main class for Deadline render farm integration"""
    
    def __init__(self):
        self.deadline_command = self._find_deadline_command()
        self.claimed_workers = {}
        self.active_jobs = {}
        self.worker_heartbeat_timeout = 60  # seconds
        debug_log("DeadlineIntegration initialized")

    def _find_deadline_command(self) -> Optional[str]:
        """Find the Deadline command executable"""
        # First try environment variable DEADLINE_PATH
        deadline_bin = os.environ.get('DEADLINE_PATH', '')
        debug_log(f"DEADLINE_PATH environment variable: {deadline_bin}")
        
        if deadline_bin and os.path.exists(os.path.join(deadline_bin, 'deadlinecommand.exe')):
            deadline_command = os.path.join(deadline_bin, 'deadlinecommand.exe')
            debug_log(f"✅ Found working Deadline command via DEADLINE_PATH: {deadline_command}")
            return deadline_command
        
        # Try common locations
        common_paths = [
            r"C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe",
            r"C:\Program Files (x86)\Thinkbox\Deadline10\bin\deadlinecommand.exe",
        ]
        
        for path in common_paths:
            if os.path.exists(path):
                debug_log(f"✅ Found Deadline command at: {path}")
                return path
        
        debug_log("❌ Deadline command not found")
        return None

    async def get_worker_status(self) -> Dict[str, Any]:
        """Get Deadline worker status"""
        if not self.deadline_command:
            return {"available": False, "error": "Deadline not available"}
        
        try:
            # Get current active workers (removes stale ones)
            active_workers = self.get_active_workers()
            
            # Return basic worker status for now
            # For now, return basic status
            return {
                "available": True,
                "available_workers": 16,  # Default render farm size
                "claimed_workers": len(active_workers),
                "total_workers": 16,
                "active_jobs": len(self.active_jobs),
                "active_workers": active_workers
            }
        except Exception as e:
            debug_log(f"Error getting worker status: {e}")
            return {"available": False, "error": str(e)}

    async def claim_workers(self, count: int = 1, master_ws: str = "localhost:8188", priority: int = 50, pool: str = "none", group: str = "none") -> Dict[str, Any]:
        """Claim Deadline workers for distributed processing"""
        if not self.deadline_command:
            return {"success": False, "error": "Deadline not available"}
            
        try:
            # Submit a distributed worker job to Deadline using proper job structure
            job_name = f"[DIST] ComfyUI Workers x{count}"
            
            # Create job submission files (like original DeadlineSubmit)
            job_info_file, plugin_info_file, workflow_file = self._create_worker_job_files(job_name, count, master_ws, priority, pool, group)
            
            # Submit to Deadline using proper arguments (must include workflow file)
            command_args = [job_info_file, plugin_info_file, workflow_file]
            result = subprocess.run([
                self.deadline_command,
                "-SubmitJob"
            ] + command_args, capture_output=True, text=True, timeout=30)
            
            debug_log(f"Deadline submission result - Return code: {result.returncode}")
            debug_log(f"Deadline submission stdout: {result.stdout}")
            if result.stderr:
                debug_log(f"Deadline submission stderr: {result.stderr}")
            
            if result.returncode == 0:
                job_id = self._extract_job_id(result.stdout)
                if job_id:
                    self.active_jobs[job_id] = {
                        "type": "distributed",
                        "count": count,
                        "master_ws": master_ws,
                        "submitted_at": time.time()
                    }
                    
                    return {
                        "success": True,
                        "job_id": job_id,
                        "message": f"Successfully claimed {count} workers for distributed processing (Job: {job_id})"
                    }
                else:
                    return {"success": False, "error": "Job submitted but JobID not found in output"}
            else:
                return {"success": False, "error": f"Deadline submission failed with code {result.returncode}: {result.stderr}"}
                
        except Exception as e:
            debug_log(f"Error claiming workers: {e}")
            return {"success": False, "error": str(e)}

    async def release_workers(self, job_ids: List[str] = None) -> Dict[str, Any]:
        """Release claimed Deadline workers"""
        if not self.deadline_command:
            return {"success": False, "error": "Deadline not available"}
            
        try:
            if job_ids:
                # Release specific jobs
                jobs_to_release = [jid for jid in job_ids if jid in self.active_jobs]
            else:
                # Release all distributed jobs
                jobs_to_release = [
                    jid for jid, job_data in self.active_jobs.items()
                    if job_data.get("type") == "distributed"
                ]
            
            released_count = 0
            for jid in jobs_to_release:
                debug_log(f"Releasing Deadline job: {jid}")
                result = subprocess.run([
                    self.deadline_command,
                    "-DeleteJob",
                    jid
                ], capture_output=True, text=True, timeout=10)
                
                debug_log(f"Delete job result - Return code: {result.returncode}")
                if result.stderr:
                    debug_log(f"Delete job stderr: {result.stderr}")
                
                if result.returncode == 0:
                    released_count += 1
                    self.active_jobs.pop(jid, None)
                    debug_log(f"Successfully released job: {jid}")
                else:
                    debug_log(f"Failed to release job {jid}: {result.stderr}")
            
            return {
                "success": True,
                "released_jobs": released_count,
                "message": f"Released {released_count} worker job(s)"
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}

    def register_worker(self, worker_id: str, worker_ip: str, worker_port: int, job_id: str = None) -> Dict[str, Any]:
        """Register a Deadline worker and add to ComfyUI-Distributed config"""
        try:
            self.claimed_workers[worker_id] = {
                "id": worker_id,
                "ip": worker_ip,
                "port": worker_port,
                "job_id": job_id,
                "last_seen": time.time()
            }
            
            # CRITICAL: Also add worker to ComfyUI-Distributed configuration
            self._add_worker_to_distributed_config(worker_id, worker_ip, worker_port)
            
            debug_log(f"✅ Registered worker: {worker_id} at {worker_ip}:{worker_port}")
            return {"success": True, "message": f"Worker {worker_id} registered and added to distributed config"}
        except Exception as e:
            debug_log(f"Error registering worker: {e}")
            return {"success": False, "error": str(e)}

    def worker_heartbeat(self, worker_id: str) -> Dict[str, Any]:
        """Update worker heartbeat"""
        try:
            if worker_id in self.claimed_workers:
                self.claimed_workers[worker_id]["last_seen"] = time.time()
                return {"success": True}
            else:
                return {"success": False, "error": "Worker not found"}
        except Exception as e:
            debug_log(f"Error updating heartbeat: {e}")
            return {"success": False, "error": str(e)}

    def unregister_worker(self, worker_id: str) -> Dict[str, Any]:
        """Unregister a worker and remove from distributed config"""
        try:
            if worker_id in self.claimed_workers:
                del self.claimed_workers[worker_id]
                
                # Also remove from distributed config
                self._remove_worker_from_distributed_config(worker_id)
                
                debug_log(f"✅ Unregistered worker: {worker_id}")
                return {"success": True, "message": f"Worker {worker_id} unregistered"}
            else:
                return {"success": False, "error": "Worker not found"}
        except Exception as e:
            debug_log(f"Error unregistering worker: {e}")
            return {"success": False, "error": str(e)}

    def get_active_workers(self) -> List[Dict[str, Any]]:
        """Get list of active workers"""
        current_time = time.time()
        active_workers = []
        
        for worker_id, worker_info in list(self.claimed_workers.items()):
            if current_time - worker_info["last_seen"] < self.worker_heartbeat_timeout:
                active_workers.append(worker_info)
            else:
                # Remove stale workers
                del self.claimed_workers[worker_id]
                debug_log(f"Removed stale worker: {worker_id}")
        
        return active_workers

    def _add_worker_to_distributed_config(self, worker_id: str, worker_ip: str, worker_port: int):
        """Add worker to ComfyUI-Distributed configuration"""
        try:
            # Try absolute import first, then relative import as fallback
            try:
                # Method 1: Direct absolute import from our plugin directory
                import importlib.util
                import os
                current_dir = os.path.dirname(__file__)
                config_path = os.path.join(current_dir, 'utils', 'config.py')
                
                spec = importlib.util.spec_from_file_location("utils.config", config_path)
                config_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(config_module)
                
                load_config = config_module.load_config
                save_config = config_module.save_config
                debug_log("✅ Successfully imported config module via importlib")
                
            except Exception as e1:
                debug_log(f"Importlib method failed: {e1}, trying relative import...")
                # Method 2: Try relative import with package context
                from .utils.config import load_config, save_config
                debug_log("✅ Successfully imported config module via relative import")
            
            # Load current distributed config
            config = load_config()
            
            # Ensure workers array exists
            if 'workers' not in config:
                config['workers'] = []
            
            # Check if worker already exists
            existing_worker = None
            for i, worker in enumerate(config['workers']):
                if worker.get('id') == worker_id:
                    existing_worker = i
                    break
            
            # Create worker entry
            worker_config = {
                "id": worker_id,
                "host": worker_ip,
                "port": worker_port,
                "cuda_device": 0,  # Default CUDA device
                "enabled": True,
                "source": "deadline",  # Mark as Deadline-managed
                "name": f"Deadline Worker ({socket.gethostname()})",  # Add display name
                "args": "",  # Empty args for Deadline workers
                "platform": "deadline"  # Platform identifier
            }
            
            if existing_worker is not None:
                # Update existing worker
                config['workers'][existing_worker] = worker_config
                debug_log(f"✅ Updated worker {worker_id} in distributed config")
            else:
                # Add new worker
                config['workers'].append(worker_config)
                debug_log(f"✅ Added worker {worker_id} to distributed config")
            
            # Save config
            save_config(config)
            
        except Exception as e:
            debug_log(f"Warning: Could not add worker to distributed config: {e}")
            # Don't fail registration if config update fails

    def _remove_worker_from_distributed_config(self, worker_id: str):
        """Remove worker from ComfyUI-Distributed configuration"""
        try:
            # Try absolute import first, then relative import as fallback
            try:
                # Method 1: Direct absolute import from our plugin directory
                import importlib.util
                import os
                current_dir = os.path.dirname(__file__)
                config_path = os.path.join(current_dir, 'utils', 'config.py')
                
                spec = importlib.util.spec_from_file_location("utils.config", config_path)
                config_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(config_module)
                
                load_config = config_module.load_config
                save_config = config_module.save_config
                debug_log("✅ Successfully imported config module via importlib (remove)")
                
            except Exception as e1:
                debug_log(f"Importlib method failed: {e1}, trying relative import...")
                # Method 2: Try relative import with package context
                from .utils.config import load_config, save_config
                debug_log("✅ Successfully imported config module via relative import (remove)")
            
            # Load current distributed config
            config = load_config()
            
            if 'workers' not in config:
                return
            
            # Remove worker with matching ID
            original_count = len(config['workers'])
            config['workers'] = [w for w in config['workers'] if w.get('id') != worker_id]
            
            if len(config['workers']) < original_count:
                save_config(config)
                debug_log(f"✅ Removed worker {worker_id} from distributed config")
            
        except Exception as e:
            debug_log(f"Warning: Could not remove worker from distributed config: {e}")

    def _create_worker_job_files(self, job_name: str, count: int, master_ws: str, priority: int = 50, pool: str = "none", group: str = "none") -> tuple:
        """Create Deadline job info and plugin info files for worker submission"""
        # Create temporary directory for submission files
        temp_dir = tempfile.mkdtemp(prefix="comfy_deadline_workers_")
        
        job_info_file = os.path.join(temp_dir, "job_info.txt")
        plugin_info_file = os.path.join(temp_dir, "plugin_info.txt")
        
        # Create job info file
        with open(job_info_file, 'w') as f:
            f.write(f"Plugin=ComfyUI\n")
            f.write(f"Name={job_name}\n")
            f.write(f"Comment=ComfyUI Distributed Workers - Interactive Mode\n")
            f.write(f"Department=ComfyUI\n")
            f.write(f"Priority={priority}\n")
            f.write(f"Pool={pool if pool != 'none' else ''}\n")
            f.write(f"Group={group if group != 'none' else ''}\n")
            f.write(f"TaskTimeoutMinutes=0\n")
            f.write(f"EnableAutoTimeout=false\n")
            f.write(f"ConcurrentTasks=1\n")
            f.write(f"LimitConcurrentTasksToNumberOfCpus=false\n")
            f.write(f"MachineLimit={count}\n")
            f.write(f"Frames=1-{count}\n")  # One task per worker machine
            f.write(f"ChunkSize=1\n")      # One task per machine
            
            # Environment variables for distributed mode
            f.write(f"EnvironmentKeyValue0=DEADLINE_DIST_MODE=1\n")
            f.write(f"EnvironmentKeyValue1=COMFY_MASTER_WS={master_ws}\n")
            f.write(f"EnvironmentKeyValue2=COMFY_MASTER_HOST={master_ws.split(':')[0]}\n")
            f.write(f"EnvironmentKeyValue3=COMFY_MASTER_PORT={master_ws.split(':')[1] if ':' in master_ws else '8188'}\n")
            f.write(f"EnvironmentKeyValue4=COMFY_FORCE_NEW_INSTANCE=1\n")
            f.write(f"EnvironmentKeyValue5=COMFY_WORKER_MODE=1\n")
        
        # Create a worker registration workflow
        dummy_workflow_file = os.path.join(temp_dir, "worker_registration.json")
        with open(dummy_workflow_file, 'w') as f:
            # Create a simple workflow that will just execute and complete
            # The actual worker registration happens via the API in the worker process
            dummy_workflow = {
                "1": {
                    "class_type": "DeadlineWorkerRegistration",
                    "inputs": {},
                    "_meta": {
                        "title": "Deadline Worker Registration"
                    }
                }
            }
            json.dump(dummy_workflow, f, indent=2)
        
        # Create plugin info file
        with open(plugin_info_file, 'w') as f:
            f.write("DefaultCudaDeviceZero=True\n")
            f.write("SeedMode=fixed\n")  # Workers don't need seed changes
            f.write("BatchMode=False\n")  # Not batch rendering
            f.write("DistributedMode=True\n")  # Mark as distributed worker mode
            f.write("WorkerMode=True\n")  # Special flag for worker-only jobs
            f.write("ForceNewInstance=True\n")  # Force new ComfyUI instance
            f.write("UseExistingInstance=False\n")  # Don't reuse existing instance
            f.write(f"WorkflowFile={dummy_workflow_file}\n")  # Provide dummy workflow
            
        debug_log(f"Created worker job files: {job_info_file}, {plugin_info_file}, {dummy_workflow_file}")
        return job_info_file, plugin_info_file, dummy_workflow_file
    
    def _extract_job_id(self, stdout: str) -> str:
        """Extract job ID from Deadline submission output using same logic as original plugin"""
        for line in stdout.split():
            if line.startswith("JobID="):
                job_id = line[6:]  # Remove "JobID=" prefix
                debug_log(f"Extracted job ID: {job_id}")
                return job_id
        
        # Fallback: look for any line containing job ID pattern
        import re
        job_id_match = re.search(r'Job ID[:\s=]+([a-f0-9]{24})', stdout, re.IGNORECASE)
        if job_id_match:
            job_id = job_id_match.group(1)
            debug_log(f"Extracted job ID via regex: {job_id}")
            return job_id
            
        debug_log(f"Could not extract job ID from output: {stdout}")
        return ""

# Create global instance
deadline_integration = DeadlineIntegration()

def register_api_endpoints():
    """Register API endpoints if server is available"""
    try:
        import server
        from aiohttp import web
        
        # Check if server is ready
        if not hasattr(server, 'PromptServer') or not server.PromptServer.instance:
            debug_log("❌ PromptServer not ready yet")
            return False
        
        # Define endpoint functions
        async def deadline_status_endpoint(request):
            status = await deadline_integration.get_worker_status()
            return web.json_response(status)

        async def deadline_claim_workers_endpoint(request):
            data = await request.json()
            count = data.get('count', 1)
            master_ws = data.get('master_ws', 'localhost:8188')
            priority = data.get('priority', 50)
            pool = data.get('pool', 'none')
            group = data.get('group', 'none')
            result = await deadline_integration.claim_workers(count, master_ws, priority, pool, group)
            return web.json_response(result)

        async def deadline_release_workers_endpoint(request):
            data = await request.json()
            job_ids = data.get('job_ids', [])
            result = await deadline_integration.release_workers(job_ids)
            return web.json_response(result)

        async def deadline_register_worker_endpoint(request):
            data = await request.json()
            worker_id = data.get('worker_id')
            worker_ip = data.get('worker_ip')
            worker_port = data.get('worker_port')
            job_id = data.get('job_id')
            result = deadline_integration.register_worker(worker_id, worker_ip, worker_port, job_id)
            return web.json_response(result)

        async def deadline_worker_heartbeat_endpoint(request):
            data = await request.json()
            worker_id = data.get('worker_id')
            result = deadline_integration.worker_heartbeat(worker_id)
            return web.json_response(result)

        async def deadline_unregister_worker_endpoint(request):
            data = await request.json()
            worker_id = data.get('worker_id')
            result = deadline_integration.unregister_worker(worker_id)
            return web.json_response(result)

        # Register endpoints
        server.PromptServer.instance.routes.get("/deadline/status")(deadline_status_endpoint)
        server.PromptServer.instance.routes.post("/deadline/claim_workers")(deadline_claim_workers_endpoint)
        server.PromptServer.instance.routes.post("/deadline/release_workers")(deadline_release_workers_endpoint)
        server.PromptServer.instance.routes.post("/deadline/register_worker")(deadline_register_worker_endpoint)
        server.PromptServer.instance.routes.post("/deadline/worker_heartbeat")(deadline_worker_heartbeat_endpoint)
        server.PromptServer.instance.routes.post("/deadline/unregister_worker")(deadline_unregister_worker_endpoint)
        
        debug_log("✅ Deadline integration API endpoints registered")
        return True
        
    except ImportError:
        debug_log("❌ Server modules not available - API endpoints not registered")
        return False
    except Exception as e:
        debug_log(f"❌ Error registering API endpoints: {e}")
        return False

# Try to register endpoints immediately
register_api_endpoints()

debug_log("✅ Deadline integration simple module loaded")