import asyncio
import time
import json
import copy
import os
import io
from aiohttp import web
import server
from PIL import Image
import numpy as np
import torch

# Import from other utilities
from .logging import debug_log, log
from .network import handle_api_error, get_client_session
from .image import tensor_to_pil
from .security import JOB_TOKEN_FIELD, require_job_token, make_job_headers

# Configure maximum payload size (50MB default, configurable via environment variable)
MAX_PAYLOAD_SIZE = int(os.environ.get('COMFYUI_MAX_PAYLOAD_SIZE', str(50 * 1024 * 1024)))

# Import runtime settings from constants
from .constants import get_worker_heartbeat_grace_timeout


# Unified Job Data Structure Keys
JOB_QUEUE = 'queue'
JOB_MODE = 'mode'
JOB_COMPLETED_TASKS = 'completed_tasks'
JOB_WORKER_STATUS = 'worker_status'
JOB_ASSIGNED_TO_WORKERS = 'assigned_to_workers'
JOB_PENDING_TASKS = 'pending_tasks'
JOB_BATCH_SIZE = 'batch_size'  # For dynamic
JOB_NUM_TILES_PER_IMAGE = 'num_tiles_per_image'  # For static

# Task Types
TASK_TYPE_TILE = 'tile'
TASK_TYPE_IMAGE = 'image'

async def _init_job_queue(multi_job_id, mode, batch_size=None, num_tiles_per_image=None, all_indices=None, enabled_workers=None, task_assignments=None, job_token=None):
    """Unified initialization for job queues in static and dynamic modes."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        if multi_job_id in prompt_server.distributed_pending_tile_jobs:
            debug_log(f"Queue already exists for {multi_job_id}")
            return

        job_data = {
            JOB_QUEUE: asyncio.Queue(),
            JOB_MODE: mode,
            JOB_COMPLETED_TASKS: {},
            JOB_WORKER_STATUS: {w: time.time() for w in enabled_workers or []},
            JOB_ASSIGNED_TO_WORKERS: {w: [] for w in enabled_workers or []},
            JOB_PENDING_TASKS: asyncio.Queue(),
            JOB_TOKEN_FIELD: job_token,
        }

        if mode == 'dynamic':
            job_data[JOB_BATCH_SIZE] = batch_size
            pending_queue = job_data[JOB_PENDING_TASKS]
            for i in all_indices or range(batch_size):
                await pending_queue.put(i)
            debug_log(f"Initialized dynamic queue with {batch_size} pending images")
        elif mode == 'static':
            job_data[JOB_NUM_TILES_PER_IMAGE] = num_tiles_per_image
            pending_queue = job_data[JOB_PENDING_TASKS]
            total_tiles = batch_size * num_tiles_per_image

            if task_assignments is None:
                # Dynamic static mode: workers and master pull all tiles from the pending queue.
                for i in range(total_tiles):
                    await pending_queue.put(i)
                debug_log(f"Initialized static queue with {total_tiles} pending tiles for dynamic distribution")
            else:
                # Legacy static mode: tasks are pre-assigned, so pending starts empty.
                # Timed-out worker tasks are the only ones that should be requeued here.
                debug_log(f"Initialized static queue with pre-assigned tasks and empty pending queue")

            # Keep backward compatibility - if task assignments provided, still track them
            if task_assignments is not None and enabled_workers:
                # task_assignments[0] is for master, 1+ are for workers
                for i, worker_id in enumerate(enabled_workers):
                    if i + 1 < len(task_assignments):
                        job_data[JOB_ASSIGNED_TO_WORKERS][worker_id] = task_assignments[i + 1]
                        debug_log(f"Worker {worker_id} pre-assigned {len(task_assignments[i + 1])} tasks (legacy mode)")
        else:
            raise ValueError(f"Unknown mode: {mode}")

        prompt_server.distributed_pending_tile_jobs[multi_job_id] = job_data

def _distribute_tasks(items: list, num_participants: int) -> list[list[any]]:
    """Distribute a list of items among N participants (master + workers)."""
    if num_participants <= 1:
        return [items]

    items_per_participant = len(items) // num_participants
    remainder = len(items) % num_participants
    assignments = []
    start_idx = 0
    for i in range(num_participants):
        count = items_per_participant + (1 if i < remainder else 0)
        end_idx = start_idx + count
        assignments.append(items[start_idx:end_idx])
        start_idx = end_idx
    return assignments

async def _get_next_task(multi_job_id):
    """Get next task from pending queue (generalized for tiles/images)."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if not job_data or JOB_PENDING_TASKS not in job_data:
            return None
        pending_queue = job_data[JOB_PENDING_TASKS]
    try:
        return await asyncio.wait_for(pending_queue.get(), timeout=1.0)
    except asyncio.TimeoutError:
        return None

