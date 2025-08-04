"""
Deadline Integration Module for ComfyUI-Deadline-Distributed

This module provides Deadline render farm integration on top of ComfyUI-Distributed functionality.
It allows users to:
1. Submit workflows to Deadline for batch rendering
2. Claim Deadline workers for interactive distributed processing
3. Manage Deadline worker pools alongside local distributed workers

Key Features:
- Deadline job submission with environment variable injection
- Interactive worker claiming/releasing
- Integration with existing ComfyUI-Distributed UI
- Support for both batch and distributed modes
"""

import subprocess
import json
import os
import asyncio
import logging
from typing import Dict, List, Optional, Any
import server
from aiohttp import web

# Import ComfyUI-Distributed utilities
from .utils.logging import debug_log, log
from .utils.config import load_config, save_config
from .utils.network import handle_api_error

class DeadlineIntegration:
    """Main class for Deadline render farm integration"""
    
    def __init__(self):
        self.deadline_command = self._find_deadline_command()
        self.claimed_workers = {}  # Track claimed Deadline workers
        self.active_jobs = {}      # Track active Deadline jobs
        
    def _find_deadline_command(self) -> Optional[str]:
        """Find deadlinecommand executable"""
        # Common Deadline installation paths
        possible_paths = [
            r"C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe",
            r"C:\Program Files\Thinkbox\Deadline\bin\deadlinecommand.exe",
            "deadlinecommand",  # If in PATH
        ]
        
        for path in possible_paths:
            try:
                result = subprocess.run([path, "-help"], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    debug_log(f"Found Deadline command: {path}")
                    return path
            except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
                continue
                
        log("Deadline command not found. Please ensure Deadline is installed.")
        return None
    
    def is_available(self) -> bool:
        """Check if Deadline integration is available"""
        return self.deadline_command is not None
    
    async def get_worker_status(self) -> Dict[str, Any]:
        """Get current Deadline worker status"""
        if not self.deadline_command:
            return {"available": False, "workers": [], "error": "Deadline not available"}
            
        try:
            # Get Deadline worker information
            result = subprocess.run([
                self.deadline_command, 
                "-GetSlaves", 
                "-json"
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                workers_data = json.loads(result.stdout)
                
                # Filter for available workers
                available_workers = [
                    worker for worker in workers_data 
                    if worker.get("State", "").lower() == "idle"
                ]
                
                return {
                    "available": True,
                    "total_workers": len(workers_data),
                    "available_workers": len(available_workers),
                    "claimed_workers": len(self.claimed_workers),
                    "workers": available_workers[:10]  # Limit for UI
                }
            else:
                return {"available": False, "error": f"Deadline error: {result.stderr}"}
                
        except Exception as e:
            return {"available": False, "error": str(e)}
    
    async def claim_workers(self, count: int = 1, master_ws: str = "localhost:8188") -> Dict[str, Any]:
        """Claim Deadline workers for distributed processing"""
        if not self.deadline_command:
            return {"success": False, "error": "Deadline not available"}
            
        try:
            # Submit a distributed worker job to Deadline
            job_name = f"[DIST] ComfyUI Workers x{count}"
            
            # Create job submission file
            job_info = self._create_worker_job_info(job_name, count, master_ws)
            
            # Submit to Deadline
            result = subprocess.run([
                self.deadline_command,
                "-SubmitJob",
                job_info
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                job_id = self._extract_job_id(result.stdout)
                self.active_jobs[job_id] = {
                    "type": "distributed",
                    "count": count,
                    "master_ws": master_ws,
                    "submitted_at": asyncio.get_event_loop().time()
                }
                
                return {
                    "success": True,
                    "job_id": job_id,
                    "message": f"Claimed {count} workers for distributed processing"
                }
            else:
                return {"success": False, "error": f"Deadline submission failed: {result.stderr}"}
                
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def release_workers(self, job_id: Optional[str] = None) -> Dict[str, Any]:
        """Release claimed Deadline workers"""
        if not self.deadline_command:
            return {"success": False, "error": "Deadline not available"}
            
        try:
            if job_id:
                # Release specific job
                jobs_to_release = [job_id] if job_id in self.active_jobs else []
            else:
                # Release all distributed jobs
                jobs_to_release = [
                    jid for jid, job_data in self.active_jobs.items()
                    if job_data.get("type") == "distributed"
                ]
            
            released_count = 0
            for jid in jobs_to_release:
                result = subprocess.run([
                    self.deadline_command,
                    "-DeleteJob",
                    jid
                ], capture_output=True, text=True, timeout=10)
                
                if result.returncode == 0:
                    released_count += 1
                    self.active_jobs.pop(jid, None)
            
            return {
                "success": True,
                "released_jobs": released_count,
                "message": f"Released {released_count} worker jobs"
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _create_worker_job_info(self, job_name: str, count: int, master_ws: str) -> str:
        """Create Deadline job info file for worker submission"""
        import tempfile
        
        # Create temporary job info file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.job', delete=False) as f:
            f.write(f"Plugin=ComfyUI\n")
            f.write(f"Name={job_name}\n")
            f.write(f"Comment=ComfyUI Distributed Workers\n")
            f.write(f"Department=ComfyUI\n")
            f.write(f"Priority=50\n")
            f.write(f"TaskTimeoutMinutes=0\n")
            f.write(f"EnableAutoTimeout=false\n")
            f.write(f"ConcurrentTasks=1\n")
            f.write(f"LimitConcurrentTasksToNumberOfCpus=false\n")
            f.write(f"MachineLimit={count}\n")
            f.write(f"Frames=1\n")
            f.write(f"ChunkSize=1\n")
            
            # Environment variables for distributed mode
            f.write(f"EnvironmentKeyValue0=DEADLINE_DIST_MODE=1\n")
            f.write(f"EnvironmentKeyValue1=COMFY_MASTER_WS={master_ws}\n")
            
            return f.name
    
    def _extract_job_id(self, stdout: str) -> str:
        """Extract job ID from Deadline submission output"""
        # Parse Deadline output to extract job ID
        lines = stdout.strip().split('\n')
        for line in lines:
            if 'JobID=' in line:
                return line.split('JobID=')[1].strip()
        return "unknown"

# Global instance
deadline_integration = DeadlineIntegration()

# --- API Endpoints ---

@server.PromptServer.instance.routes.get("/deadline/status")
async def deadline_status_endpoint(request):
    """Get Deadline worker status"""
    try:
        status = await deadline_integration.get_worker_status()
        return web.json_response(status)
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/deadline/claim_workers")
async def deadline_claim_workers_endpoint(request):
    """Claim Deadline workers for distributed processing"""
    try:
        data = await request.json()
        count = data.get('count', 1)
        master_ws = data.get('master_ws', 'localhost:8188')
        
        result = await deadline_integration.claim_workers(count, master_ws)
        return web.json_response(result)
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/deadline/release_workers")
async def deadline_release_workers_endpoint(request):
    """Release claimed Deadline workers"""
    try:
        data = await request.json()
        job_id = data.get('job_id')
        
        result = await deadline_integration.release_workers(job_id)
        return web.json_response(result)
    except Exception as e:
        return await handle_api_error(request, e, 500)

debug_log("Deadline integration API endpoints registered")