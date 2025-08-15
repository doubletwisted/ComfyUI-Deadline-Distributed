import { BUTTON_STYLES, TIMEOUTS } from './constants.js';

export async function handleWorkerOperation(extension, button, operation, successText, errorText) {
    const originalText = button.textContent;
    const originalStyle = button.style.cssText;
    
    button.textContent = operation.loadingText;
    button.disabled = true;
    
    try {
        const urlsToProcess = extension.enabledWorkers.map(w => ({ 
            name: w.name, 
            url: extension.getWorkerUrl(w)
        }));
        
        if (urlsToProcess.length === 0) {
            button.textContent = "No Workers";
            button.style.backgroundColor = "#c04c4c";
            setTimeout(() => {
                button.textContent = originalText;
                button.style.cssText = originalStyle;
                button.disabled = false;
            }, TIMEOUTS.BUTTON_RESET);
            return;
        }
        
        const promises = urlsToProcess.map(target =>
            fetch(`${target.url}${operation.endpoint}`, { 
                method: 'POST', 
                mode: 'cors'
            })
                .then(response => ({ ok: response.ok, name: target.name }))
                .catch(() => ({ ok: false, name: target.name }))
        );
        
        const results = await Promise.all(promises);
        const failures = results.filter(r => !r.ok);
        
        if (failures.length === 0) {
            button.textContent = successText;
            button.style.backgroundColor = BUTTON_STYLES.success.split(':')[1].trim().replace(';', '');
            if (operation.onSuccess) operation.onSuccess();
        } else {
            button.textContent = errorText;
            button.style.backgroundColor = BUTTON_STYLES.error.split(':')[1].trim().replace(';', '');
            extension.log(`${operation.name} failed on: ${failures.map(f => f.name).join(", ")}`, "error");
        }
        
        setTimeout(() => {
            button.textContent = originalText;
            button.style.cssText = originalStyle;
        }, TIMEOUTS.BUTTON_RESET);
    } finally {
        button.disabled = false;
    }
}

export async function handleInterruptWorkers(extension, button) {
    return handleWorkerOperation(extension, button, {
        name: "Interrupt",
        endpoint: "/interrupt",
        loadingText: "Interrupting...",
        onSuccess: () => setTimeout(() => extension.checkAllWorkerStatuses(), TIMEOUTS.POST_ACTION_DELAY)
    }, "Interrupted!", "Error! See Console");
}

export async function handleClearMemory(extension, button) {
    return handleWorkerOperation(extension, button, {
        name: "Clear memory",
        endpoint: "/distributed/clear_memory",
        loadingText: "Clearing..."
    }, "Success!", "Error! See Console");
}

export function findNodesByClass(apiPrompt, className) {
    return Object.entries(apiPrompt)
        .filter(([, nodeData]) => nodeData.class_type === className)
        .map(([nodeId, nodeData]) => ({ id: nodeId, data: nodeData }));
}

/**
 * Find all image references in the workflow
 * Looks for inputs named "image" that contain filename strings
 */
export function findImageReferences(extension, apiPrompt) {
    const images = new Map();
    // Updated regex to handle:
    // - Standard files: "image.png"
    // - Subfolder files: "subfolder/image.png"
    // - ComfyUI special format: "clipspace/file.png [input]"
    // - Video files: "video.mp4", "animation.avi", etc.
    const imageExtensions = /\.(png|jpg|jpeg|gif|webp|bmp|mp4|avi|mov|mkv|webm)(\s*\[\w+\])?$/i;
    
    for (const [nodeId, node] of Object.entries(apiPrompt)) {
        // Check for both 'image' and 'video' inputs
        const mediaInputs = [];
        if (node.inputs && node.inputs.image) {
            mediaInputs.push(node.inputs.image);
        }
        if (node.inputs && node.inputs.video) {
            mediaInputs.push(node.inputs.video);
        }
        
        for (const mediaValue of mediaInputs) {
            if (typeof mediaValue === 'string') {
                // Clean special suffixes like [input] or [output]
                const cleanValue = mediaValue.replace(/\s*\[\w+\]$/, '').trim();
                // Normalize to forward slashes so subfolder/filename can be derived reliably
                const normalizedValue = cleanValue.replace(/\\/g, '/');
                if (imageExtensions.test(normalizedValue)) {
                    images.set(normalizedValue, {
                        nodeId,
                        nodeType: node.class_type,
                        inputName: 'image'  // Keep as 'image' for compatibility
                    });
                    extension.log(`Found media reference: ${normalizedValue} in node ${nodeId} (${node.class_type})`, "debug");
                }
            }
        }
    }
    
    return images;
}