async def _drain_results_queue(multi_job_id):
    """Drain pending results from queue and update completed_tasks. Returns count drained."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if not job_data or JOB_QUEUE not in job_data or JOB_COMPLETED_TASKS not in job_data:
            return 0
        q = job_data[JOB_QUEUE]
        completed_tasks = job_data[JOB_COMPLETED_TASKS]

        collected = 0
        while not q.empty():
            try:
                result = q.get_nowait()
                worker_id = result['worker_id']
                is_last = result.get('is_last', False)
                
                if 'image_idx' in result and 'image' in result:
                    task_id = result['image_idx']
                    if task_id not in completed_tasks:
                        completed_tasks[task_id] = result['image']
                        collected += 1
                elif 'tiles' in result:
                    for tile_data in result['tiles']:
                        task_id = tile_data.get('global_idx', tile_data['tile_idx'])
                        if task_id not in completed_tasks:
                            completed_tasks[task_id] = tile_data
                            collected += 1
                elif 'tensor' in result and 'tile_idx' in result:  # Single tile backward compat
                    task_id = result.get('global_idx', result['tile_idx'])
                    if task_id not in completed_tasks:
                        completed_tasks[task_id] = {
                            'tensor': result['tensor'],
                            'tile_idx': result['tile_idx'],
                            'x': result['x'],
                            'y': result['y'],
                            'extracted_width': result['extracted_width'],
                            'extracted_height': result['extracted_height'],
                            'padding': result['padding'],
                            'batch_idx': result.get('batch_idx', 0),
                            'global_idx': task_id
                        }
                        collected += 1

                if is_last:
                    # Track worker completion
                    if worker_id in job_data[JOB_WORKER_STATUS]:
                        del job_data[JOB_WORKER_STATUS][worker_id]
            except asyncio.QueueEmpty:
                break

        return collected

async def _check_and_requeue_timed_out_workers(multi_job_id, total_tasks):
    """Check timed out workers and requeue their tasks. Returns requeued count."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if not job_data:
            return 0

        current_time = time.time()
        requeued_count = 0
        completed_tasks = job_data.get(JOB_COMPLETED_TASKS, {})

        for worker, last_heartbeat in list(job_data.get(JOB_WORKER_STATUS, {}).items()):
            if current_time - last_heartbeat > get_worker_heartbeat_grace_timeout():
                log(f"Worker {worker} timed out")
                for task_id in job_data.get(JOB_ASSIGNED_TO_WORKERS, {}).get(worker, []):
                    if task_id not in completed_tasks:
                        await job_data[JOB_PENDING_TASKS].put(task_id)
                        requeued_count += 1
                if JOB_WORKER_STATUS in job_data:
                    del job_data[JOB_WORKER_STATUS][worker]
                if JOB_ASSIGNED_TO_WORKERS in job_data:
                    job_data[JOB_ASSIGNED_TO_WORKERS][worker] = []

        return requeued_count

async def _get_completed_count(multi_job_id):
    """Get count of completed tasks."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if job_data and JOB_COMPLETED_TASKS in job_data:
            return len(job_data[JOB_COMPLETED_TASKS])
        return 0

async def _mark_task_completed(multi_job_id, task_id, result):
    """Mark a task as completed."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if job_data and JOB_COMPLETED_TASKS in job_data:
            job_data[JOB_COMPLETED_TASKS][task_id] = result

