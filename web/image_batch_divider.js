import { app } from "/scripts/app.js";

app.registerExtension({
    name: "Distributed.ImageBatchDivider",
    async nodeCreated(node) {
        if (node.comfyClass === "ImageBatchDivider" || node.comfyClass === "DeadlineImageBatchDivider") {
            try {
                const updateOutputs = () => {
                    if (!node.widgets) return;
                    
                    const divideByWidget = node.widgets.find(w => w.name === "divide_by");
                    if (!divideByWidget) return;
                    
                    const divideBy = parseInt(divideByWidget.value, 10) || 1;
                    const totalOutputs = divideBy;  // Direct divide by value
                    
                    // Ensure outputs array exists
                    if (!node.outputs) node.outputs = [];
                    
                    // Remove excess outputs
                    while (node.outputs.length > totalOutputs) {
                        node.removeOutput(node.outputs.length - 1);
                    }
                    
                    // Add missing outputs
                    while (node.outputs.length < totalOutputs) {
                        const outputIndex = node.outputs.length + 1;
                        node.addOutput(`batch_${outputIndex}`, "IMAGE");
                    }
                    
                    if (node.setDirty) node.setDirty(true);  // Refresh canvas
                };
                
                // Initial update with delay to allow workflow loading
                setTimeout(updateOutputs, 200);
                
                // Find the widget and set up responsive handlers
                const divideByWidget = node.widgets.find(w => w.name === "divide_by");
                if (divideByWidget) {
                    // Override callback for immediate trigger on value set
                    const originalCallback = divideByWidget.callback;
                    divideByWidget.callback = (value) => {
                        updateOutputs();
                        if (originalCallback) originalCallback.call(divideByWidget, value);  // Preserve 'this' context
                    };
                    
                    // Add event listener for real-time input changes (e.g., typing/dragging)
                    if (divideByWidget.inputEl) {
                        divideByWidget.inputEl.addEventListener('input', updateOutputs);
                    }
                    
                    // Lightweight MutationObserver as fallback (observe attributes on widget element if available)
                    const observer = new MutationObserver(updateOutputs);
                    if (divideByWidget.element) {
                        observer.observe(divideByWidget.element, { attributes: true, childList: true, subtree: true });
                    }
                    
                    // Store cleanup function
                    node._batchDividerCleanup = () => {
                        observer.disconnect();
                        if (divideByWidget.inputEl) {
                            divideByWidget.inputEl.removeEventListener('input', updateOutputs);
                        }
                        divideByWidget.callback = originalCallback;  // Restore original
                    };
                }
                
                // Add post-configure hook for reliable workflow loading
                const originalConfigure = node.configure;
                node.configure = function(data) {
                    const result = originalConfigure ? originalConfigure.call(this, data) : undefined;
                    updateOutputs();  // Re-run after config load
                    return result;
                };
            } catch (error) {
                console.error("Error in ImageBatchDivider extension:", error);
            }
        }
    },
    
    nodeBeforeRemove(node) {
        if ((node.comfyClass === "ImageBatchDivider" || node.comfyClass === "DeadlineImageBatchDivider") && node._batchDividerCleanup) {
            node._batchDividerCleanup();
        }
    }
});