/**
 * Find only upstream nodes (inputs) for distributed collector nodes
 * This is used for workers to avoid executing downstream nodes like SaveImage
 * @param {Object} apiPrompt - The API prompt containing the workflow
 * @param {Array<string>} collectorIds - Array of collector node IDs
 * @returns {Set<string>} Set of node IDs that feed into collectors
 */
export function findCollectorUpstreamNodes(apiPrompt, collectorIds) {
    const connected = new Set(collectorIds); // Include all collectors
    const toProcess = [...collectorIds];
    
    // Only traverse upstream (inputs)
    while (toProcess.length > 0) {
        const nodeId = toProcess.pop();
        const node = apiPrompt[nodeId];
        
        // Traverse upstream (inputs) only
        if (node && node.inputs) {
            for (const [inputName, inputValue] of Object.entries(node.inputs)) {
                if (Array.isArray(inputValue) && inputValue.length === 2) {
                    const sourceNodeId = String(inputValue[0]);
                    if (!connected.has(sourceNodeId)) {
                        connected.add(sourceNodeId);
                        toProcess.push(sourceNodeId);
                    }
                }
            }
        }
    }
    
    return connected;
}

/**
 * Prune workflow to only include nodes connected to distributed nodes
 * @param {Object} apiPrompt - The full workflow API prompt
 * @param {Array} distributedNodes - Array of distributed nodes (optional, will find if not provided)
 * @returns {Object} Pruned API prompt with only required nodes
 */
export function pruneWorkflowForWorker(extension, apiPrompt, distributedNodes = null) {
    // Find all distributed nodes if not provided
    if (!distributedNodes) {
            const collectorNodes = findNodesByClass(apiPrompt, "DeadlineDistributedCollector");
    const upscaleNodes = findNodesByClass(apiPrompt, "DeadlineUltimateSDUpscaleDistributed");
        distributedNodes = [...collectorNodes, ...upscaleNodes];
    }
    
    if (distributedNodes.length === 0) {
        // No distributed nodes, return full workflow
        return apiPrompt;
    }
    
    // Get all nodes connected to distributed nodes
    const distributedIds = distributedNodes.map(node => node.id);
    
    // For workers, only include upstream nodes (this removes ALL downstream nodes after collectors)
    const connectedNodes = findCollectorUpstreamNodes(apiPrompt, distributedIds);
    
    extension.log(`Pruning workflow: keeping ${connectedNodes.size} of ${Object.keys(apiPrompt).length} nodes (removed all downstream nodes)`, "debug");
    
    // Create pruned prompt with only required nodes
    const prunedPrompt = {};
    for (const nodeId of connectedNodes) {
        prunedPrompt[nodeId] = JSON.parse(JSON.stringify(apiPrompt[nodeId]));
    }
    
    // Check if any distributed node has downstream SaveImage nodes that were removed
    // If so, add a PreviewImage node after the collector
    for (const distNode of distributedNodes) {
        const distNodeId = distNode.id;
        
        // Check if this distributed node had any downstream nodes in the original workflow
        const originalOutputMap = new Map();
        for (const [nodeId, node] of Object.entries(apiPrompt)) {
            if (node.inputs) {
                for (const [inputName, inputValue] of Object.entries(node.inputs)) {
                    if (Array.isArray(inputValue) && inputValue.length === 2 && String(inputValue[0]) === distNodeId) {
                        if (!originalOutputMap.has(distNodeId)) {
                            originalOutputMap.set(distNodeId, []);
                        }
                        originalOutputMap.get(distNodeId).push({nodeId, inputName});
                    }
                }
            }
        }
        
        // If this distributed node had downstream nodes that were removed, add a PreviewImage
        if (originalOutputMap.has(distNodeId) && originalOutputMap.get(distNodeId).length > 0) {
            // Generate unique numeric ID: max existing numeric key +1
            const existingIds = Object.keys(prunedPrompt)
                .filter(k => !isNaN(parseInt(k)))
                .map(k => parseInt(k));
            const maxId = existingIds.length > 0 ? Math.max(...existingIds) : 0;
            const previewNodeId = String(maxId + 1);
            
            // Add PreviewImage node connected to the distributed node
            prunedPrompt[previewNodeId] = {
                inputs: {
                    images: [distNodeId, 0]  // Connect to first output of distributed node
                },
                class_type: "PreviewImage",
                _meta: {
                    title: "Preview Image (auto-added)"
                }
            };
            
            extension.log(`Added PreviewImage node ${previewNodeId} after distributed node ${distNodeId} for worker`, "debug");
        }
    }
    
    return prunedPrompt;
}

