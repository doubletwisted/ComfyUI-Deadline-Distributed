"""
Async helper utilities for ComfyUI-Distributed.
"""
import asyncio
import concurrent.futures
from typing import Optional, Any, Coroutine
from .network import get_server_loop

def run_async_in_server_loop(coro: Coroutine, timeout: Optional[float] = None) -> Any:
    """
    Run async coroutine in server's event loop and wait for result.
    
    This is useful when you need to run async code from a synchronous context
    but want to use the server's existing event loop instead of creating a new one.
    
    Args:
        coro: The coroutine to run
        timeout: Optional timeout in seconds
        
    Returns:
        The result of the coroutine
        
    Raises:
        TimeoutError: If the operation times out
        Exception: Any exception raised by the coroutine
    """
    loop = get_server_loop()
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop is loop:
        coro.close()
        raise RuntimeError("Cannot synchronously wait for the server event loop from the server event loop thread")

    future = asyncio.run_coroutine_threadsafe(coro, loop)

    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError(f"Async operation timed out after {timeout} seconds")
