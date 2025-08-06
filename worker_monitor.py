#!/usr/bin/env python3
"""
Worker process monitor - monitors if the master process is still alive
and terminates the worker if the master dies.
"""
import os
import sys
import time
import subprocess
import platform
import signal

# Add parent directory to path to import utils
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from ComfyUI_Distributed.utils.process import is_process_alive, terminate_process
    from ComfyUI_Distributed.utils.constants import WORKER_CHECK_INTERVAL, PROCESS_TERMINATION_TIMEOUT
except ImportError:
    # Fallback if running from different context
    def is_process_alive(pid):
        """Check if a process with given PID is still alive."""
        try:
            if platform.system() == "Windows":
                # Windows: use tasklist
                result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], 
                                      capture_output=True, text=True)
                return str(pid) in result.stdout
            else:
                # Unix: send signal 0
                os.kill(pid, 0)
                return True
        except (OSError, subprocess.SubprocessError):
            return False
    
    WORKER_CHECK_INTERVAL = 2.0
    PROCESS_TERMINATION_TIMEOUT = 5.0

def monitor_and_run(master_pid, command):
    """Run command and monitor master process."""
    # Start the actual worker process
    worker_process = subprocess.Popen(command)
    
    print(f"[Distributed] Started worker PID: {worker_process.pid}")
    print(f"[Distributed] Monitoring master PID: {master_pid}")
    
    # Write worker PID to a file so parent can track it
    monitor_pid = os.getpid()
    pid_info_file = os.environ.get('WORKER_PID_FILE')
    if pid_info_file:
        try:
            with open(pid_info_file, 'w') as f:
                f.write(f"{monitor_pid},{worker_process.pid}")
            print(f"[Distributed] Wrote PID info to {pid_info_file}")
        except Exception as e:
            print(f"[Distributed] Could not write PID file: {e}")
    
    # Define cleanup function
    def cleanup_worker(signum=None, frame=None):
        """Clean up worker process when monitor is terminated."""
        if signum:
            print(f"\n[Distributed] Received signal {signum}, terminating worker...")
        else:
            print("\n[Distributed] Terminating worker...")
        
        if worker_process.poll() is None:  # Still running
            try:
                terminate_process(worker_process, timeout=PROCESS_TERMINATION_TIMEOUT)
            except NameError:
                # Fallback if terminate_process wasn't imported
                worker_process.terminate()
                try:
                    worker_process.wait(timeout=PROCESS_TERMINATION_TIMEOUT)
                except subprocess.TimeoutExpired:
                    print("[Distributed] Worker didn't terminate gracefully, forcing kill...")
                    worker_process.kill()
                    worker_process.wait()
        
        print("[Distributed] Worker terminated.")
        sys.exit(0)
    
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, cleanup_worker)
    signal.signal(signal.SIGINT, cleanup_worker)
    if platform.system() != "Windows":
        signal.signal(signal.SIGHUP, cleanup_worker)
    
    # Monitor loop
    check_interval = WORKER_CHECK_INTERVAL
    
    try:
        while True:
            # Check if worker is still running
            if worker_process.poll() is not None:
                print(f"[Distributed] Worker process exited with code: {worker_process.returncode}")
                sys.exit(worker_process.returncode)
            
            # Check if master is still alive
            if not is_process_alive(master_pid):
                print(f"[Distributed] Master process {master_pid} is no longer running. Terminating worker...")
                cleanup_worker()
            
            time.sleep(check_interval)
            
    except KeyboardInterrupt:
        cleanup_worker()

if __name__ == "__main__":
    # Get master PID from environment
    master_pid = os.environ.get('COMFYUI_MASTER_PID')
    if not master_pid:
        print("[Distributed] Error: COMFYUI_MASTER_PID not set")
        sys.exit(1)
    
    try:
        master_pid = int(master_pid)
    except ValueError:
        print(f"[Distributed] Error: Invalid master PID: {master_pid}")
        sys.exit(1)
    
    # Get the actual command to run (all remaining arguments)
    if len(sys.argv) < 2:
        print("[Distributed] Error: No command specified")
        sys.exit(1)
    
    command = sys.argv[1:]
    
    # Start monitoring
    monitor_and_run(master_pid, command)