/**
 * Check if a node has an upstream node of a specific type
 * @param {Object} apiPrompt - The workflow API prompt
 * @param {string} nodeId - The node to check
 * @param {string} upstreamType - The class_type to look for upstream
 * @returns {boolean} True if an upstream node of the specified type exists
 */
export function hasUpstreamNode(apiPrompt, nodeId, upstreamType) {
    const visited = new Set();
    const toProcess = [nodeId];
    
    while (toProcess.length > 0) {
        const currentId = toProcess.pop();
        if (visited.has(currentId)) continue;
        visited.add(currentId);
        
        const node = apiPrompt[currentId];
        if (!node) continue;
        
        // Check inputs for upstream connections
        if (node.inputs) {
            for (const [inputName, inputValue] of Object.entries(node.inputs)) {
                if (Array.isArray(inputValue) && inputValue.length === 2) {
                    const sourceNodeId = String(inputValue[0]);
                    const sourceNode = apiPrompt[sourceNodeId];
                    
                    if (sourceNode && sourceNode.class_type === upstreamType) {
                        return true;
                    }
                    
                    if (!visited.has(sourceNodeId)) {
                        toProcess.push(sourceNodeId);
                    }
                }
            }
        }
    }
    
    return false;
}

/**
 * Get system information from a worker
 * @param {string} workerUrl - The worker URL
 * @returns {Promise<Object>} System information including platform details
 */
export async function getWorkerSystemInfo(workerUrl) {
    try {
        const response = await fetch(`${workerUrl}/distributed/system_info`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        return await response.json();
    } catch (error) {
        console.warn(`Failed to get system info from ${workerUrl}:`, error);
        // Return sensible defaults
        return {
            platform: {
                os_name: 'posix',  // Assume Linux
                path_separator: '/',
                system: 'Linux'
            }
        };
    }
}

// Cache system info to avoid repeated calls
const systemInfoCache = new Map();

/**
 * Get cached system information from a worker
 * @param {string} workerUrl - The worker URL
 * @returns {Promise<Object>} Cached or fresh system information
 */
export async function getCachedWorkerSystemInfo(workerUrl) {
    if (systemInfoCache.has(workerUrl)) {
        return systemInfoCache.get(workerUrl);
    }
    
    const info = await getWorkerSystemInfo(workerUrl);
    systemInfoCache.set(workerUrl, info);
    return info;
}

/**
 * Clear the system info cache
 */
export function clearSystemInfoCache() {
    systemInfoCache.clear();
}