# Import statements (original + new)
import torch
import numpy as np
from PIL import Image, ImageFilter, ImageDraw
import json
import asyncio
import aiohttp
from aiohttp import web
import io
import server
import math
import os
import time
from typing import List, Tuple
from functools import wraps

# Import ComfyUI modules
import comfy.samplers
import comfy.model_management

# Import shared utilities
from .utils.logging import debug_log, log
from .utils.config import CONFIG_FILE
from .utils.image import tensor_to_pil, pil_to_tensor
from .utils.network import get_server_port, get_server_loop, get_client_session, handle_api_error
from .utils.async_helpers import run_async_in_server_loop
from .utils.constants import (
    TILE_COLLECTION_TIMEOUT, TILE_WAIT_TIMEOUT, TILE_TRANSFER_TIMEOUT,
    QUEUE_INIT_TIMEOUT, TILE_SEND_TIMEOUT, MAX_BATCH, HEARTBEAT_TIMEOUT
)

# Import for controller support
from .utils.usdu_utils import crop_cond
from .utils.usdu_managment import (
    clone_conditioning, ensure_tile_jobs_initialized,
    # Job management functions
    _init_job_queue, _distribute_tasks, _get_next_task, _drain_results_queue,
    _check_and_requeue_timed_out_workers, _get_completed_count, _mark_task_completed,
    _send_heartbeat_to_master, _cleanup_job,
    # Constants
    JOB_COMPLETED_TASKS, JOB_WORKER_STATUS, JOB_PENDING_TASKS
)


# Note: MAX_BATCH and HEARTBEAT_TIMEOUT are imported from utils.constants
# They can be overridden via environment variables:
# - COMFYUI_MAX_BATCH (default: 20)
# - COMFYUI_HEARTBEAT_TIMEOUT (default: 120)

# Sync wrapper decorator for async methods
def sync_wrapper(async_func):
    """Decorator to wrap async methods for synchronous execution."""
    @wraps(async_func)
    def sync_func(self, *args, **kwargs):
        # Use run_async_in_server_loop for ComfyUI compatibility
        return run_async_in_server_loop(
            async_func(self, *args, **kwargs),
            timeout=600.0  # 10 minute timeout for long operations
        )
    return sync_func

# Note: tensor_to_pil and pil_to_tensor are imported from utils.image