async def _send_heartbeat_to_master(multi_job_id, master_url, worker_id, job_token=None):
    """Send heartbeat to master."""
    try:
        data = {'multi_job_id': multi_job_id, 'worker_id': str(worker_id)}
        session = await get_client_session()
        url = f"{master_url}/distributed/heartbeat"
        async with session.post(url, json=data, headers=make_job_headers(job_token)) as response:
            response.raise_for_status()
    except Exception as e:
        debug_log(f"Heartbeat failed: {e}")

async def _cleanup_job(multi_job_id):
    """Cleanup the job data."""
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        if multi_job_id in prompt_server.distributed_pending_tile_jobs:
            del prompt_server.distributed_pending_tile_jobs[multi_job_id]
            debug_log(f"Cleaned up job {multi_job_id}")

# API Endpoints (generalized)

@server.PromptServer.instance.routes.post("/distributed/request_task")
async def request_task_endpoint(request):
    try:
        data = await request.json()
        worker_id = data.get('worker_id')
        multi_job_id = data.get('multi_job_id')
        
        if not worker_id or not multi_job_id:
            return await handle_api_error(request, "Missing worker_id or multi_job_id", 400)
        
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                mode = job_data.get(JOB_MODE)
                pending_queue = job_data.get(JOB_PENDING_TASKS)
                expected_token = job_data.get(JOB_TOKEN_FIELD)
                if not pending_queue:
                    return await handle_api_error(request, "No pending tasks", 400)
            else:
                return await handle_api_error(request, "Job not found", 404)

        blocked = require_job_token(request, expected_token, data=data)
        if blocked:
            return blocked

        try:
            task_id = await asyncio.wait_for(pending_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return web.json_response({"task_id": None})

        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data:
                return await handle_api_error(request, "Job not found", 404)
            if JOB_ASSIGNED_TO_WORKERS in job_data and worker_id in job_data[JOB_ASSIGNED_TO_WORKERS]:
                job_data[JOB_ASSIGNED_TO_WORKERS][worker_id].append(task_id)
            if JOB_WORKER_STATUS in job_data:
                job_data[JOB_WORKER_STATUS][worker_id] = time.time()
            remaining = pending_queue.qsize()
            debug_log(f"Assigned task {task_id} to worker {worker_id} in {mode} mode")
            return web.json_response({"task_id": task_id, "estimated_remaining": remaining, "mode": mode})
    except Exception as e:
        return await handle_api_error(request, e, 500)


@server.PromptServer.instance.routes.post("/distributed/heartbeat")
async def heartbeat_endpoint(request):
    try:
        data = await request.json()
        worker_id = data.get('worker_id')
        multi_job_id = data.get('multi_job_id')
        
        if not worker_id or not multi_job_id:
            return await handle_api_error(request, "Missing worker_id or multi_job_id", 400)
        
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                blocked = require_job_token(request, job_data.get(JOB_TOKEN_FIELD), data=data)
                if blocked:
                    return blocked
                if JOB_WORKER_STATUS in job_data:
                    job_data[JOB_WORKER_STATUS][worker_id] = time.time()
                    debug_log(f"Heartbeat from worker {worker_id}")
                    return web.json_response({"status": "success"})
                else:
                    return await handle_api_error(request, "Worker status tracking not available", 400)
            else:
                return await handle_api_error(request, "Job not found", 404)
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/distributed/submit_tiles")
async def submit_tiles_endpoint(request):
    """Endpoint for workers to submit processed tiles in static mode."""
    try:
        content_length = request.headers.get('content-length')
        if content_length and int(content_length) > MAX_PAYLOAD_SIZE:
            return await handle_api_error(request, f"Payload too large: {content_length} bytes", 413)
        
        data = await request.post()
        multi_job_id = data.get('multi_job_id')
        worker_id = data.get('worker_id')
        is_last = data.get('is_last', 'False').lower() == 'true'
        
        if multi_job_id is None or worker_id is None:
            return await handle_api_error(request, "Missing multi_job_id or worker_id", 400)

        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data:
                return await handle_api_error(request, "Job not found", 404)
            if JOB_MODE in job_data and job_data[JOB_MODE] != 'static':
                return await handle_api_error(request, "Mode mismatch: expected static mode", 400)
            blocked = require_job_token(request, job_data.get(JOB_TOKEN_FIELD), data=data)
            if blocked:
                return blocked
        
        batch_size = int(data.get('batch_size', 0))
        tiles = []
        
        # Handle completion signal
        if batch_size == 0 and is_last:
            async with prompt_server.distributed_tile_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                    job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                    if JOB_MODE in job_data and job_data[JOB_MODE] != 'static':
                        return await handle_api_error(request, "Mode mismatch: expected static mode", 400)
                    if JOB_QUEUE in job_data:
                        await job_data[JOB_QUEUE].put({
                            'worker_id': worker_id,
                            'is_last': True,
                            'tiles': []
                        })
                        debug_log(f"Received completion signal from worker {worker_id}")
                        return web.json_response({"status": "success"})
        
        # Handle batch tiles with metadata
        if batch_size > 0:
            padding = int(data.get('padding', 32))
            metadata_field = data.get('tiles_metadata')
            if metadata_field:
                if hasattr(metadata_field, 'file'):
                    metadata_str = metadata_field.file.read().decode('utf-8')
                elif isinstance(metadata_field, (bytes, bytearray)):
                    metadata_str = metadata_field.decode('utf-8')
                else:
                    metadata_str = str(metadata_field)
                
                metadata = json.loads(metadata_str)
                if len(metadata) != batch_size:
                    return await handle_api_error(request, "Metadata length mismatch", 400)
                
                tile_data_list = []
                
                for i in range(batch_size):
                    tile_field = data.get(f'tile_{i}')
                    if tile_field is None:
                        continue
                    
                    img_data = tile_field.file.read()
                    img = Image.open(io.BytesIO(img_data)).convert("RGB")
                    img_np = np.array(img).astype(np.float32) / 255.0
                    tensor = torch.from_numpy(img_np)[None,]
                    
                    if i < len(metadata):
                        tile_meta = metadata[i]
                        tile_idx = tile_meta.get('tile_idx', i)
                        tile_info = {
                            'tensor': tensor,
                            'tile_idx': tile_idx,
                            'x': tile_meta['x'],
                            'y': tile_meta['y'],
                            'extracted_width': tile_meta['extracted_width'],
                            'extracted_height': tile_meta['extracted_height'],
                            'padding': padding,
                            'batch_idx': tile_meta.get('batch_idx', 0),
                            'global_idx': tile_meta.get('global_idx', tile_idx)
                        }
                        tile_data_list.append(tile_info)
                    
                tile_data_list.sort(key=lambda x: x['tile_idx'])
                tiles.extend(tile_data_list)
            else:
                # Legacy format
                for i in range(batch_size):
                    tile_field = data.get(f'tile_{i}')
                    if tile_field is None:
                        continue
                    
                    tile_idx = int(data.get(f'tile_{i}_idx', i))
                    x = int(data.get(f'tile_{i}_x', 0))
                    y = int(data.get(f'tile_{i}_y', 0))
                    extracted_width = int(data.get(f'tile_{i}_width', 512))
                    extracted_height = int(data.get(f'tile_{i}_height', 512))
                    
                    img_data = tile_field.file.read()
                    img = Image.open(io.BytesIO(img_data)).convert("RGB")
                    img_np = np.array(img).astype(np.float32) / 255.0
                    tensor = torch.from_numpy(img_np)[None,]
                    
                    tiles.append({
                        'tensor': tensor,
                        'tile_idx': tile_idx,
                        'x': x,
                        'y': y,
                        'extracted_width': extracted_width,
                        'extracted_height': extracted_height,
                        'padding': padding
                    })
        else:
            # Single tile legacy
            image_file = data.get('image')
            if not image_file:
                return await handle_api_error(request, "Missing image data", 400)
                
            tile_idx = int(data.get('tile_idx', 0))
            x = int(data.get('x', 0))
            y = int(data.get('y', 0))
            extracted_width = int(data.get('extracted_width', 512))
            extracted_height = int(data.get('extracted_height', 512))
            padding = int(data.get('padding', 32))
            
            img_data = image_file.file.read()
            img = Image.open(io.BytesIO(img_data)).convert("RGB")
            img_np = np.array(img).astype(np.float32) / 255.0
            tensor = torch.from_numpy(img_np)[None,]
            
            tiles = [{
                'tensor': tensor,
                'tile_idx': tile_idx,
                'x': x,
                'y': y,
                'extracted_width': extracted_width,
                'extracted_height': extracted_height,
                'padding': padding,
                'batch_idx': 0,
                'global_idx': tile_idx
            }]

        # Submit tiles to queue
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                q = job_data[JOB_QUEUE]
                if batch_size > 0 or len(tiles) > 0:
                    await q.put({
                        'worker_id': worker_id,
                        'tiles': tiles,
                        'is_last': is_last
                    })
                else:
                    await q.put({
                        'worker_id': worker_id,
                        'is_last': True,
                        'tiles': []
                    })
                    
                return web.json_response({"status": "success"})
            else:
                return await handle_api_error(request, "Job not found", 404)
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.post("/distributed/submit_image")
async def submit_image_endpoint(request):
    """Endpoint for workers to submit processed images in dynamic mode."""
    try:
        content_length = request.headers.get('content-length')
        if content_length and int(content_length) > MAX_PAYLOAD_SIZE:
            return await handle_api_error(request, f"Payload too large: {content_length} bytes", 413)
        
        data = await request.post()
        multi_job_id = data.get('multi_job_id')
        worker_id = data.get('worker_id')
        is_last = data.get('is_last', 'False').lower() == 'true'
        
        if multi_job_id is None or worker_id is None:
            return await handle_api_error(request, "Missing multi_job_id or worker_id", 400)

        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data:
                return await handle_api_error(request, "Job not found", 404)
            if JOB_MODE in job_data and job_data[JOB_MODE] != 'dynamic':
                return await handle_api_error(request, "Mode mismatch: expected dynamic mode", 400)
            blocked = require_job_token(request, job_data.get(JOB_TOKEN_FIELD), data=data)
            if blocked:
                return blocked
        
        # Handle image submission
        if 'full_image' in data and 'image_idx' in data:
            image_idx = int(data.get('image_idx'))
            img_data = data['full_image'].file.read()
            img = Image.open(io.BytesIO(img_data)).convert("RGB")
            
            debug_log(f"Received full image {image_idx} from worker {worker_id}")
            
            async with prompt_server.distributed_tile_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                    job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                    if JOB_QUEUE in job_data:
                        await job_data[JOB_QUEUE].put({
                            'worker_id': worker_id,
                            'image_idx': image_idx,
                            'image': img,
                            'is_last': is_last
                        })
                        return web.json_response({"status": "success"})
        
        # Handle completion signal (no image data)
        elif is_last:
            async with prompt_server.distributed_tile_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                    job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                    if JOB_MODE in job_data and job_data[JOB_MODE] != 'dynamic':
                        return await handle_api_error(request, "Mode mismatch: expected dynamic mode", 400)
                    if JOB_QUEUE in job_data:
                        await job_data[JOB_QUEUE].put({
                            'worker_id': worker_id,
                            'is_last': True,
                            'tiles': []  # For compatibility
                        })
                        debug_log(f"Received completion signal from worker {worker_id}")
                        return web.json_response({"status": "success"})
        else:
            return await handle_api_error(request, "Missing image data or invalid request", 400)
            
        return await handle_api_error(request, "Job not found", 404)
    except Exception as e:
        return await handle_api_error(request, e, 500)

# Keep legacy endpoint for backward compatibility
@server.PromptServer.instance.routes.post("/distributed/tile_complete")
async def tile_complete_endpoint(request):
    try:
        content_length = request.headers.get('content-length')
        if content_length and int(content_length) > MAX_PAYLOAD_SIZE:
            return await handle_api_error(request, f"Payload too large: {content_length} bytes", 413)
        
        data = await request.post()
        multi_job_id = data.get('multi_job_id')
        worker_id = data.get('worker_id')
        is_last = data.get('is_last', 'False').lower() == 'true'
        
        if multi_job_id is None or worker_id is None:
            return await handle_api_error(request, "Missing multi_job_id or worker_id", 400)

        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data:
                return await handle_api_error(request, "Job not found", 404)
            blocked = require_job_token(request, job_data.get(JOB_TOKEN_FIELD), data=data)
            if blocked:
                return blocked
        
        if 'full_image' in data and 'image_idx' in data:
            image_idx = int(data.get('image_idx'))
            img_data = data['full_image'].file.read()
            img = Image.open(io.BytesIO(img_data)).convert("RGB")
            
            debug_log(f"Received full image {image_idx} from worker {worker_id}")
            
            async with prompt_server.distributed_tile_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                    job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                    if JOB_MODE in job_data and job_data[JOB_MODE] != 'dynamic':
                        return await handle_api_error(request, "Mode mismatch for image submission", 400)
                    if JOB_QUEUE in job_data:
                        await job_data[JOB_QUEUE].put({
                            'worker_id': worker_id,
                            'image_idx': image_idx,
                            'image': img,
                            'is_last': is_last
                        })
                        return web.json_response({"status": "success"})
        
        batch_size = int(data.get('batch_size', 0))
        tiles = []
        
        if batch_size == 0 and is_last:
            async with prompt_server.distributed_tile_jobs_lock:
                if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                    job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                    if JOB_QUEUE in job_data:
                        await job_data[JOB_QUEUE].put({
                            'worker_id': worker_id,
                            'is_last': True,
                            'tiles': []
                        })
                        debug_log(f"Received completion signal from worker {worker_id}")
                        return web.json_response({"status": "success"})
        
        if batch_size > 0:
            padding = int(data.get('padding', 32))
            metadata_field = data.get('tiles_metadata')
            if metadata_field:
                if hasattr(metadata_field, 'file'):
                    metadata_str = metadata_field.file.read().decode('utf-8')
                elif isinstance(metadata_field, (bytes, bytearray)):
                    metadata_str = metadata_field.decode('utf-8')
                else:
                    metadata_str = str(metadata_field)
                
                metadata = json.loads(metadata_str)
                if len(metadata) != batch_size:
                    return await handle_api_error(request, "Metadata length mismatch", 400)
                
                tile_data_list = []
                
                for i in range(batch_size):
                    tile_field = data.get(f'tile_{i}')
                    if tile_field is None:
                        continue
                    
                    img_data = tile_field.file.read()
                    img = Image.open(io.BytesIO(img_data)).convert("RGB")
                    img_np = np.array(img).astype(np.float32) / 255.0
                    tensor = torch.from_numpy(img_np)[None,]
                    
                    if i < len(metadata):
                        tile_meta = metadata[i]
                        tile_idx = tile_meta.get('tile_idx', i)
                        tile_info = {
                            'tensor': tensor,
                            'tile_idx': tile_idx,
                            'x': tile_meta['x'],
                            'y': tile_meta['y'],
                            'extracted_width': tile_meta['extracted_width'],
                            'extracted_height': tile_meta['extracted_height'],
                            'padding': padding,
                            'batch_idx': tile_meta.get('batch_idx', 0),
                            'global_idx': tile_meta.get('global_idx', tile_idx)
                        }
                        tile_data_list.append(tile_info)
                    
                tile_data_list.sort(key=lambda x: x['tile_idx'])
                tiles.extend(tile_data_list)
            else:
                # Legacy
                for i in range(batch_size):
                    tile_field = data.get(f'tile_{i}')
                    if tile_field is None:
                        continue
                    
                    tile_idx = int(data.get(f'tile_{i}_idx', i))
                    x = int(data.get(f'tile_{i}_x', 0))
                    y = int(data.get(f'tile_{i}_y', 0))
                    extracted_width = int(data.get(f'tile_{i}_width', 512))
                    extracted_height = int(data.get(f'tile_{i}_height', 512))
                    
                    img_data = tile_field.file.read()
                    img = Image.open(io.BytesIO(img_data)).convert("RGB")
                    img_np = np.array(img).astype(np.float32) / 255.0
                    tensor = torch.from_numpy(img_np)[None,]
                    
                    tiles.append({
                        'tensor': tensor,
                        'tile_idx': tile_idx,
                        'x': x,
                        'y': y,
                        'extracted_width': extracted_width,
                        'extracted_height': extracted_height,
                        'padding': padding
                    })
        else:
            # Single tile legacy
            image_file = data.get('image')
            if not image_file:
                return await handle_api_error(request, "Missing image data", 400)
                
            tile_idx = int(data.get('tile_idx', 0))
            x = int(data.get('x', 0))
            y = int(data.get('y', 0))
            extracted_width = int(data.get('extracted_width', 512))
            extracted_height = int(data.get('extracted_height', 512))
            padding = int(data.get('padding', 32))
            
            img_data = image_file.file.read()
            img = Image.open(io.BytesIO(img_data)).convert("RGB")
            img_np = np.array(img).astype(np.float32) / 255.0
            tensor = torch.from_numpy(img_np)[None,]
            
            tiles = [{
                'tensor': tensor,
                'tile_idx': tile_idx,
                'x': x,
                'y': y,
                'extracted_width': extracted_width,
                'extracted_height': extracted_height,
                'padding': padding,
                'batch_idx': 0,
                'global_idx': tile_idx
            }]

        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                if JOB_MODE in job_data and job_data[JOB_MODE] != 'static':
                    return await handle_api_error(request, "Mode mismatch for tile submission", 400)
                
                q = job_data[JOB_QUEUE]
                if batch_size > 0 or len(tiles) > 0:
                    await q.put({
                        'worker_id': worker_id,
                        'tiles': tiles,
                        'is_last': is_last
                    })
                else:
                    await q.put({
                        'worker_id': worker_id,
                        'is_last': True,
                        'tiles': []
                    })
                    
                return web.json_response({"status": "success"})
            else:
                return await handle_api_error(request, "Job not found", 404)
    except Exception as e:
        return await handle_api_error(request, e, 500)



# Helper functions for shallow copying conditioning without duplicating models
def clone_control_chain(control, clone_hint=True):
    """Shallow copy the ControlNet chain, optionally cloning hints but sharing models."""
    if control is None:
        return None
    new_control = copy.copy(control)  # Shallow copy (shares model)
    if clone_hint and hasattr(control, 'cond_hint_original'):
        new_control.cond_hint_original = control.cond_hint_original.clone()
    if hasattr(control, 'previous_controlnet'):
        new_control.previous_controlnet = clone_control_chain(control.previous_controlnet, clone_hint)
    return new_control

def clone_conditioning(cond_list, clone_hints=True):
    """Clone conditioning without duplicating ControlNet models."""
    new_cond = []
    for emb, cond_dict in cond_list:
        new_emb = emb.clone() if emb is not None else None
        new_dict = cond_dict.copy()
        if 'control' in new_dict:
            new_dict['control'] = clone_control_chain(new_dict['control'], clone_hints)
        if 'mask' in new_dict:
            new_dict['mask'] = new_dict['mask'].clone()
        # Handle other potential fields if needed
        if 'pooled_output' in new_dict:
            new_dict['pooled_output'] = new_dict['pooled_output'].clone()
        if 'area' in new_dict:
            new_dict['area'] = new_dict['area'][:]  # Shallow copy list/tuple
        new_cond.append([new_emb, new_dict])
    return new_cond

# Helper function to ensure persistent state is initialized
def ensure_tile_jobs_initialized():
    """Ensure tile job storage is initialized on the server instance."""
    prompt_server = server.PromptServer.instance
    if not hasattr(prompt_server, 'distributed_pending_tile_jobs'):
        debug_log("Initializing persistent tile job queue on server instance.")
        prompt_server.distributed_pending_tile_jobs = {}
        prompt_server.distributed_tile_jobs_lock = asyncio.Lock()
    else:
        # Clean up any legacy queue structures that don't have the 'mode' field
        # (Should be rare after fixes, but keep for safety)
        to_remove = [job_id for job_id, job_data in prompt_server.distributed_pending_tile_jobs.items()
                     if not isinstance(job_data, dict) or 'mode' not in job_data]
        for job_id in to_remove:
            debug_log(f"Removing legacy queue structure for job {job_id}")
            del prompt_server.distributed_pending_tile_jobs[job_id]
    return prompt_server

# API Endpoint for tile completion
@server.PromptServer.instance.routes.post("/distributed/request_image")
async def request_image_endpoint(request):
    """Endpoint for workers to request tasks (images in dynamic mode, tiles in static mode)."""
    try:
        data = await request.json()
        worker_id = data.get('worker_id')
        multi_job_id = data.get('multi_job_id')
        
        if not worker_id or not multi_job_id:
            return await handle_api_error(request, "Missing worker_id or multi_job_id", 400)
        
        prompt_server = ensure_tile_jobs_initialized()
        async with prompt_server.distributed_tile_jobs_lock:
            if multi_job_id in prompt_server.distributed_pending_tile_jobs:
                job_data = prompt_server.distributed_pending_tile_jobs[multi_job_id]
                if not isinstance(job_data, dict) or 'mode' not in job_data:
                    return await handle_api_error(request, "Invalid job data structure", 500)
                
                mode = job_data['mode']
                expected_token = job_data.get(JOB_TOKEN_FIELD)
                
                # Handle both dynamic and static modes
                if mode == 'dynamic' and 'pending_images' in job_data:
                    pending_queue = job_data['pending_images']
                elif mode == 'static' and JOB_PENDING_TASKS in job_data:
                    pending_queue = job_data[JOB_PENDING_TASKS]
                else:
                    return await handle_api_error(request, f"Invalid {mode} mode configuration", 400)
            else:
                return await handle_api_error(request, "Job not found", 404)

        blocked = require_job_token(request, expected_token, data=data)
        if blocked:
            return blocked

        try:
            task_idx = await asyncio.wait_for(pending_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            if mode == 'dynamic':
                return web.json_response({"image_idx": None})
            return web.json_response({"tile_idx": None})

        async with prompt_server.distributed_tile_jobs_lock:
            job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
            if not job_data:
                return await handle_api_error(request, "Job not found", 404)
            if 'assigned_to_workers' in job_data and worker_id in job_data['assigned_to_workers']:
                job_data['assigned_to_workers'][worker_id].append(task_idx)
            if 'worker_status' in job_data:
                job_data['worker_status'][worker_id] = time.time()
            remaining = pending_queue.qsize()

        if mode == 'dynamic':
            debug_log(f"UltimateSDUpscale API - Assigned image {task_idx} to worker {worker_id}")
            return web.json_response({"image_idx": task_idx, "estimated_remaining": remaining})
        debug_log(f"UltimateSDUpscale API - Assigned tile {task_idx} to worker {worker_id}")
        return web.json_response({"tile_idx": task_idx, "estimated_remaining": remaining})
    except Exception as e:
        return await handle_api_error(request, e, 500)

@server.PromptServer.instance.routes.get("/distributed/job_status")
async def job_status_endpoint(request):
    """Endpoint to check if a job is ready."""
    multi_job_id = request.query.get('multi_job_id')
    if not multi_job_id:
        return web.json_response({"ready": False})
    prompt_server = ensure_tile_jobs_initialized()
    async with prompt_server.distributed_tile_jobs_lock:
        job_data = prompt_server.distributed_pending_tile_jobs.get(multi_job_id)
        if job_data:
            blocked = require_job_token(request, job_data.get(JOB_TOKEN_FIELD))
            if blocked:
                return blocked
        ready = bool(job_data and isinstance(job_data, dict) and 'queue' in job_data)
        return web.json_response({"ready": ready})