class UltimateSDUpscaleDistributed:
    """
    Distributed version of Ultimate SD Upscale (No Upscale).
    
    Supports three processing modes:
    1. Single GPU: No workers available, process everything locally
    2. Static Mode: Small batches, distributes tiles across workers (flattened)
    3. Dynamic Mode: Large batches, assigns whole images to workers dynamically
    
    Features:
    - Multi-mode batch handling for efficient video/image upscaling
    - Tiled VAE support for memory efficiency
    - Dynamic load balancing for large batches
    - Backward compatible with single-image workflows
    
    Environment Variables:
    - COMFYUI_MAX_BATCH: Chunk size for tile sending (default 20)
    - COMFYUI_MAX_PAYLOAD_SIZE: Max API payload bytes (default 50MB)
    
    Threshold: dynamic_threshold input controls mode switch (default 8)
    """
    
    def __init__(self):
        """Initialize the node and ensure persistent storage exists."""
        # Pre-initialize the persistent storage on node creation
        ensure_tile_jobs_initialized()
        debug_log("UltimateSDUpscaleDistributed - Node initialized")
    
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "upscaled_image": ("IMAGE",),
                "model": ("MODEL",),
                "positive": ("CONDITIONING",),
                "negative": ("CONDITIONING",),
                "vae": ("VAE",),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS,),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS,),
                "denoise": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}),
                "tile_width": ("INT", {"default": 512, "min": 64, "max": 2048, "step": 8}),
                "tile_height": ("INT", {"default": 512, "min": 64, "max": 2048, "step": 8}),
                "padding": ("INT", {"default": 32, "min": 0, "max": 256, "step": 8}),
                "mask_blur": ("INT", {"default": 8, "min": 0, "max": 256}),
                "force_uniform_tiles": ("BOOLEAN", {"default": True}),
                "tiled_decode": ("BOOLEAN", {"default": False}),
                "static_distribution": ("BOOLEAN", {"default": False, "label": "Use Static Distribution (legacy)"}),
            },
            "hidden": {
                "multi_job_id": ("STRING", {"default": ""}),
                "is_worker": ("BOOLEAN", {"default": False}),
                "master_url": ("STRING", {"default": ""}),
                "enabled_worker_ids": ("STRING", {"default": "[]"}),
                "worker_id": ("STRING", {"default": ""}),
                "tile_indices": ("STRING", {"default": ""}),  # Unused - kept for compatibility
                "dynamic_threshold": ("INT", {"default": 8, "min": 1, "max": 64}),
            },
        }
    
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "image/upscaling"
    
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        """Force re-execution."""
        return float("nan")  # Always re-execute
    
    
    def run(self, upscaled_image, model, positive, negative, vae, seed, steps, cfg, 
            sampler_name, scheduler, denoise, tile_width, tile_height, padding, 
            mask_blur, force_uniform_tiles, tiled_decode, static_distribution=False,
            multi_job_id="", is_worker=False, master_url="", enabled_worker_ids="[]", 
            worker_id="", tile_indices="", dynamic_threshold=8):
        """Entry point - runs SYNCHRONOUSLY like Ultimate SD Upscaler."""
        if not multi_job_id:
            # No distributed processing, run single GPU version
            return self.process_single_gpu(upscaled_image, model, positive, negative, vae,
                                          seed, steps, cfg, sampler_name, scheduler, denoise,
                                          tile_width, tile_height, padding, mask_blur, force_uniform_tiles, tiled_decode)
        
        if is_worker:
            # Worker mode: process tiles synchronously
            return self.process_worker(upscaled_image, model, positive, negative, vae,
                                      seed, steps, cfg, sampler_name, scheduler, denoise,
                                      tile_width, tile_height, padding, mask_blur,
                                      force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                      worker_id, enabled_worker_ids, dynamic_threshold, static_distribution)
        else:
            # Master mode: distribute and collect synchronously
            return self.process_master(upscaled_image, model, positive, negative, vae,
                                     seed, steps, cfg, sampler_name, scheduler, denoise,
                                     tile_width, tile_height, padding, mask_blur,
                                     force_uniform_tiles, tiled_decode, multi_job_id, enabled_worker_ids, 
                                     dynamic_threshold, static_distribution)
    
    def process_worker(self, upscaled_image, model, positive, negative, vae,
                      seed, steps, cfg, sampler_name, scheduler, denoise,
                      tile_width, tile_height, padding, mask_blur,
                      force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                      worker_id, enabled_worker_ids, dynamic_threshold, static_distribution=False):
        """Unified worker processing - handles both static and dynamic modes."""
        # Get batch size to determine mode
        batch_size = upscaled_image.shape[0]
        
        # Ensure mode consistency across master/workers via shared threshold
        # Determine mode (must match master's logic)
        enabled_workers = json.loads(enabled_worker_ids)
        num_workers = len(enabled_workers)
        
        mode = self._determine_processing_mode(batch_size, num_workers, dynamic_threshold)
            
        debug_log(f"UltimateSDUpscale Worker - Mode: {mode}, batch_size: {batch_size}")
        
        if mode == "dynamic":
            return self.process_worker_dynamic(upscaled_image, model, positive, negative, vae,
                                             seed, steps, cfg, sampler_name, scheduler, denoise,
                                             tile_width, tile_height, padding, mask_blur,
                                             force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                             worker_id, enabled_worker_ids, dynamic_threshold)
        
        # Static mode - enhanced with health monitoring and retry logic
        return self._process_worker_static_sync(upscaled_image, model, positive, negative, vae,
                                               seed, steps, cfg, sampler_name, scheduler, denoise,
                                               tile_width, tile_height, padding, mask_blur,
                                               force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                               worker_id, enabled_workers, static_distribution)
    
    def process_master(self, upscaled_image, model, positive, negative, vae,
                      seed, steps, cfg, sampler_name, scheduler, denoise,
                      tile_width, tile_height, padding, mask_blur,
                      force_uniform_tiles, tiled_decode, multi_job_id, enabled_worker_ids, 
                      dynamic_threshold, static_distribution=False):
        """Unified master processing with enhanced monitoring and failure handling."""
        # Round tile dimensions
        tile_width = self.round_to_multiple(tile_width)
        tile_height = self.round_to_multiple(tile_height)
        
        # Get image dimensions and batch size
        batch_size, height, width, _ = upscaled_image.shape
        
        # Calculate all tiles
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
        num_tiles_per_image = len(all_tiles)
        
        debug_log(f"UltimateSDUpscale Master - Tiles per image: {num_tiles_per_image}")
        
        # Parse enabled workers
        enabled_workers = json.loads(enabled_worker_ids)
        num_workers = len(enabled_workers)
        
        # Determine processing mode
        mode = self._determine_processing_mode(batch_size, num_workers, dynamic_threshold)
        
        debug_log(f"UltimateSDUpscale Master - Mode: {mode}, batch_size: {batch_size}, workers: {num_workers}")
        
        if mode == "single_gpu":
            # No workers, process all tiles locally
            return self.process_single_gpu(upscaled_image, model, positive, negative, vae,
                                         seed, steps, cfg, sampler_name, scheduler, denoise,
                                         tile_width, tile_height, padding, mask_blur, force_uniform_tiles, tiled_decode)
        
        elif mode == "dynamic":
            # Dynamic mode for large batches
            return self.process_master_dynamic(upscaled_image, model, positive, negative, vae,
                                             seed, steps, cfg, sampler_name, scheduler, denoise,
                                             tile_width, tile_height, padding, mask_blur,
                                             force_uniform_tiles, tiled_decode, multi_job_id, enabled_workers)
        
        # Static mode - enhanced with unified job management
        return self._process_master_static_sync(upscaled_image, model, positive, negative, vae,
                                               seed, steps, cfg, sampler_name, scheduler, denoise,
                                               tile_width, tile_height, padding, mask_blur,
                                               force_uniform_tiles, tiled_decode, multi_job_id, enabled_workers,
                                               all_tiles, num_tiles_per_image, static_distribution)
    
    def _get_worker_global_indices(self, total_tiles, enabled_workers, worker_id):
        """Calculate which global tile indices are assigned to a specific worker in flattened mode."""
        num_workers = len(enabled_workers)
        
        debug_log(f"UltimateSDUpscale Worker - Worker ID: {worker_id}, Enabled workers: {enabled_workers}")
        
        try:
            worker_index = enabled_workers.index(worker_id)
        except ValueError:
            log(f"UltimateSDUpscale Worker - Worker {worker_id} not found in enabled list {enabled_workers}")
            return []
        
        global_indices = list(range(total_tiles))
        all_assignments = _distribute_tasks(global_indices, num_workers + 1)
        return all_assignments[worker_index + 1]  # +1 because 0 is master
    
    def _get_master_global_indices(self, total_tiles, num_workers):
        """Calculate which global tile indices are assigned to the master in flattened mode."""
        global_indices = list(range(total_tiles))
        all_assignments = _distribute_tasks(global_indices, num_workers + 1)
        return all_assignments[0]
    
    def _process_and_blend_tile(self, tile_idx, tile_pos, upscaled_image, result_image,
                               model, positive, negative, vae, seed, steps, cfg,
                               sampler_name, scheduler, denoise, tile_width, tile_height,
                               padding, mask_blur, image_width, image_height, tiled_decode, batch_idx: int = 0):
        """Process a single tile and blend it into the result image."""
        x, y = tile_pos
        
        # Extract and process tile
        tile_tensor, x1, y1, ew, eh = self.extract_tile_with_padding(
            upscaled_image, x, y, tile_width, tile_height, padding
        )
        
        processed_tile = self.process_tile(tile_tensor, model, positive, negative, vae,
                                         seed + tile_idx, steps, cfg, sampler_name, 
                                         scheduler, denoise, tiled_decode, batch_idx=batch_idx,
                                         region=(x1, y1, x1 + ew, y1 + eh), image_size=(image_width, image_height))
        
        # Convert and blend
        processed_pil = tensor_to_pil(processed_tile, 0)
        # Create mask for this specific tile
        tile_mask = self.create_tile_mask(image_width, image_height, x, y, tile_width, tile_height, mask_blur)
        # Use extraction position and size for blending
        result_image = self.blend_tile(result_image, processed_pil, 
                                     x1, y1, (ew, eh), tile_mask, padding)
        
        return result_image
    
    async def _init_job_queue_unified(self, multi_job_id, mode='static', batch_size=None, 
                                     total_tiles=None, enabled_workers=None, num_tiles_per_image=None,
                                     assigned_to_workers=None, all_indices=None, task_assignments=None):
        """Unified job queue initialization for both static and dynamic modes."""
        if mode == 'dynamic':
            # Dynamic mode initialization
            await _init_job_queue(multi_job_id, 'dynamic', batch_size=batch_size, 
                                 all_indices=all_indices, 
                                 enabled_workers=list(assigned_to_workers.keys()) if assigned_to_workers else [])
            # Add additional fields for backward compatibility
            prompt_server = ensure_tile_jobs_initialized()
            async with prompt_server.distributed_tile_jobs_lock:
                job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                job_data['completed_images'] = {}
                job_data['completed_tiles'] = {}  # For uniformity
                # Fix: The endpoint expects 'pending_images', not 'pending_tasks'
                job_data['pending_images'] = job_data[JOB_PENDING_TASKS]
            debug_log(f"UltimateSDUpscale Master Dynamic - Initialized job queue with {batch_size} pending images for all participants")
        else:
            # Static mode initialization
            # If num_tiles_per_image not provided, calculate it
            if num_tiles_per_image is None and total_tiles is not None and batch_size is not None:
                num_tiles_per_image = total_tiles // batch_size if batch_size > 0 else total_tiles
            
            await _init_job_queue(multi_job_id, 'static', 
                                 batch_size=batch_size, 
                                 num_tiles_per_image=num_tiles_per_image,
                                 all_indices=None,
                                 enabled_workers=enabled_workers,
                                 task_assignments=task_assignments)
            
            # Add completed_tiles and total_items for backward compatibility
            prompt_server = ensure_tile_jobs_initialized()
            async with prompt_server.distributed_tile_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                    job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                    job_data['completed_tiles'] = {}
                    if total_tiles is not None:
                        job_data['total_items'] = total_tiles
                    debug_log(f"UltimateSDUpscale Master - Initialized job queue for {multi_job_id}")
    
    # Keep compatibility wrappers
    async def _init_job_queue_dynamic(self, multi_job_id, batch_size, assigned_to_workers=None, worker_status=None, all_indices=None):
        """Initialize the job queue for dynamic mode with pending images."""
        await self._init_job_queue_unified(multi_job_id, mode='dynamic', batch_size=batch_size,
                                         assigned_to_workers=assigned_to_workers, all_indices=all_indices)
    
    async def _init_job_queue(self, multi_job_id, total_tiles, enabled_workers, batch_size=1, num_tiles_per_image=None, task_assignments=None):
        """Initialize the job queue for collecting tiles."""
        await self._init_job_queue_unified(multi_job_id, mode='static', batch_size=batch_size,
                                         total_tiles=total_tiles, enabled_workers=enabled_workers,
                                         num_tiles_per_image=num_tiles_per_image, task_assignments=task_assignments)
    
    async def _async_collect_results(self, multi_job_id, num_workers, mode='static', 
                                   remaining_to_collect=None, batch_size=None):
        """Unified async helper to collect results from workers (tiles or images)."""
        # Get the already initialized queue
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id not in prompt_server.distributed_pending_tile_jobs:
                raise RuntimeError(f"Job queue not initialized for {multi_job_id}")
            job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
            if not isinstance(job_data, dict) or 'mode' not in job_data:
                raise RuntimeError("Invalid job data structure")
            if job_data['mode'] != mode:
                raise RuntimeError(f"Mode mismatch: expected {mode}, got {job_data['mode']}")
            q = job_data['queue']
            
            # For dynamic mode, get reference to completed_images
            if mode == 'dynamic':
                completed_images = job_data['completed_images']
                # Calculate expected count for logging
                expected_count = remaining_to_collect or batch_size
            else:
                # Calculate expected count from job data for static mode
                expected_count = job_data.get('total_items', 0) - len(job_data.get('completed_items', set()))
        
        mode_str = "Dynamic" if mode == 'dynamic' else "Static"
        item_type = "images" if mode == 'dynamic' else "tiles"
        debug_log(f"UltimateSDUpscale Master {mode_str} - Starting collection, expecting {expected_count} {item_type} from {num_workers} workers")
        
        collected_results = {}
        workers_done = set()
        timeout = 120.0 if mode == 'dynamic' else 60.0  # Longer timeout for full images
        last_heartbeat_check = time.time()
        collected_count = 0
        
        while len(workers_done) < num_workers:
            # Check for user interruption
            if comfy.model_management.processing_interrupted():
                log("Processing interrupted by user")
                raise comfy.model_management.InterruptProcessingException()
                
            # For dynamic mode with remaining_to_collect, check if we've collected enough
            if mode == 'dynamic' and remaining_to_collect and collected_count >= remaining_to_collect:
                break
                
            try:
                # Shorter timeout for regular checks in dynamic mode
                wait_timeout = 10.0 if mode == 'dynamic' else timeout
                result = await asyncio.wait_for(q.get(), timeout=wait_timeout)
                worker_id = result['worker_id']
                is_last = result.get('is_last', False)
                
                if mode == 'static':
                    # Handle tiles
                    tiles = result.get('tiles', [])
                    if tiles:
                        # Batch mode
                        debug_log(f"UltimateSDUpscale Master - Received batch of {len(tiles)} tiles from worker '{worker_id}' (is_last={is_last})")
                        
                        for tile_data in tiles:
                            # Validate required fields
                            if 'batch_idx' not in tile_data:
                                log(f"UltimateSDUpscale Master - Missing batch_idx in tile data, skipping")
                                continue
                            
                            tile_idx = tile_data['tile_idx']
                            # Use global_idx as key if available (for batch processing)
                            key = tile_data.get('global_idx', tile_idx)
                            
                            # Store the full tile data including metadata
                            collected_results[key] = {
                                'tensor': tile_data['tensor'],
                                'tile_idx': tile_idx,
                                'x': tile_data['x'],
                                'y': tile_data['y'],
                                'extracted_width': tile_data['extracted_width'],
                                'extracted_height': tile_data['extracted_height'],
                                'padding': tile_data['padding'],
                                'worker_id': worker_id,
                                'batch_idx': tile_data.get('batch_idx', 0),
                                'global_idx': tile_data.get('global_idx', tile_idx)
                            }
                    else:
                        # Single tile mode (backward compat)
                        tile_idx = result['tile_idx']
                        collected_results[tile_idx] = result
                        debug_log(f"UltimateSDUpscale Master - Received single tile {tile_idx} from worker '{worker_id}' (is_last={is_last})")
                
                elif mode == 'dynamic':
                    # Handle full images
                    if 'image_idx' in result and 'image' in result:
                        image_idx = result['image_idx']
                        image_pil = result['image']
                        completed_images[image_idx] = image_pil
                        collected_results[image_idx] = image_pil
                        collected_count += 1
                        debug_log(f"UltimateSDUpscale Master Dynamic - Received image {image_idx} from worker {worker_id}")
                
                if is_last:
                    workers_done.add(worker_id)
                    debug_log(f"UltimateSDUpscale Master {mode_str} - Worker {worker_id} completed")
                    
            except asyncio.TimeoutError:
                if mode == 'dynamic':
                    # Check for worker timeouts periodically
                    current_time = time.time()
                    if current_time - last_heartbeat_check >= 10.0:
                        # Use the class method to check and requeue
                        requeued = await self._check_and_requeue_timed_out_workers(multi_job_id, batch_size)
                        if requeued > 0:
                            log(f"UltimateSDUpscale Master Dynamic - Requeued {requeued} images from timed out workers")
                        last_heartbeat_check = current_time
                    
                    # Check if we've been waiting too long overall
                    if current_time - last_heartbeat_check > timeout:
                        log(f"UltimateSDUpscale Master Dynamic - Overall timeout waiting for images")
                        break
                else:
                    log(f"UltimateSDUpscale Master - Timeout waiting for {item_type}")
                    break
        
        debug_log(f"UltimateSDUpscale Master {mode_str} - Collection complete. Got {len(collected_results)} {item_type} from {len(workers_done)} workers")
        
        # Clean up job queue
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                del prompt_server.distributed_pending_tile_jobs[multi_job_id]
        
        return collected_results if mode == 'static' else completed_images
    
    # Keep compatibility wrappers for existing code
    async def _async_collect_worker_tiles(self, multi_job_id, num_workers):
        """Async helper to collect tiles from workers."""
        return await self._async_collect_results(multi_job_id, num_workers, mode='static')
    
    async def _mark_image_completed(self, multi_job_id, image_idx, image_pil):
        """Mark an image as completed in the job data."""
        # Mark the image as completed with the image data
        await _mark_task_completed(multi_job_id, image_idx, {'image': image_pil})
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and 'completed_images' in job_data:
                job_data['completed_images'][image_idx] = image_pil

    async def _async_collect_dynamic_images(self, multi_job_id, remaining_to_collect, num_workers, batch_size, master_processed_count):
        """Collect remaining processed images from workers."""
        return await self._async_collect_results(multi_job_id, num_workers, mode='dynamic', 
                                               remaining_to_collect=remaining_to_collect, 
                                               batch_size=batch_size)
    
    def round_to_multiple(self, value: int, multiple: int = 8) -> int:
        """Round value to nearest multiple."""
        return round(value / multiple) * multiple
    
    def calculate_tiles(self, image_width: int, image_height: int, 
                       tile_width: int, tile_height: int, force_uniform_tiles: bool = True) -> List[Tuple[int, int]]:
        """Calculate tile positions for the image.
        
        Tiles are placed at grid positions without overlap in their placement.
        The overlap happens during extraction with padding.
        
        If force_uniform_tiles is True, tiles are distributed evenly across the image."""
        if force_uniform_tiles:
            # Calculate number of tiles needed
            num_tiles_x = math.ceil(image_width / tile_width)
            num_tiles_y = math.ceil(image_height / tile_height)
            
            # Calculate uniform spacing
            tiles = []
            
            # Handle X dimension
            if num_tiles_x == 1:
                tiles_x = [0]
            else:
                # Calculate step to distribute tiles evenly
                step_x = (image_width - tile_width) / (num_tiles_x - 1)
                # Check for excessive overlap
                if step_x < tile_width / 2:
                    debug_log(f"Warning: Uniform tiles would have excessive overlap (step_x={step_x}, tile_width={tile_width}). Falling back to non-uniform mode.")
                    force_uniform_tiles = False
                else:
                    tiles_x = [round(i * step_x) for i in range(num_tiles_x)]
                    # Ensure tiles are aligned to 8-pixel boundaries
                    tiles_x = [self.round_to_multiple(x, 8) for x in tiles_x]
            
            # Handle Y dimension (if still using uniform tiles)
            if force_uniform_tiles:
                if num_tiles_y == 1:
                    tiles_y = [0]
                else:
                    # Calculate step to distribute tiles evenly
                    step_y = (image_height - tile_height) / (num_tiles_y - 1)
                    # Check for excessive overlap
                    if step_y < tile_height / 2:
                        debug_log(f"Warning: Uniform tiles would have excessive overlap (step_y={step_y}, tile_height={tile_height}). Falling back to non-uniform mode.")
                        force_uniform_tiles = False
                    else:
                        tiles_y = [round(i * step_y) for i in range(num_tiles_y)]
                        # Ensure tiles are aligned to 8-pixel boundaries
                        tiles_y = [self.round_to_multiple(y, 8) for y in tiles_y]
        
        # If still using uniform tiles, generate positions
        if force_uniform_tiles:
            # Generate all tile positions
            for y in tiles_y:
                for x in tiles_x:
                    tiles.append((x, y))
                    
            return tiles
        
        # Fall through to non-uniform mode if needed
        if not force_uniform_tiles:
            # Original non-uniform tile calculation
            tiles = []
            for y in range(0, image_height, tile_height):
                for x in range(0, image_width, tile_width):
                    tiles.append((x, y))
            return tiles
    
    def extract_tile_with_padding(self, image: torch.Tensor, x: int, y: int,
                                 tile_width: int, tile_height: int, padding: int) -> Tuple[torch.Tensor, int, int, int, int]:
        """Extract a tile from the image with padding."""
        _, h, w, _ = image.shape
        
        # Calculate extraction bounds with padding
        x1 = max(0, x - padding)
        y1 = max(0, y - padding)
        x2 = min(w, x + tile_width + padding)
        y2 = min(h, y + tile_height + padding)
        
        # Store the actual extracted size before resizing
        extracted_width = x2 - x1
        extracted_height = y2 - y1
        
        # Debug logging (uncomment if needed)
        # debug_log(f"[Extract] Tile at ({x}, {y}) -> Extract from ({x1}, {y1}) to ({x2}, {y2}), size: {extracted_width}x{extracted_height}")
        
        # Extract tile
        tile = image[:, y1:y2, x1:x2, :]
        
        # Convert to PIL using utility function
        tile_pil = tensor_to_pil(tile, 0)
        
        # Resize to target dimensions for processing
        tile_pil = tile_pil.resize((tile_width, tile_height), Image.LANCZOS)
        
        # Convert back to tensor using utility function
        tile_tensor = pil_to_tensor(tile_pil)
        
        # Move to same device as original image
        if image.is_cuda:
            tile_tensor = tile_tensor.cuda()
        
        return tile_tensor, x1, y1, extracted_width, extracted_height
    
    def process_tile(self, tile_tensor: torch.Tensor, model, positive, negative, vae,
                     seed: int, steps: int, cfg: float, sampler_name: str, 
                     scheduler: str, denoise: float, tiled_decode: bool = False,
                     batch_idx: int = 0, region: Tuple[int, int, int, int] = None,
                     image_size: Tuple[int, int] = None) -> torch.Tensor:
        """Process a single tile through SD sampling. 
        Note: positive and negative should already be pre-sliced for the current batch_idx."""
        debug_log(f"[process_tile] Processing tile for batch_idx={batch_idx}, seed={seed}, region={region}")
        
        
        # Import here to avoid circular dependencies
        from nodes import common_ksampler, VAEEncode, VAEDecode
        
        # Try to import tiled VAE nodes if available
        try:
            from nodes import VAEEncodeTiled, VAEDecodeTiled
            tiled_vae_available = True
        except ImportError:
            tiled_vae_available = False
            if tiled_decode:
                debug_log("Tiled VAE nodes not available, falling back to standard VAE")
        
        # Convert to PIL and back to ensure clean tensor without gradient tracking
        tile_pil = tensor_to_pil(tile_tensor, 0)
        clean_tensor = pil_to_tensor(tile_pil)
        
        # Ensure tensor is detached and doesn't require gradients
        clean_tensor = clean_tensor.detach()
        if hasattr(clean_tensor, 'requires_grad_'):
            clean_tensor.requires_grad_(False)
        
        # Move to correct device
        if tile_tensor.is_cuda:
            clean_tensor = clean_tensor.cuda()
            clean_tensor = clean_tensor.detach()  # Detach again after device transfer
        
        # Clone conditioning per tile (shares models, clones hints for cropping)
        positive_tile = clone_conditioning(positive, clone_hints=True)
        negative_tile = clone_conditioning(negative, clone_hints=True)
        
        # Crop conditioning to tile region if provided (assumes hints at image resolution)
        if region is not None and image_size is not None:
            init_size = image_size  # (width, height) of full image
            canvas_size = image_size
            tile_size = (tile_tensor.shape[2], tile_tensor.shape[1])  # (width, height)
            w_pad = 0  # No extra pad needed; region already includes padding
            h_pad = 0
            positive_cropped = crop_cond(positive_tile, region, init_size, canvas_size, tile_size, w_pad, h_pad)
            negative_cropped = crop_cond(negative_tile, region, init_size, canvas_size, tile_size, w_pad, h_pad)
        else:
            # No region cropping needed, use cloned conditioning as-is
            positive_cropped = positive_tile
            negative_cropped = negative_tile
        
        # Encode to latent (always non-tiled, matching original node)
        latent = VAEEncode().encode(vae, clean_tensor)[0]
        
        # Sample
        samples = common_ksampler(model, seed, steps, cfg, sampler_name, scheduler, 
                                positive_cropped, negative_cropped, latent, denoise=denoise)[0]
        
        # Decode back to image
        if tiled_decode and tiled_vae_available:
            image = VAEDecodeTiled().decode(vae, samples, tile_size=512)[0]
        else:
            image = VAEDecode().decode(vae, samples)[0]
        
        return image
    
    def create_tile_mask(self, image_width: int, image_height: int,
                        x: int, y: int, tile_width: int, tile_height: int, 
                        mask_blur: int) -> Image.Image:
        """Create a mask for blending tiles - matches Ultimate SD Upscale approach.
        
        Creates a black image with a white rectangle at the tile position,
        then applies blur to create soft edges.
        """
        # Create a full-size mask matching the image dimensions
        mask = Image.new('L', (image_width, image_height), 0)  # Black background
        
        # Draw white rectangle at tile position
        draw = ImageDraw.Draw(mask)
        draw.rectangle([x, y, x + tile_width, y + tile_height], fill=255)
        
        # Apply blur to soften edges
        if mask_blur > 0:
            mask = mask.filter(ImageFilter.GaussianBlur(mask_blur))
        
        return mask
    
    def blend_tile(self, base_image: Image.Image, tile_image: Image.Image,
                  x: int, y: int, extracted_size: Tuple[int, int], 
                  mask: Image.Image, padding: int) -> Image.Image:
        """Blend a processed tile back into the base image using Ultimate SD Upscale's exact approach.
        
        This follows the exact method from ComfyUI_UltimateSDUpscale/modules/processing.py
        """
        extracted_width, extracted_height = extracted_size

        # Debug logging (uncomment if needed)
        # debug_log(f"[Blend] Placing tile at ({x}, {y}), size: {extracted_width}x{extracted_height}")

        
        # Calculate the crop region that was used for extraction
        crop_region = (x, y, x + extracted_width, y + extracted_height)
        
        # The mask is already full-size, no need to crop
        
        # Resize the processed tile back to the extracted size
        if tile_image.size != (extracted_width, extracted_height):
            tile_resized = tile_image.resize((extracted_width, extracted_height), Image.LANCZOS)
        else:
            tile_resized = tile_image
        
        # Follow Ultimate SD Upscale blending approach:
        # Put the tile into position
        image_tile_only = Image.new('RGBA', base_image.size)
        image_tile_only.paste(tile_resized, crop_region[:2])
        
        # Add the mask as an alpha channel
        # Must make a copy due to the possibility of an edge becoming black
        temp = image_tile_only.copy()
        temp.putalpha(mask)  # Use the full image mask
        image_tile_only.paste(temp, image_tile_only)
        
        # Add back the tile to the initial image according to the mask in the alpha channel
        result = base_image.convert('RGBA')
        result.alpha_composite(image_tile_only)
        
        # Convert back to RGB
        return result.convert('RGB')
    
    def _slice_conditioning(self, positive, negative, batch_idx):
        """Helper to slice conditioning for a specific batch index."""
        # Clone and slice conditioning properly, including ControlNet hints
        positive_sliced = clone_conditioning(positive)
        negative_sliced = clone_conditioning(negative)
        
        for cond_list in [positive_sliced, negative_sliced]:
            for i in range(len(cond_list)):
                emb, cond_dict = cond_list[i]
                if emb.shape[0] > 1:
                    cond_list[i][0] = emb[batch_idx:batch_idx+1]
                if 'control' in cond_dict:
                    control = cond_dict['control']
                    while control is not None:
                        hint = control.cond_hint_original
                        if hint.shape[0] > 1:
                            control.cond_hint_original = hint[batch_idx:batch_idx+1]
                        control = control.previous_controlnet
                if 'mask' in cond_dict and cond_dict['mask'].shape[0] > 1:
                    cond_dict['mask'] = cond_dict['mask'][batch_idx:batch_idx+1]
        
        return positive_sliced, negative_sliced
    
    async def _get_all_completed_tasks(self, multi_job_id):
        """Helper to retrieve all completed tasks from the job data."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and JOB_COMPLETED_TASKS in job_data:
                return dict(job_data[JOB_COMPLETED_TASKS])  # Return a copy
            return {}
    
    async def _process_worker_static_async(self, upscaled_image, model, positive, negative, vae,
                                    seed, steps, cfg, sampler_name, scheduler, denoise,
                                    tile_width, tile_height, padding, mask_blur,
                                    force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                    worker_id, enabled_workers):
        """Enhanced static mode worker with health monitoring and retry logic."""
        # Round tile dimensions
        tile_width = self.round_to_multiple(tile_width)
        tile_height = self.round_to_multiple(tile_height)
        
        # Calculate all tile positions
        batch_size = upscaled_image.shape[0]
        _, height, width, _ = upscaled_image.shape
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
        
        # Calculate total tiles across all images
        num_tiles_per_image = len(all_tiles)
        total_tiles = batch_size * num_tiles_per_image
        
        # Get initial assigned global tile indices for this worker
        assigned_global_indices = self._get_worker_global_indices(total_tiles, enabled_workers, worker_id)
        
        debug_log(f"UltimateSDUpscale Worker - Worker {worker_id} initially assigned {len(assigned_global_indices)} tiles")
        
        # Process initially assigned tiles
        processed_tiles = []
        sliced_conditioning_cache = {}
        
        # Process assigned tiles with heartbeat after each
        for global_idx in assigned_global_indices:
            # Process the tile
            processed_tile = await self._process_single_tile(
                global_idx, num_tiles_per_image, upscaled_image, all_tiles,
                model, positive, negative, vae, seed, steps, cfg, sampler_name,
                scheduler, denoise, tiled_decode, tile_width, tile_height, padding,
                width, height, sliced_conditioning_cache
            )
            
            if processed_tile:
                processed_tiles.append(processed_tile)
                
                # Send heartbeat after each tile
                try:
                    await _send_heartbeat_to_master(multi_job_id, master_url, worker_id)
                except Exception as e:
                    debug_log(f"Heartbeat failed: {e}")
        
        # Send initial batch of processed tiles
        if processed_tiles:
            await self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id)
            processed_tiles = []
        
        # Check for requeued tiles from failed workers
        debug_log(f"Worker {worker_id} checking for requeued tiles...")
        while True:
            # Request next task from pending queue
            task_id = await _get_next_task(multi_job_id)
            if task_id is None:
                break
                
            debug_log(f"Worker {worker_id} processing requeued tile {task_id}")
            
            # Process the requeued tile
            processed_tile = await self._process_single_tile(
                task_id, num_tiles_per_image, upscaled_image, all_tiles,
                model, positive, negative, vae, seed, steps, cfg, sampler_name,
                scheduler, denoise, tiled_decode, tile_width, tile_height, padding,
                width, height, sliced_conditioning_cache
            )
            
            if processed_tile:
                processed_tiles.append(processed_tile)
                
                # Send heartbeat
                try:
                    await _send_heartbeat_to_master(multi_job_id, master_url, worker_id)
                except Exception as e:
                    debug_log(f"Heartbeat failed: {e}")
                
                # Send tiles in batches
                if len(processed_tiles) >= MAX_BATCH:
                    await self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id)
                    processed_tiles = []
        
        # Send any remaining tiles
        if processed_tiles:
            await self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id)
        
        debug_log(f"Worker {worker_id} completed all assigned and requeued tiles")
        return (upscaled_image,)
    
    def _process_worker_static_sync(self, upscaled_image, model, positive, negative, vae,
                                    seed, steps, cfg, sampler_name, scheduler, denoise,
                                    tile_width, tile_height, padding, mask_blur,
                                    force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                    worker_id, enabled_workers, static_distribution=False):
        """Worker static mode processing with optional dynamic queue pulling."""
        # Round tile dimensions
        tile_width = self.round_to_multiple(tile_width)
        tile_height = self.round_to_multiple(tile_height)
        
        # Get dimensions and calculate tiles
        _, height, width, _ = upscaled_image.shape
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
        num_tiles_per_image = len(all_tiles)
        batch_size = upscaled_image.shape[0]
        total_tiles = batch_size * num_tiles_per_image
        
        processed_tiles = []
        sliced_conditioning_cache = {}
        
        if static_distribution:
            # Legacy mode: Use pre-assigned tiles
            debug_log(f"UltimateSDUpscale Worker - Worker {worker_id} using static tile assignment")
            worker_global_indices = self._get_worker_global_indices(total_tiles, enabled_workers, worker_id)
            debug_log(f"UltimateSDUpscale Worker - Worker {worker_id} processing {len(worker_global_indices)} pre-assigned tiles")
            
            # Process assigned tiles synchronously
            for global_idx in worker_global_indices:
                batch_idx = global_idx // num_tiles_per_image
                tile_idx = global_idx % num_tiles_per_image
                
                if batch_idx >= batch_size:
                    continue
                
                # Get or create sliced conditioning
                if batch_idx not in sliced_conditioning_cache:
                    positive_sliced, negative_sliced = self._slice_conditioning(positive, negative, batch_idx)
                    sliced_conditioning_cache[batch_idx] = (positive_sliced, negative_sliced)
                else:
                    positive_sliced, negative_sliced = sliced_conditioning_cache[batch_idx]
                
                # Process tile synchronously
                processed_tile = self._process_single_tile(
                    global_idx, num_tiles_per_image, upscaled_image, all_tiles,
                    model, positive_sliced, negative_sliced, vae, seed, steps, cfg, sampler_name,
                    scheduler, denoise, tiled_decode, tile_width, tile_height, padding,
                    width, height, sliced_conditioning_cache
                )
                
                if processed_tile:
                    processed_tiles.append(processed_tile)
                    
                    # Send heartbeat (async operation)
                    try:
                        run_async_in_server_loop(
                            _send_heartbeat_to_master(multi_job_id, master_url, worker_id),
                            timeout=5.0
                        )
                    except Exception as e:
                        debug_log(f"Heartbeat failed: {e}")
                    
                    # Send tiles in batches
                    if len(processed_tiles) >= MAX_BATCH:
                        run_async_in_server_loop(
                            self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id),
                            timeout=TILE_SEND_TIMEOUT
                        )
                        processed_tiles = []
            
            # Send remaining tiles
            if processed_tiles:
                run_async_in_server_loop(
                    self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id),
                    timeout=TILE_SEND_TIMEOUT
                )
                processed_tiles = []
            
            # Check for requeued tiles from failed workers
            debug_log(f"Worker {worker_id} checking for requeued tiles...")
            while True:
                # Request next task from pending queue (async operation)
                task_id = run_async_in_server_loop(
                    _get_next_task(multi_job_id),
                    timeout=5.0
                )
                if task_id is None:
                    break
                    
                debug_log(f"Worker {worker_id} processing requeued tile {task_id}")
                
                # Process the requeued tile synchronously
                batch_idx = task_id // num_tiles_per_image
                tile_idx = task_id % num_tiles_per_image
                
                if batch_idx >= batch_size:
                    continue
                
                # Get or create sliced conditioning
                if batch_idx not in sliced_conditioning_cache:
                    positive_sliced, negative_sliced = self._slice_conditioning(positive, negative, batch_idx)
                    sliced_conditioning_cache[batch_idx] = (positive_sliced, negative_sliced)
                else:
                    positive_sliced, negative_sliced = sliced_conditioning_cache[batch_idx]
                
                processed_tile = self._process_single_tile(
                    task_id, num_tiles_per_image, upscaled_image, all_tiles,
                    model, positive_sliced, negative_sliced, vae, seed, steps, cfg, sampler_name,
                    scheduler, denoise, tiled_decode, tile_width, tile_height, padding,
                    width, height, sliced_conditioning_cache
                )
                
                if processed_tile:
                    processed_tiles.append(processed_tile)
                    
                    # Send heartbeat
                    try:
                        run_async_in_server_loop(
                            _send_heartbeat_to_master(multi_job_id, master_url, worker_id),
                            timeout=5.0
                        )
                    except Exception as e:
                        debug_log(f"Heartbeat failed: {e}")
                    
                    # Send tiles in batches
                    if len(processed_tiles) >= MAX_BATCH:
                        run_async_in_server_loop(
                            self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id),
                            timeout=TILE_SEND_TIMEOUT
                        )
                        processed_tiles = []
        
        else:
            # Dynamic queue mode
            debug_log(f"UltimateSDUpscale Worker - Worker {worker_id} ready to process tiles dynamically")
            processed_count = 0
            
            # Poll for job readiness
            max_poll_attempts = 20
            for attempt in range(max_poll_attempts):
                ready = run_async_in_server_loop(
                    self._check_job_status(multi_job_id, master_url),
                    timeout=5.0
                )
                if ready:
                    debug_log(f"Job {multi_job_id} ready after {attempt} attempts")
                    break
                time.sleep(1.0)
            else:
                log(f"Job {multi_job_id} not ready after {max_poll_attempts} attempts, aborting")
                return (upscaled_image,)
            
            # Main processing loop - pull tiles from queue
            while True:
                # Request a tile to process
                tile_idx, estimated_remaining = run_async_in_server_loop(
                    self._request_tile_from_master(multi_job_id, master_url, worker_id),
                    timeout=TILE_WAIT_TIMEOUT
                )
                
                if tile_idx is None:
                    debug_log(f"UltimateSDUpscale Worker - No more tiles to process")
                    break
                
                debug_log(f"UltimateSDUpscale Worker - Assigned tile {tile_idx} to worker {worker_id}")
                processed_count += 1
                
                # Calculate batch and local tile indices
                batch_idx = tile_idx // num_tiles_per_image
                local_tile_idx = tile_idx % num_tiles_per_image
                
                if batch_idx >= batch_size:
                    continue
                
                # Get or create sliced conditioning
                if batch_idx not in sliced_conditioning_cache:
                    positive_sliced, negative_sliced = self._slice_conditioning(positive, negative, batch_idx)
                    sliced_conditioning_cache[batch_idx] = (positive_sliced, negative_sliced)
                else:
                    positive_sliced, negative_sliced = sliced_conditioning_cache[batch_idx]
                
                # Process tile synchronously
                processed_tile = self._process_single_tile(
                    tile_idx, num_tiles_per_image, upscaled_image, all_tiles,
                    model, positive_sliced, negative_sliced, vae, seed, steps, cfg, sampler_name,
                    scheduler, denoise, tiled_decode, tile_width, tile_height, padding,
                    width, height, sliced_conditioning_cache
                )
                
                if processed_tile:
                    processed_tiles.append(processed_tile)
                    
                    # Send heartbeat
                    try:
                        run_async_in_server_loop(
                            _send_heartbeat_to_master(multi_job_id, master_url, worker_id),
                            timeout=5.0
                        )
                    except Exception as e:
                        debug_log(f"Heartbeat failed: {e}")
                    
                    # Send tiles in batches
                    if len(processed_tiles) >= MAX_BATCH:
                        run_async_in_server_loop(
                            self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id),
                            timeout=TILE_SEND_TIMEOUT
                        )
                        processed_tiles = []
        
        # Send any remaining tiles
        if processed_tiles:
            run_async_in_server_loop(
                self.send_tiles_batch_to_master(processed_tiles, multi_job_id, master_url, padding, worker_id),
                timeout=TILE_SEND_TIMEOUT
            )
        
        debug_log(f"Worker {worker_id} completed all assigned and requeued tiles")
        return (upscaled_image,)
    
    async def _async_collect_and_monitor_static(self, multi_job_id, total_tiles, expected_total):
        """Async helper for collection and monitoring in static mode.
        Returns collected tasks dict. Caller should check if all tasks are complete."""
        last_progress_log = time.time()
        progress_interval = 5.0
        last_heartbeat_check = time.time()
        stall_count = 0
        max_stall_count = 120  # Allow 120 iterations with no progress before returning
        last_completed_count = 0
        
        while True:
            # Check for user interruption
            if comfy.model_management.processing_interrupted():
                log("Processing interrupted by user")
                raise comfy.model_management.InterruptProcessingException()
            
            # Drain any pending results
            collected_count = await _drain_results_queue(multi_job_id)
            
            # Check and requeue timed-out workers periodically
            current_time = time.time()
            if current_time - last_heartbeat_check >= 10.0:
                requeued_count = await self._check_and_requeue_timed_out_workers(multi_job_id, expected_total)
                if requeued_count > 0:
                    log(f"Requeued {requeued_count} tasks from timed-out workers")
                    stall_count = 0  # Reset stall count when we requeue
                last_heartbeat_check = current_time
            
            # Get current completion count
            completed_count = await _get_completed_count(multi_job_id)
            
            # Progress logging
            if current_time - last_progress_log >= progress_interval:
                log(f"Progress: {completed_count}/{expected_total} tasks completed")
                last_progress_log = current_time
            
            # Check if all tasks are completed
            if completed_count >= expected_total:
                debug_log(f"All {expected_total} tasks completed")
                break
            
            # Check if we're making progress
            if completed_count > last_completed_count:
                stall_count = 0  # Reset stall count on progress
                last_completed_count = completed_count
            else:
                stall_count += 1
            
            # If no progress for too long, check if we need to process locally
            if stall_count >= max_stall_count:
                prompt_server = ensure_tile_jobs_initialized()
                async with prompt_server.distributed_tile_jobs_lock:
                    job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
                    if job_data:
                        pending_queue = job_data.get(JOB_PENDING_TASKS)
                        # If there are pending tasks but no active workers, or we're stalled
                        if pending_queue and not pending_queue.empty():
                            log(f"Collection stalled with {expected_total - completed_count} tasks remaining. Returning for local processing.")
                            break
            
            # Wait a bit before next check
            await asyncio.sleep(0.5)
        
        # Get all completed tasks for return
        return await self._get_all_completed_tasks(multi_job_id)
    
    def _process_master_static_sync(self, upscaled_image, model, positive, negative, vae,
                                    seed, steps, cfg, sampler_name, scheduler, denoise,
                                    tile_width, tile_height, padding, mask_blur,
                                    force_uniform_tiles, tiled_decode, multi_job_id, enabled_workers,
                                    all_tiles, num_tiles_per_image, static_distribution=False):
        """Static mode master processing with optional dynamic queue pulling."""
        batch_size = upscaled_image.shape[0]
        _, height, width, _ = upscaled_image.shape
        total_tiles = batch_size * num_tiles_per_image
        
        # Convert batch to PIL list for processing
        result_images = []
        for b in range(batch_size):
            image_pil = tensor_to_pil(upscaled_image[b:b+1], 0)
            result_images.append(image_pil.copy())
        
        sliced_conditioning_cache = {}
        master_indices = set()  # Track which tiles master processed
        master_processed_count = 0  # Track how many tiles master processed
        
        if static_distribution:
            # Legacy mode: Pre-assign tiles to master and workers
            debug_log(f"UltimateSDUpscale Master - Using static tile distribution")
            
            # Distribute tasks across master and workers
            all_indices = list(range(total_tiles))
            num_participants = len(enabled_workers) + 1
            task_assignments = _distribute_tasks(all_indices, num_participants)
            master_indices = set(task_assignments[0])  # Convert to set for efficient lookup
            
            # Initialize job queue with task assignments (don't populate pending queue)
            run_async_in_server_loop(
                self._init_job_queue(multi_job_id, total_tiles, enabled_workers, batch_size, num_tiles_per_image, task_assignments),
                timeout=10.0
            )
            
            debug_log(f"UltimateSDUpscale Master - Initialized static job queue for {total_tiles} tiles")
            debug_log(f"UltimateSDUpscale Master - Processing {len(master_indices)} tiles locally")
            master_processed_count = len(master_indices)
            
            # Process master's tiles synchronously
            for global_idx in master_indices:
                # Check for user interruption
                comfy.model_management.throw_exception_if_processing_interrupted()
                batch_idx = global_idx // num_tiles_per_image
                tile_idx = global_idx % num_tiles_per_image
                
                if batch_idx >= batch_size:
                    continue
                
                # Get or create sliced conditioning
                if batch_idx not in sliced_conditioning_cache:
                    positive_sliced, negative_sliced = self._slice_conditioning(positive, negative, batch_idx)
                    sliced_conditioning_cache[batch_idx] = (positive_sliced, negative_sliced)
                else:
                    positive_sliced, negative_sliced = sliced_conditioning_cache[batch_idx]
                
                # Use unique seed per image
                image_seed = seed + batch_idx * 1000
                
                # Process tile synchronously - no async context for tensor operations
                result_images[batch_idx] = self._process_and_blend_tile(
                    tile_idx, all_tiles[tile_idx], upscaled_image[batch_idx:batch_idx+1], result_images[batch_idx],
                    model, positive_sliced, negative_sliced, vae, image_seed, steps, cfg,
                    sampler_name, scheduler, denoise, tile_width, tile_height,
                    padding, mask_blur, width, height, tiled_decode, batch_idx=batch_idx
                )
                
                # Mark as completed (async operation)
                run_async_in_server_loop(
                    _mark_task_completed(multi_job_id, global_idx, {'batch_idx': batch_idx, 'tile_idx': tile_idx}),
                    timeout=5.0
                )
        else:
            # Dynamic queue mode: All tiles go to pending queue
            debug_log(f"UltimateSDUpscale Master - Using dynamic tile distribution")
            
            # Initialize job queue with all tiles in pending queue for dynamic distribution
            run_async_in_server_loop(
                self._init_job_queue(multi_job_id, total_tiles, enabled_workers, batch_size, num_tiles_per_image),
                timeout=10.0
            )
            
            debug_log(f"UltimateSDUpscale Master - Initialized static job queue for {total_tiles} tiles with dynamic distribution")
            debug_log(f"UltimateSDUpscale Master - Processing tiles dynamically from queue")
            
            # Process tiles by pulling from queue (similar to dynamic mode)
            processed_count = 0
            consecutive_no_tile = 0
            max_consecutive_no_tile = 5
            
            while processed_count < total_tiles:
                # Check for user interruption
                comfy.model_management.throw_exception_if_processing_interrupted()
                
                # Try to get a tile to process
                tile_idx = run_async_in_server_loop(
                    self._get_next_tile_index(multi_job_id),
                    timeout=5.0
                )
                
                if tile_idx is not None:
                    consecutive_no_tile = 0
                    processed_count += 1
                    
                    # Calculate batch and local tile indices
                    batch_idx = tile_idx // num_tiles_per_image
                    local_tile_idx = tile_idx % num_tiles_per_image
                    
                    if batch_idx >= batch_size:
                        continue
                    
                    debug_log(f"Master processing tile {tile_idx} (batch {batch_idx}, tile {local_tile_idx})")
                    
                    # Get or create sliced conditioning
                    if batch_idx not in sliced_conditioning_cache:
                        positive_sliced, negative_sliced = self._slice_conditioning(positive, negative, batch_idx)
                        sliced_conditioning_cache[batch_idx] = (positive_sliced, negative_sliced)
                    else:
                        positive_sliced, negative_sliced = sliced_conditioning_cache[batch_idx]
                    
                    # Use unique seed per image
                    image_seed = seed + batch_idx * 1000
                    
                    # Process tile synchronously
                    result_images[batch_idx] = self._process_and_blend_tile(
                        local_tile_idx, all_tiles[local_tile_idx], upscaled_image[batch_idx:batch_idx+1], result_images[batch_idx],
                        model, positive_sliced, negative_sliced, vae, image_seed, steps, cfg,
                        sampler_name, scheduler, denoise, tile_width, tile_height,
                        padding, mask_blur, width, height, tiled_decode, batch_idx=batch_idx
                    )
                    
                    # Mark as completed
                    run_async_in_server_loop(
                        _mark_task_completed(multi_job_id, tile_idx, {'batch_idx': batch_idx, 'tile_idx': local_tile_idx}),
                        timeout=5.0
                    )
                else:
                    consecutive_no_tile += 1
                    if consecutive_no_tile >= max_consecutive_no_tile:
                        # No more tiles available, break to start collecting
                        debug_log(f"Master processed {processed_count} tiles, moving to collection phase")
                        break
                    # Small wait before retry
                    time.sleep(0.1)
            
            # Set master_processed_count for the collection phase
            master_processed_count = processed_count
        
        # Continue processing any remaining tiles while collecting worker results
        remaining_tiles = total_tiles - master_processed_count
        if remaining_tiles > 0:
            debug_log(f"Master waiting for {remaining_tiles} tiles from workers")
            
            # Collect worker results using async operations
            try:
                collected_tasks = run_async_in_server_loop(
                    self._async_collect_and_monitor_static(multi_job_id, total_tiles, expected_total=total_tiles),
                    timeout=300.0  # 5 minutes to allow for retries and requeuing
                )
            except comfy.model_management.InterruptProcessingException:
                # Clean up job on interruption
                run_async_in_server_loop(_cleanup_job(multi_job_id), timeout=5.0)
                raise
            
            # Check if we need to process any remaining tasks locally after collection
            completed_count = len(collected_tasks)
            if completed_count < total_tiles:
                log(f"Processing remaining {total_tiles - completed_count} tasks locally after worker failures")
                
                # Process any remaining pending tasks
                while True:
                    # Check for user interruption
                    comfy.model_management.throw_exception_if_processing_interrupted()
                    
                    # Get next task from pending queue
                    task_id = run_async_in_server_loop(
                        self._get_next_tile_index(multi_job_id),
                        timeout=5.0
                    )
                    
                    if task_id is None:
                        break
                    
                    batch_idx = task_id // num_tiles_per_image
                    local_tile_idx = task_id % num_tiles_per_image
                    
                    if batch_idx >= batch_size:
                        continue
                    
                    # Get or create sliced conditioning
                    if batch_idx not in sliced_conditioning_cache:
                        positive_sliced, negative_sliced = self._slice_conditioning(positive, negative, batch_idx)
                        sliced_conditioning_cache[batch_idx] = (positive_sliced, negative_sliced)
                    else:
                        positive_sliced, negative_sliced = sliced_conditioning_cache[batch_idx]
                    
                    # Use unique seed per image
                    image_seed = seed + batch_idx * 1000
                    
                    # Process tile synchronously
                    result_images[batch_idx] = self._process_and_blend_tile(
                        local_tile_idx, all_tiles[local_tile_idx], upscaled_image[batch_idx:batch_idx+1], result_images[batch_idx],
                        model, positive_sliced, negative_sliced, vae, image_seed, steps, cfg,
                        sampler_name, scheduler, denoise, tile_width, tile_height,
                        padding, mask_blur, width, height, tiled_decode, batch_idx=batch_idx
                    )
                    
                    # Mark as completed and store in collected_tasks
                    run_async_in_server_loop(
                        _mark_task_completed(multi_job_id, task_id, {'batch_idx': batch_idx, 'tile_idx': local_tile_idx}),
                        timeout=5.0
                    )
                    # Add to collected_tasks so we skip it in the blending loop
                    collected_tasks[task_id] = {'batch_idx': batch_idx, 'tile_idx': local_tile_idx}
        else:
            # Master processed all tiles
            collected_tasks = run_async_in_server_loop(
                self._get_all_completed_tasks(multi_job_id),
                timeout=5.0
            )
        
        # Blend worker tiles synchronously
        for global_idx, tile_data in collected_tasks.items():
            # Skip tiles already processed by master
            if static_distribution and global_idx in master_indices:
                continue  # Skip master's tiles in static mode
            
            # Skip tiles that don't have tensor data (already processed)
            if 'tensor' not in tile_data:
                continue
            
            batch_idx = tile_data.get('batch_idx', global_idx // num_tiles_per_image)
            tile_idx = tile_data.get('tile_idx', global_idx % num_tiles_per_image)
            
            if batch_idx >= batch_size:
                continue
            
            # Blend tile synchronously
            x = tile_data.get('x', 0)
            y = tile_data.get('y', 0)
            tile_tensor = tile_data['tensor']
            tile_pil = tensor_to_pil(tile_tensor, 0)
            orig_x, orig_y = all_tiles[tile_idx]
            tile_mask = self.create_tile_mask(width, height, orig_x, orig_y, tile_width, tile_height, mask_blur)
            extracted_width = tile_data.get('extracted_width', tile_width + 2 * padding)
            extracted_height = tile_data.get('extracted_height', tile_height + 2 * padding)
            result_images[batch_idx] = self.blend_tile(result_images[batch_idx], tile_pil,
                                                      x, y, (extracted_width, extracted_height), tile_mask, padding)
        
        try:
            # Convert back to tensor
            if batch_size == 1:
                result_tensor = pil_to_tensor(result_images[0])
            else:
                result_tensors = [pil_to_tensor(img) for img in result_images]
                result_tensor = torch.cat(result_tensors, dim=0)
            
            if upscaled_image.is_cuda:
                result_tensor = result_tensor.cuda()
            
            log(f"UltimateSDUpscale Master - Job {multi_job_id} complete")
            return (result_tensor,)
        finally:
            # Cleanup (async operation) - always execute
            run_async_in_server_loop(_cleanup_job(multi_job_id), timeout=5.0)
    
    def _process_single_tile(self, global_idx, num_tiles_per_image, upscaled_image, all_tiles,
                                  model, positive, negative, vae, seed, steps, cfg, sampler_name,
                                  scheduler, denoise, tiled_decode, tile_width, tile_height, padding,
                                  width, height, sliced_conditioning_cache):
        """Process a single tile."""
        # Calculate which image and tile this corresponds to
        batch_idx = global_idx // num_tiles_per_image
        tile_idx = global_idx % num_tiles_per_image
        
        # Skip if batch_idx is out of range
        if batch_idx >= upscaled_image.shape[0]:
            debug_log(f"Warning: Calculated batch_idx {batch_idx} exceeds batch size {upscaled_image.shape[0]}")
            return None
        
        # Get or create sliced conditioning for this batch index
        if batch_idx not in sliced_conditioning_cache:
            positive_sliced, negative_sliced = self._slice_conditioning(positive, negative, batch_idx)
            sliced_conditioning_cache[batch_idx] = (positive_sliced, negative_sliced)
        else:
            positive_sliced, negative_sliced = sliced_conditioning_cache[batch_idx]
        
        x, y = all_tiles[tile_idx]
        
        # Extract tile from the specific image in the batch
        tile_tensor, x1, y1, ew, eh = self.extract_tile_with_padding(
            upscaled_image[batch_idx:batch_idx+1], x, y, tile_width, tile_height, padding
        )
        
        # Process tile through SD with unique seed
        image_seed = seed + batch_idx * 1000
        processed_tile = self.process_tile(tile_tensor, model, positive_sliced, negative_sliced, vae,
                                         image_seed + tile_idx, steps, cfg, sampler_name,
                                         scheduler, denoise, tiled_decode, batch_idx=batch_idx,
                                         region=(x1, y1, x1 + ew, y1 + eh), image_size=(width, height))
        
        return {
            'tile': processed_tile,
            'global_idx': global_idx,
            'batch_idx': batch_idx,
            'tile_idx': tile_idx,
            'x': x1,
            'y': y1,
            'extracted_width': ew,
            'extracted_height': eh
        }
    
    async def send_tiles_batch_to_master(self, processed_tiles, multi_job_id, master_url, 
                                       padding, worker_id):
        """Send all processed tiles to master, chunked if large."""
        if not processed_tiles:
            return  # Early exit if empty
        
        total_tiles = len(processed_tiles)
        debug_log(f"UltimateSDUpscale Worker - Sending {total_tiles} tiles in chunks of max {MAX_BATCH}")
        
        for start in range(0, total_tiles, MAX_BATCH):
            chunk = processed_tiles[start:start + MAX_BATCH]
            chunk_size = len(chunk)
            is_chunk_last = (start + chunk_size == total_tiles)  # True only for final chunk
            
            data = aiohttp.FormData()
            data.add_field('multi_job_id', multi_job_id)
            data.add_field('worker_id', str(worker_id))
            data.add_field('is_last', str(is_chunk_last))
            data.add_field('batch_size', str(chunk_size))
            data.add_field('padding', str(padding))
            
            # Chunk metadata: Keep absolute tile_idx and batch_idx if present
            metadata = []
            for tile_data in chunk:
                meta = {
                    'tile_idx': tile_data['tile_idx'],  # Original/absolute idx
                    'x': tile_data['x'],
                    'y': tile_data['y'],
                    'extracted_width': tile_data['extracted_width'],
                    'extracted_height': tile_data['extracted_height']
                }
                # Add batch_idx and global_idx if present (for batch processing)
                if 'batch_idx' in tile_data:
                    meta['batch_idx'] = tile_data['batch_idx']
                if 'global_idx' in tile_data:
                    meta['global_idx'] = tile_data['global_idx']
                metadata.append(meta)
            
            # Add JSON metadata
            data.add_field('tiles_metadata', json.dumps(metadata), content_type='application/json')
            
            # Add chunk images as 'tile_{j}' where j=0 to chunk_size-1
            for j, tile_data in enumerate(chunk):
                # Convert tensor to PIL
                img = tensor_to_pil(tile_data['tile'], 0)
                byte_io = io.BytesIO()
                img.save(byte_io, format='PNG', compress_level=0)
                byte_io.seek(0)
                
                # Add image field
                data.add_field(f'tile_{j}', byte_io, filename=f'tile_{j}.png', content_type='image/png')
            
            
            # Retry logic with exponential backoff
            max_retries = 5
            retry_delay = 0.5
            
            for attempt in range(max_retries):
                try:
                    session = await get_client_session()
                    url = f"{master_url}/distributed/submit_tiles"
                    
                    async with session.post(url, data=data) as response:
                        response.raise_for_status()
                        break  # Success, move to next chunk
                        
                except Exception as e:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        log(f"UltimateSDUpscale Worker - Failed to send chunk after {max_retries} attempts: {e}")
                        raise

    def process_single_gpu(self, upscaled_image, model, positive, negative, vae,
                          seed, steps, cfg, sampler_name, scheduler, denoise,
                          tile_width, tile_height, padding, mask_blur, force_uniform_tiles, tiled_decode):
        """Process all tiles on a single GPU (no distribution)."""
        # Round tile dimensions
        tile_width = self.round_to_multiple(tile_width)
        tile_height = self.round_to_multiple(tile_height)
        
        # Get image dimensions and batch size
        batch_size, height, width, _ = upscaled_image.shape
        
        # Calculate all tiles
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
        
        debug_log(f"UltimateSDUpscale - Processing {len(all_tiles)} tiles locally for batch of {batch_size}")
        
        # Convert batch to PIL list
        result_images = []
        for b in range(batch_size):
            image_pil = tensor_to_pil(upscaled_image[b:b+1], 0)
            # Ensure RGB mode for consistency
            if image_pil.mode != 'RGB':
                image_pil = image_pil.convert('RGB')
            result_images.append(image_pil.copy())
        
        # Process each image in the batch
        for batch_idx in range(batch_size):
            debug_log(f"[process_single_gpu] Processing batch_idx {batch_idx}")
            # Pre-slice conditioning once per image (not per tile)
            positive_sliced = clone_conditioning(positive)
            negative_sliced = clone_conditioning(negative)
            for cond_list in [positive_sliced, negative_sliced]:
                for i in range(len(cond_list)):
                    emb, cond_dict = cond_list[i]
                    if emb.shape[0] > 1:
                        cond_list[i][0] = emb[batch_idx:batch_idx+1]
                    if 'control' in cond_dict:
                        control = cond_dict['control']
                        while control is not None:
                            hint = control.cond_hint_original
                            if hint.shape[0] > 1:
                                control.cond_hint_original = hint[batch_idx:batch_idx+1]
                            control = control.previous_controlnet
                    if 'mask' in cond_dict and cond_dict['mask'].shape[0] > 1:
                        cond_dict['mask'] = cond_dict['mask'][batch_idx:batch_idx+1]
            
            # Process each tile for this image with pre-sliced conditioning
            for tile_idx, tile_pos in enumerate(all_tiles):
                # Use unique seed per image
                image_seed = seed + batch_idx * 1000
                result_images[batch_idx] = self._process_and_blend_tile(
                    tile_idx, tile_pos, upscaled_image[batch_idx:batch_idx+1], result_images[batch_idx],
                    model, positive_sliced, negative_sliced, vae, image_seed, steps, cfg,
                    sampler_name, scheduler, denoise, tile_width, tile_height,
                    padding, mask_blur, width, height, tiled_decode, batch_idx=batch_idx
                )
        
        # Convert back to tensor
        if batch_size == 1:
            result_tensor = pil_to_tensor(result_images[0])
        else:
            result_tensors = [pil_to_tensor(img) for img in result_images]
            result_tensor = torch.cat(result_tensors, dim=0)
            
        if upscaled_image.is_cuda:
            result_tensor = result_tensor.cuda()
        
        return (result_tensor,)
    
    def process_master_dynamic(self, upscaled_image, model, positive, negative, vae,
                              seed, steps, cfg, sampler_name, scheduler, denoise,
                              tile_width, tile_height, padding, mask_blur,
                              force_uniform_tiles, tiled_decode, multi_job_id, enabled_workers):
        """Dynamic mode for large batches - assigns whole images to workers dynamically, including master."""
        # Get batch size and dimensions
        batch_size, height, width, _ = upscaled_image.shape
        num_workers = len(enabled_workers)
        
        debug_log(f"UltimateSDUpscale Master Dynamic - Processing batch of {batch_size} images with {num_workers} workers")
        
        # No fixed share - all images are dynamic
        all_indices = list(range(batch_size))
        
        log(f"Processing {batch_size} images dynamically across master + {num_workers} workers.")
        
        # Calculate tiles for processing
        all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
        
        # Initialize job queue for communication
        try:
            assigned_to_workers = {w: [] for w in enabled_workers}
            worker_status = {w: time.time() for w in enabled_workers}
            
            run_async_in_server_loop(
                self._init_job_queue_dynamic(multi_job_id, batch_size, assigned_to_workers, worker_status, all_indices),
                timeout=2.0
            )
        except Exception as e:
            debug_log(f"UltimateSDUpscale Master Dynamic - Queue initialization error: {e}")
            raise RuntimeError(f"Failed to initialize dynamic mode queue: {e}")
        
        # Convert batch to PIL list
        result_images = [tensor_to_pil(upscaled_image[b:b+1], 0).convert('RGB').copy() for b in range(batch_size)]
        
        # Process images dynamically with master participating
        prompt_server = ensure_tile_jobs_initialized()
        processed_count = 0
        consecutive_retries = 0
        max_consecutive_retries = 10
        
        # Process loop - master pulls from queue and processes synchronously
        while processed_count < batch_size:
            # Try to get an image to process
            image_idx = run_async_in_server_loop(
                self._get_next_image_index(multi_job_id),
                timeout=5.0  # Short timeout to allow frequent checks
            )

            if image_idx is not None:
                # Reset retry counter and process locally
                consecutive_retries = 0
                debug_log(f"Master processing image {image_idx} dynamically")
                processed_count += 1

                # Process locally
                single_tensor = upscaled_image[image_idx:image_idx+1]
                local_image = result_images[image_idx]
                image_seed = seed + image_idx * 1000
                
                # Pre-slice conditioning once per image (not per tile)
                positive_sliced = clone_conditioning(positive)
                negative_sliced = clone_conditioning(negative)
                for cond_list in [positive_sliced, negative_sliced]:
                    for i in range(len(cond_list)):
                        emb, cond_dict = cond_list[i]
                        if emb.shape[0] > 1:
                            cond_list[i][0] = emb[image_idx:image_idx+1]
                        if 'control' in cond_dict:
                            control = cond_dict['control']
                            while control is not None:
                                hint = control.cond_hint_original
                                if hint.shape[0] > 1:
                                    control.cond_hint_original = hint[image_idx:image_idx+1]
                                control = control.previous_controlnet
                        if 'mask' in cond_dict and cond_dict['mask'].shape[0] > 1:
                            cond_dict['mask'] = cond_dict['mask'][image_idx:image_idx+1]
                
                for tile_idx, pos in enumerate(all_tiles):
                    local_image = self._process_and_blend_tile(
                        tile_idx, pos, single_tensor, local_image,
                        model, positive_sliced, negative_sliced, vae, image_seed, steps, cfg,
                        sampler_name, scheduler, denoise, tile_width, tile_height,
                        padding, mask_blur, width, height, tiled_decode, batch_idx=image_idx
                    )
                    
                    # Yield after each tile to minimize worker downtime
                    run_async_in_server_loop(self._async_yield(), timeout=0.1)
                    # Note: No per-tile drain here – that's what makes this "per-image"
                
                result_images[image_idx] = local_image
                
                # Mark as completed
                run_async_in_server_loop(
                    self._mark_image_completed(multi_job_id, image_idx, local_image),
                    timeout=5.0
                )
                
                # NEW: Drain after the full image is marked complete (catches workers who finished during master's processing)
                drained_count = run_async_in_server_loop(
                    self._drain_worker_results_queue(multi_job_id),
                    timeout=5.0
                )
                
                if drained_count > 0:
                    debug_log(f"Drained {drained_count} worker images after master's image {image_idx}")
                
                # NEW: Log overall progress (includes master's image + any drained workers)
                completed_now = run_async_in_server_loop(
                    self._get_total_completed_count(multi_job_id),
                    timeout=1.0
                )
                log(f"Progress: {completed_now}/{batch_size} images completed")
                
                # Yield to allow workers to get new images after completing one
                run_async_in_server_loop(self._async_yield(), timeout=0.1)
            else:
                # Queue empty: collect any queued worker results to update progress
                drained_count = run_async_in_server_loop(
                    self._drain_worker_results_queue(multi_job_id),
                    timeout=5.0
                )
                run_async_in_server_loop(self._async_yield(), timeout=0.1)  # Yield after drain
                
                # Check for timed out workers and requeue their images
                requeued_count = run_async_in_server_loop(
                    self._check_and_requeue_timed_out_workers(multi_job_id, batch_size),
                    timeout=5.0
                )
                run_async_in_server_loop(self._async_yield(), timeout=0.1)  # Yield after requeue
                
                if requeued_count > 0:
                    log(f"Requeued {requeued_count} images from timed out workers")
                    consecutive_retries = 0  # Reset since we have work to do
                    continue

                # Now check total completed (includes newly collected)
                completed_now = run_async_in_server_loop(
                    self._get_total_completed_count(multi_job_id),
                    timeout=1.0
                )
                
                log(f"Progress: {completed_now}/{batch_size} images completed")
                
                if completed_now >= batch_size:
                    break

                run_async_in_server_loop(self._async_yield(), timeout=0.1)  # Yield before pending check
                
                # Check if there are pending images in the queue (could be requeued)
                pending_count = run_async_in_server_loop(
                    self._get_pending_count(multi_job_id),
                    timeout=1.0
                )
                
                if pending_count > 0:
                    consecutive_retries = 0  # Reset retries since there's work to do
                    continue

                consecutive_retries += 1
                if consecutive_retries >= max_consecutive_retries:
                    log(f"Max retries ({max_consecutive_retries}) reached. Forcing collection of remaining results.")
                    break  # Force exit to collection phase

                debug_log("Waiting for workers")
                # Use async sleep to allow event loop to process worker requests
                run_async_in_server_loop(asyncio.sleep(2), timeout=3.0)
        
        debug_log(f"Master processed {processed_count} images locally")
        
        # Get all completed images to check what needs to be collected
        all_completed = run_async_in_server_loop(
            self._get_all_completed_images(multi_job_id),
            timeout=5.0
        )
        
        # Calculate how many we still need to collect
        remaining_to_collect = batch_size - len(all_completed)
        
        if remaining_to_collect > 0:
            debug_log(f"Waiting for {remaining_to_collect} more images from workers")
            collected_images = run_async_in_server_loop(
                self._async_collect_dynamic_images(multi_job_id, remaining_to_collect, num_workers, batch_size, processed_count),
                timeout=TILE_COLLECTION_TIMEOUT * 2
            )
            
            # Merge collected with already completed
            all_completed.update(collected_images)
        
        # Update result images with all completed images
        for idx, processed_img in all_completed.items():
            if idx < batch_size:
                result_images[idx] = processed_img
        
        # Convert back to tensor
        result_tensor = torch.cat([pil_to_tensor(img) for img in result_images], dim=0) if batch_size > 1 else pil_to_tensor(result_images[0])
        if upscaled_image.is_cuda:
            result_tensor = result_tensor.cuda()
        
        debug_log(f"UltimateSDUpscale Master Dynamic - Job {multi_job_id} complete")
        log(f"Completed processing all {batch_size} images")
        return (result_tensor,)
    
    
    async def _async_yield(self):
        """Simple async yield to allow event loop processing."""
        await asyncio.sleep(0)
    
    async def _get_next_image_index(self, multi_job_id):
        """Get next image index from pending queue for master."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data or 'pending_images' not in job_data:
                return None
            try:
                image_idx = await asyncio.wait_for(job_data['pending_images'].get(), timeout=1.0)
                return image_idx
            except asyncio.TimeoutError:
                return None
    
    async def _get_next_tile_index(self, multi_job_id):
        """Get next tile index from pending queue for master in static mode."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data or JOB_PENDING_TASKS not in job_data:
                return None
            try:
                tile_idx = await asyncio.wait_for(job_data[JOB_PENDING_TASKS].get(), timeout=1.0)
                return tile_idx
            except asyncio.TimeoutError:
                return None
    
    
    async def _get_total_completed_count(self, multi_job_id):
        """Get total count of all completed images (master + workers)."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and 'completed_images' in job_data:
                return len(job_data['completed_images'])
            return 0
    
    async def _get_all_completed_images(self, multi_job_id):
        """Get all completed images."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and 'completed_images' in job_data:
                return job_data['completed_images'].copy()
            return {}
    
    async def _get_pending_count(self, multi_job_id):
        """Get count of pending images in the queue."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if job_data and 'pending_images' in job_data:
                return job_data['pending_images'].qsize()
            return 0
    
    async def _drain_worker_results_queue(self, multi_job_id):
        """Drain pending worker results from queue and update completed_images. Returns count of drained images."""
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data or 'queue' not in job_data or 'completed_images' not in job_data:
                return 0
            q = job_data['queue']
            completed_images = job_data['completed_images']

            collected = 0
            while not q.empty():
                try:
                    result = await asyncio.wait_for(q.get(), timeout=0.1)
                    worker_id = result['worker_id']
                    is_last = result.get('is_last', False)

                    if 'image_idx' in result and 'image' in result:
                        image_idx = result['image_idx']
                        image_pil = result['image']
                        if image_idx not in completed_images:
                            completed_images[image_idx] = image_pil
                            collected += 1
                            debug_log(f"Drained image {image_idx} from worker {worker_id}")

                    if is_last:
                        # Optional: track worker completion if needed
                        pass
                except asyncio.TimeoutError:
                    break  # No more immediately available

            if collected > 0:
                debug_log(f"Drained {collected} worker images during retry")
            
            return collected
    
    async def _check_and_requeue_timed_out_workers(self, multi_job_id, batch_size):
        """Check for timed out workers and requeue their assigned images. Returns count of requeued images."""
        # Use the original function from usdu_managment.py
        return await _check_and_requeue_timed_out_workers(multi_job_id, batch_size)
    
    
    def process_worker_dynamic(self, upscaled_image, model, positive, negative, vae,
                                  seed, steps, cfg, sampler_name, scheduler, denoise,
                                  tile_width, tile_height, padding, mask_blur,
                                  force_uniform_tiles, tiled_decode, multi_job_id, master_url,
                                  worker_id, enabled_worker_ids, dynamic_threshold):
            """Worker processing in dynamic mode - processes whole images."""
            # Round tile dimensions
            tile_width = self.round_to_multiple(tile_width)
            tile_height = self.round_to_multiple(tile_height)
            
            # Get dimensions
            _, height, width, _ = upscaled_image.shape
            all_tiles = self.calculate_tiles(width, height, tile_width, tile_height, force_uniform_tiles)
            
            debug_log(f"UltimateSDUpscale Worker Dynamic - Worker {worker_id} ready to process images")
            
            # Keep track of processed images for is_last detection
            processed_count = 0
            
            # Poll for job readiness to avoid races during master init
            max_poll_attempts = 20  # ~20s at 1s sleep
            for attempt in range(max_poll_attempts):
                ready = run_async_in_server_loop(
                    self._check_job_status(multi_job_id, master_url),
                    timeout=5.0
                )
                if ready:
                    debug_log(f"Job {multi_job_id} ready after {attempt} attempts")
                    break
                time.sleep(1.0)  # Poll every 1s
            else:
                log(f"Job {multi_job_id} not ready after {max_poll_attempts} attempts, aborting")
                return (upscaled_image,)
            
            # Loop to request and process images
            while True:
                # Request an image to process
                image_idx, estimated_remaining = run_async_in_server_loop(
                    self._request_image_from_master(multi_job_id, master_url, worker_id),
                    timeout=TILE_WAIT_TIMEOUT
                )
                
                if image_idx is None:
                    debug_log(f"UltimateSDUpscale Worker Dynamic - No more images to process")
                    break
                    
                debug_log(f"UltimateSDUpscale Worker Dynamic - Mode: dynamic, assigned image {image_idx} to worker {worker_id}")
                processed_count += 1
                
                # Determine if this should be marked as last for this worker
                is_last_for_worker = (estimated_remaining == 0)
                
                # Extract single image tensor
                single_tensor = upscaled_image[image_idx:image_idx+1]
                
                # Convert to PIL for processing
                local_image = tensor_to_pil(single_tensor, 0).copy()
                
                # Process all tiles for this image
                image_seed = seed + image_idx * 1000
                
                # Pre-slice conditioning once per image (not per tile)
                positive_sliced = clone_conditioning(positive)
                negative_sliced = clone_conditioning(negative)
                for cond_list in [positive_sliced, negative_sliced]:
                    for i in range(len(cond_list)):
                        emb, cond_dict = cond_list[i]
                        if emb.shape[0] > 1:
                            cond_list[i][0] = emb[image_idx:image_idx+1]
                        if 'control' in cond_dict:
                            control = cond_dict['control']
                            while control is not None:
                                hint = control.cond_hint_original
                                if hint.shape[0] > 1:
                                    control.cond_hint_original = hint[image_idx:image_idx+1]
                                control = control.previous_controlnet
                        if 'mask' in cond_dict and cond_dict['mask'].shape[0] > 1:
                            cond_dict['mask'] = cond_dict['mask'][image_idx:image_idx+1]
                
                for tile_idx, pos in enumerate(all_tiles):
                    local_image = self._process_and_blend_tile(
                        tile_idx, pos, single_tensor, local_image,
                        model, positive_sliced, negative_sliced, vae, image_seed, steps, cfg,
                        sampler_name, scheduler, denoise, tile_width, tile_height,
                        padding, mask_blur, width, height, tiled_decode, batch_idx=image_idx
                    )
                    run_async_in_server_loop(
                        _send_heartbeat_to_master(multi_job_id, master_url, worker_id),
                        timeout=5.0
                    )
                
                # Send processed image back to master
                try:
                    # Use the estimated remaining to determine if this is the last image
                    is_last = is_last_for_worker
                    run_async_in_server_loop(
                        self._send_full_image_to_master(local_image, image_idx, multi_job_id, 
                                                      master_url, worker_id, is_last),
                        timeout=TILE_SEND_TIMEOUT
                    )
                    # Send heartbeat after processing
                    run_async_in_server_loop(
                        _send_heartbeat_to_master(multi_job_id, master_url, worker_id),
                        timeout=5.0
                    )
                    if is_last:
                        break
                except Exception as e:
                    log(f"UltimateSDUpscale Worker Dynamic - Error sending image {image_idx}: {e}")
                    # Continue processing other images
            
            # Send final is_last signal
            debug_log(f"UltimateSDUpscale Worker Dynamic - Worker {worker_id} processed {processed_count} images, sending completion signal")
            try:
                run_async_in_server_loop(
                    self._send_worker_complete_signal(multi_job_id, master_url, worker_id),
                    timeout=TILE_SEND_TIMEOUT
                )
            except Exception as e:
                log(f"UltimateSDUpscale Worker Dynamic - Error sending completion signal: {e}")
            
            return (upscaled_image,)
    
    async def _request_image_from_master(self, multi_job_id, master_url, worker_id):
        """Request an image index to process from master in dynamic mode."""
        # Enhanced retries with 404-specific delay, backoff cap, and total timeout to handle init races
        max_retries = 10
        retry_delay = 0.5
        start_time = time.time()
        
        for attempt in range(max_retries):
            # Check total timeout
            if time.time() - start_time > 30:
                log(f"Total request timeout after 30s for worker {worker_id}")
                return None, 0
                
            try:
                session = await get_client_session()
                url = f"{master_url}/distributed/request_image"
                
                async with session.post(url, json={
                    'worker_id': str(worker_id),
                    'multi_job_id': multi_job_id
                }) as response:
                    if response.status == 200:
                        data = await response.json()
                        image_idx = data.get('image_idx')
                        estimated_remaining = data.get('estimated_remaining', 0)
                        return image_idx, estimated_remaining
                    elif response.status == 404:
                        # Special handling for 404 - job not found yet
                        text = await response.text()
                        debug_log(f"Job not found (404), will retry: {text}")
                        await asyncio.sleep(1.0)
                    else:
                        text = await response.text()
                        debug_log(f"Request image failed: {response.status} - {text}")
                        
            except Exception as e:
                if attempt < max_retries - 1:
                    debug_log(f"Retry {attempt + 1}/{max_retries} after error: {e}")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    retry_delay = min(retry_delay, 5.0)  # Cap backoff at 5s
                else:
                    log(f"Failed to request image after {max_retries} attempts: {e}")
                    raise
        
        return None, 0
    
    async def _request_tile_from_master(self, multi_job_id, master_url, worker_id):
        """Request a tile index to process from master in static mode (reusing dynamic infrastructure)."""
        # Reuse the same retry logic as dynamic mode
        max_retries = 10
        retry_delay = 0.5
        start_time = time.time()
        
        for attempt in range(max_retries):
            # Check total timeout
            if time.time() - start_time > 30:
                log(f"Total request timeout after 30s for worker {worker_id}")
                return None, 0
                
            try:
                session = await get_client_session()
                url = f"{master_url}/distributed/request_image"  # Same endpoint
                
                async with session.post(url, json={
                    'worker_id': str(worker_id),
                    'multi_job_id': multi_job_id
                }) as response:
                    if response.status == 200:
                        data = await response.json()
                        tile_idx = data.get('tile_idx')
                        estimated_remaining = data.get('estimated_remaining', 0)
                        return tile_idx, estimated_remaining
                    elif response.status == 404:
                        # Special handling for 404 - job not found yet
                        text = await response.text()
                        debug_log(f"Job not found (404), will retry: {text}")
                        await asyncio.sleep(1.0)
                    else:
                        text = await response.text()
                        debug_log(f"Request tile failed: {response.status} - {text}")
                        
            except Exception as e:
                if attempt < max_retries - 1:
                    debug_log(f"Retry {attempt + 1}/{max_retries} after error: {e}")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    retry_delay = min(retry_delay, 5.0)  # Cap backoff at 5s
                else:
                    log(f"Failed to request tile after {max_retries} attempts: {e}")
                    raise
        
        return None, 0
    
    async def _check_job_status(self, multi_job_id, master_url):
        """Check if job is ready on the master."""
        try:
            session = await get_client_session()
            url = f"{master_url}/distributed/job_status?multi_job_id={multi_job_id}"
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('ready', False)
                return False
        except Exception as e:
            debug_log(f"Job status check failed: {e}")
            return False
    
    async def _send_full_image_to_master(self, image_pil, image_idx, multi_job_id, 
                                        master_url, worker_id, is_last):
        """Send a processed full image back to master in dynamic mode."""
        # Serialize image to PNG
        byte_io = io.BytesIO()
        image_pil.save(byte_io, format='PNG', compress_level=0)
        byte_io.seek(0)
        
        # Prepare form data
        data = aiohttp.FormData()
        data.add_field('multi_job_id', multi_job_id)
        data.add_field('worker_id', str(worker_id))
        data.add_field('image_idx', str(image_idx))
        data.add_field('is_last', str(is_last))
        data.add_field('full_image', byte_io, filename=f'image_{image_idx}.png', 
                      content_type='image/png')
        
        # Retry logic
        max_retries = 5
        retry_delay = 0.5
        
        for attempt in range(max_retries):
            try:
                session = await get_client_session()
                url = f"{master_url}/distributed/submit_image"
                
                async with session.post(url, data=data) as response:
                    response.raise_for_status()
                    debug_log(f"Successfully sent image {image_idx} to master")
                    return
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    debug_log(f"Retry {attempt + 1}/{max_retries} after error: {e}")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    log(f"Failed to send image {image_idx} after {max_retries} attempts: {e}")
                    raise
    
    # Note: Using _send_heartbeat_to_master from usdu_managment.py instead of duplicate
    
    async def _send_worker_complete_signal(self, multi_job_id, master_url, worker_id):
        """Send completion signal to master in dynamic mode."""
        # Send a dummy request with is_last=True
        data = aiohttp.FormData()
        data.add_field('multi_job_id', multi_job_id)
        data.add_field('worker_id', str(worker_id))
        data.add_field('is_last', 'true')
        # No image data - just completion signal
        
        session = await get_client_session()
        url = f"{master_url}/distributed/submit_image"
        
        async with session.post(url, data=data) as response:
            response.raise_for_status()
            debug_log(f"Worker {worker_id} sent completion signal")

    def _determine_processing_mode(self, batch_size: int, num_workers: int, dynamic_threshold: int) -> str:
        """Determines the processing mode based on batch size and worker count."""
        if num_workers == 0:
            return "single_gpu"
        if batch_size <= dynamic_threshold:
            return "static"
        return "dynamic"

    # Note: Using _distribute_tasks from usdu_managment.py instead of duplicate _distribute_items

# Ensure initialization before registering routes
ensure_tile_jobs_initialized()

# Node registration
NODE_CLASS_MAPPINGS = {
    "DeadlineUltimateSDUpscaleDistributed": UltimateSDUpscaleDistributed,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DeadlineUltimateSDUpscaleDistributed": "Deadline Ultimate SD Upscale Distributed (No Upscale)",
}