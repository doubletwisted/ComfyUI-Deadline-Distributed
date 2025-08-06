import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { DistributedUI } from './ui.js';

import { createStateManager } from './stateManager.js';
import { createApiClient } from './apiClient.js';
import { renderSidebarContent } from './sidebarRenderer.js';
import { handleWorkerOperation, handleInterruptWorkers, handleClearMemory } from './workerUtils.js';
import { setupInterceptor, executeParallelDistributed } from './executionUtils.js';
import { BUTTON_STYLES, PULSE_ANIMATION_CSS, TIMEOUTS, STATUS_COLORS } from './constants.js';

class DistributedExtension {
    constructor() {
        this.config = null;
        this.originalQueuePrompt = api.queuePrompt.bind(api);
        this.statusCheckInterval = null;
        this.logAutoRefreshInterval = null;
        this.masterSettingsExpanded = false;
        this.app = app; // Store app reference for toast notifications
        
        // Initialize centralized state
        this.state = createStateManager();
        
        // Initialize UI component factory
        this.ui = new DistributedUI();
        
        // Initialize API client
        this.api = createApiClient(window.location.origin);
        
        // Initialize status check timeout reference
        this.statusCheckTimeout = null;
        
        // Initialize abort controller for status checks
        this.statusCheckAbortController = null;

        // Inject CSS for pulsing animation
        this.injectStyles();

        this.loadConfig().then(async () => {
            this.registerSidebarTab();
            this.setupInterceptor();
            // Don't start polling until panel opens
            // this.startStatusChecking();
            this.loadManagedWorkers();
            // Detect master IP after everything is set up
            this.detectMasterIP();
        });
    }

    // Debug logging helpers
    log(message, level = "info") {
        if (level === "debug" && !this.config?.settings?.debug) return;
        if (level === "error") {
            console.error(`[Distributed] ${message}`);
        } else {
            console.log(`[Distributed] ${message}`);
        }
    }

    // Generate UUID with fallback for non-secure contexts
    generateUUID() {
        if (crypto.randomUUID) {
            return crypto.randomUUID();
        }
        // Fallback for non-secure contexts
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
            const r = Math.random() * 16 | 0;
            const v = c == 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    injectStyles() {
        const styleId = 'distributed-styles';
        if (!document.getElementById(styleId)) {
            const style = document.createElement('style');
            style.id = styleId;
            style.textContent = PULSE_ANIMATION_CSS;
            document.head.appendChild(style);
        }
    }

    // --- State & Config Management (Single Source of Truth) ---

    get enabledWorkers() {
        return this.config?.workers?.filter(w => w.enabled) || [];
    }

    get isEnabled() {
        return this.enabledWorkers.length > 0;
    }

    async loadConfig() {
        try {
            this.config = await this.api.getConfig();
            this.log("Loaded config: " + JSON.stringify(this.config), "debug");
            
            // Ensure default flag values
            if (!this.config.settings) {
                this.config.settings = {};
            }
            if (this.config.settings.has_auto_populated_workers === undefined) {
                this.config.settings.has_auto_populated_workers = false;
            }
            
            // Load stored master CUDA device
            this.masterCudaDevice = this.config?.master?.cuda_device ?? undefined;
            
            // Sync to state
            if (this.config.workers) {
                this.config.workers.forEach(w => {
                    this.state.updateWorker(w.id, { enabled: w.enabled });
                });
            }
        } catch (error) {
            this.log("Failed to load config: " + error.message, "error");
            this.config = { workers: [], settings: { has_auto_populated_workers: false } };
        }
    }

    async updateWorkerEnabled(workerId, enabled) {
        const worker = this.config.workers.find(w => w.id === workerId);
        if (worker) {
            worker.enabled = enabled;
            this.state.updateWorker(workerId, { enabled });

            // Immediately update status dot based on enabled state
            const statusDot = document.getElementById(`status-${workerId}`);
            if (statusDot) {
                if (enabled) {
                    // Enabled: Start with checking state and trigger check
                    this.ui.updateStatusDot(workerId, STATUS_COLORS.OFFLINE_RED, "Checking status...", true);
                    setTimeout(() => this.checkWorkerStatus(worker), TIMEOUTS.STATUS_CHECK_DELAY);
                } else {
                    // Disabled: Set to gray
                    this.ui.updateStatusDot(workerId, STATUS_COLORS.DISABLED_GRAY, "Disabled", false);
                }
            }
        }
        
        try {
            await this.api.updateWorker(workerId, { enabled });
        } catch (error) {
            this.log("Error updating worker: " + error.message, "error");
        }
    }

    async _updateSetting(key, value) {
        // Update local config
        if (!this.config.settings) {
            this.config.settings = {};
        }
        this.config.settings[key] = value;
        
        try {
            await this.api.updateSetting(key, value);
            
            app.extensionManager.toast.add({
                severity: "success",
                summary: "Setting Updated",
                detail: `${key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())} ${value ? 'enabled' : 'disabled'}`,
                life: 2000
            });
        } catch (error) {
            this.log(`Error updating setting '${key}': ${error.message}`, "error");
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Setting Update Failed",
                detail: error.message,
                life: 3000
            });
        }
    }
    
    async updateDeadlineSettings(priority, pool, group) {
        try {
            // Update local config immediately for better UX
            if (!this.config.deadline) {
                this.config.deadline = {};
            }
            this.config.deadline.priority = priority;
            this.config.deadline.pool = pool;
            this.config.deadline.group = group;
            
            // Persist to backend
            await this.api.updateDeadline({ priority, pool, group });
            this.log(`Updated deadline settings: Priority=${priority}, Pool=${pool}, Group=${group}`, "info");
            
            app.extensionManager.toast.add({
                severity: "success",
                summary: "Deadline Settings Updated",
                detail: `Priority: ${priority}, Pool: ${pool}, Group: ${group}`,
                life: 2000
            });
        } catch (error) {
            this.log(`Error updating deadline settings: ${error.message}`, "error");
            
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Update Failed",
                detail: `Failed to update deadline settings: ${error.message}`,
                life: 3000
            });
        }
    }

    // --- UI Rendering ---

    registerSidebarTab() {
        app.extensionManager.registerSidebarTab({
            id: "distributed",
            icon: "pi pi-server",
            title: "Distributed",
            tooltip: "Distributed Control Panel",
            type: "custom",
            render: (el) => {
                this.panelElement = el;
                this.onPanelOpen();
                return renderSidebarContent(this, el);
            },
            destroy: () => {
                this.onPanelClose();
            }
        });
    }
    
    onPanelOpen() {
        this.log("Panel opened - starting status polling", "debug");
        if (!this.statusCheckTimeout) {
            this.checkAllWorkerStatuses();
        }
    }
    
    onPanelClose() {
        this.log("Panel closed - stopping status polling", "debug");
        
        // Cancel any pending status checks
        if (this.statusCheckAbortController) {
            this.statusCheckAbortController.abort();
            this.statusCheckAbortController = null;
        }
        
        // Clear the timeout
        if (this.statusCheckTimeout) {
            clearTimeout(this.statusCheckTimeout);
            this.statusCheckTimeout = null;
        }
        
        this.panelElement = null;
    }

    updateSummary() {
        const summaryEl = document.getElementById('distributed-summary');
        if (summaryEl) {
            // Count active workers from state
            const activeWorkers = this.config.workers.filter(w => {
                const status = this.state.getWorkerStatus(w.id);
                return status.online;
            }).length;
            const totalGPUs = activeWorkers + 1;
            if (this.isEnabled) {
                summaryEl.textContent = `If Collector node is present, total generation = (${totalGPUs} GPUs × Batch Size)`;
            } else {
                summaryEl.textContent = "Only the master GPU will be used.";
            }
        }
    }

    // --- Core Logic & Execution ---

    setupInterceptor() {
        setupInterceptor(this);
    }

    async executeParallelDistributed(promptWrapper) {
        return executeParallelDistributed(this, promptWrapper);
    }

    startStatusChecking() {
        this.checkAllWorkerStatuses();
    }

    async checkAllWorkerStatuses() {
        // Don't continue if panel is closed
        if (!this.panelElement) return;
        
        // Create new abort controller for this round of checks
        this.statusCheckAbortController = new AbortController();
        
        // Periodically reload config to pick up new Deadline workers (every 10 seconds)
        if (!this.lastConfigReload || Date.now() - this.lastConfigReload > 5000) {
            await this.loadConfig();
            this.lastConfigReload = Date.now();
        }
        
        // Check master status
        this.checkMasterStatus();
        
        if (!this.config || !this.config.workers) return;
        
        for (const worker of this.config.workers) {
            // Check status for enabled workers OR workers that are launching
            if (worker.enabled || this.state.isWorkerLaunching(worker.id)) {
                this.checkWorkerStatus(worker);
            }
        }
        
        // Determine next interval based on current state
        let isActive = this.state.getMasterStatus() === 'processing';  // Master is busy

        // Check workers for activity
        this.config.workers.forEach(worker => {
            const ws = this.state.getWorker(worker.id);  // Get worker state
            if (ws.launching || ws.status?.processing) {  // Launching or processing
                isActive = true;
            }
        });

        // Set next delay: 1s if active, 5s if idle
        const nextInterval = isActive ? 1000 : 5000;

        // Schedule the next check
        this.statusCheckTimeout = setTimeout(() => this.checkAllWorkerStatuses(), nextInterval);
    }

    async checkMasterStatus() {
        try {
            const response = await fetch(`${window.location.origin}/prompt`, {
                method: 'GET',
                signal: AbortSignal.timeout(TIMEOUTS.STATUS_CHECK)
            });
            
            if (response.ok) {
                const data = await response.json();
                const queueRemaining = data.exec_info?.queue_remaining || 0;
                const isProcessing = queueRemaining > 0;
                
                // Update master status in state
                this.state.setMasterStatus(isProcessing ? 'processing' : 'online');
                
                // Update master status dot
                const statusDot = document.getElementById('master-status');
                if (statusDot) {
                    if (isProcessing) {
                        statusDot.style.backgroundColor = "#f0ad4e";
                        statusDot.title = `Processing (${queueRemaining} in queue)`;
                    } else {
                        statusDot.style.backgroundColor = "#4CAF50";
                        statusDot.title = "Online";
                    }
                }
            }
        } catch (error) {
            // Master is always online (we're running on it), so keep it green
            const statusDot = document.getElementById('master-status');
            if (statusDot) {
                statusDot.style.backgroundColor = "#4CAF50";
                statusDot.title = "Online";
            }
        }
    }

    // Helper to build worker URL
    getWorkerUrl(worker, endpoint = '') {
        const host = worker.host || window.location.hostname;
        
        // Cloud workers always use HTTPS
        const isCloud = worker.type === 'cloud';
        
        // Detect if we're running on Runpod (for local workers on Runpod infrastructure)
        const isRunpodProxy = host.endsWith('.proxy.runpod.net');
        
        // For local workers on Runpod, construct the port-specific proxy URL
        let finalHost = host;
        if (!worker.host && isRunpodProxy) {
            const match = host.match(/^(.*)\.proxy\.runpod\.net$/);
            if (match) {
                const podId = match[1];
                const domain = 'proxy.runpod.net';
                finalHost = `${podId}-${worker.port}.${domain}`;
            } else {
                // Fallback or log error if no match (shouldn't happen)
                console.error(`[Distributed] Failed to parse Runpod proxy host: ${host}`);
            }
        }
        
        // Determine protocol: HTTPS for cloud, Runpod proxies, or port 443
        const useHttps = isCloud || isRunpodProxy || worker.port === 443;
        const protocol = useHttps ? 'https' : 'http';
        
        // Only add port if non-standard
        const defaultPort = useHttps ? 443 : 80;
        const needsPort = !isRunpodProxy && worker.port !== defaultPort;
        const portStr = needsPort ? `:${worker.port}` : '';
        
        return `${protocol}://${finalHost}${portStr}${endpoint}`;
    }

    async checkWorkerStatus(worker) {
        // Assume caller ensured enabled; proceed with check
        const url = this.getWorkerUrl(worker, '/prompt');
        const statusDot = document.getElementById(`status-${worker.id}`);
        
        try {
            // Combine timeout with abort controller signal
            const timeoutSignal = AbortSignal.timeout(TIMEOUTS.STATUS_CHECK);
            const signal = this.statusCheckAbortController 
                ? AbortSignal.any([timeoutSignal, this.statusCheckAbortController.signal])
                : timeoutSignal;
            
            const response = await fetch(url, {
                method: 'GET',
                mode: 'cors',
                signal: signal
            });
            
            if (response.ok) {
                const data = await response.json();
                const queueRemaining = data.exec_info?.queue_remaining || 0;
                const isProcessing = queueRemaining > 0;
                
                // Update status
                this.state.setWorkerStatus(worker.id, {
                    online: true,
                    processing: isProcessing,
                    queueCount: queueRemaining
                });
                
                // Update status dot based on processing state
                if (isProcessing) {
                    this.ui.updateStatusDot(
                        worker.id,
                        "#f0ad4e",
                        `Online - Processing (${queueRemaining} in queue)`,
                        false
                    );
                } else {
                    this.ui.updateStatusDot(worker.id, "#3ca03c", "Online - Idle", false);
                }
                
                // Clear launching state since worker is now online
                if (this.state.isWorkerLaunching(worker.id)) {
                    this.state.setWorkerLaunching(worker.id, false);
                    this.clearLaunchingFlag(worker.id);
                }
            } else {
                throw new Error(`HTTP ${response.status}`);
            }
        } catch (error) {
            // Don't process aborted requests
            if (error.name === 'AbortError') {
                return;
            }
            
            // Worker is offline or unreachable
            this.state.setWorkerStatus(worker.id, {
                online: false,
                processing: false,
                queueCount: 0
            });
            
            // Check if worker is launching
            if (this.state.isWorkerLaunching(worker.id)) {
                this.ui.updateStatusDot(worker.id, "#f0ad4e", "Launching...", true);
            } else if (worker.enabled) {
                // Only update to red if not currently launching AND still enabled
                this.ui.updateStatusDot(worker.id, "#c04c4c", "Offline - Cannot connect", false);
            }
            // If disabled, don't update the dot (leave it gray)
            
            this.log(`Worker ${worker.id} status check failed: ${error.message}`, "debug");
        }
        
        // Update control buttons based on new status
        this.updateWorkerControls(worker.id);
    }

    async launchWorker(workerId) {
        const worker = this.config.workers.find(w => w.id === workerId);
        const launchBtn = document.querySelector(`#controls-${workerId} button`);

        // If worker is disabled, enable it first
        if (!worker.enabled) {
            await this.updateWorkerEnabled(workerId, true);
            
            // Update the checkbox UI
            const checkbox = document.getElementById(`gpu-${workerId}`);
            if (checkbox) {
                checkbox.checked = true;
            }
            
            this.updateSummary();
        }

        this.ui.updateStatusDot(workerId, "#f0ad4e", "Launching...", true);
        this.state.setWorkerLaunching(workerId, true);

        // Allow 90 seconds for worker to launch (model loading can take time)
        setTimeout(() => {
            this.state.setWorkerLaunching(workerId, false);
        }, TIMEOUTS.LAUNCH);

        if (!launchBtn) return;

        try {
            // Disable button immediately
            launchBtn.disabled = true;
            
            const result = await this.api.launchWorker(workerId);
            if (result) {
                this.log(`Launched ${worker.name} (PID: ${result.pid})`, "info");
                if (result.log_file) {
                    this.log(`Log file: ${result.log_file}`, "debug");
                }
                
                this.state.setWorkerManaged(workerId, {
                    pid: result.pid,
                    log_file: result.log_file,
                    started_at: Date.now()
                });
                
                // Update controls immediately to hide launch button and show stop/log buttons
                this.updateWorkerControls(workerId);
                setTimeout(() => this.checkWorkerStatus(worker), TIMEOUTS.STATUS_CHECK);
            }
        } catch (error) {
            // Check if worker was already running
            if (error.message && error.message.includes("already running")) {
                this.log(`Worker ${worker.name} is already running`, "info");
                this.updateWorkerControls(workerId);
                setTimeout(() => this.checkWorkerStatus(worker), TIMEOUTS.STATUS_CHECK_DELAY);
            } else {
                this.log(`Error launching worker: ${error.message || error}`, "error");
                
                // Re-enable button on error
                if (launchBtn) {
                    launchBtn.disabled = false;
                }
            }
        }
    }

    async stopWorker(workerId) {
        const worker = this.config.workers.find(w => w.id === workerId);
        const stopBtn = document.querySelectorAll(`#controls-${workerId} button`)[1];
        
        // Provide immediate feedback
        if (stopBtn) {
            stopBtn.disabled = true;
            stopBtn.textContent = "Stopping...";
            stopBtn.style.backgroundColor = "#666";
        }
        
        try {
            const result = await this.api.stopWorker(workerId);
            if (result) {
                this.log(`Stopped worker: ${result.message}`, "info");
                this.state.setWorkerManaged(workerId, null);
                
                // Immediately update status to offline
                this.ui.updateStatusDot(workerId, "#c04c4c", "Offline");
                this.state.setWorkerStatus(workerId, { online: false });
                
                // Flash success feedback
                if (stopBtn) {
                    stopBtn.style.backgroundColor = BUTTON_STYLES.success;
                    stopBtn.textContent = "Stopped!";
                    setTimeout(() => {
                        this.updateWorkerControls(workerId);
                    }, TIMEOUTS.FLASH_SHORT);
                }
                
                // Verify status after a short delay
                setTimeout(() => this.checkWorkerStatus(worker), TIMEOUTS.STATUS_CHECK);
            } else {
                this.log(`Failed to stop worker: ${result.message}`, "error");
                
                // Flash error feedback
                if (stopBtn) {
                    stopBtn.style.backgroundColor = BUTTON_STYLES.error;
                    stopBtn.textContent = result.message.includes("already stopped") ? "Not Running" : "Failed";
                    
                    // If already stopped, update status immediately
                    if (result.message.includes("already stopped")) {
                        this.ui.updateStatusDot(workerId, "#c04c4c", "Offline");
                        this.state.setWorkerStatus(workerId, { online: false });
                    }
                    
                    setTimeout(() => {
                        this.updateWorkerControls(workerId);
                    }, TIMEOUTS.FLASH_MEDIUM);
                }
            }
        } catch (error) {
            this.log(`Error stopping worker: ${error}`, "error");
            
            // Reset button on error
            if (stopBtn) {
                stopBtn.style.backgroundColor = BUTTON_STYLES.error;
                stopBtn.textContent = "Error";
                setTimeout(() => {
                    this.updateWorkerControls(workerId);
                }, TIMEOUTS.FLASH_MEDIUM);
            }
        }
    }

    async clearLaunchingFlag(workerId) {
        try {
            await this.api.clearLaunchingFlag(workerId);
            this.log(`Cleared launching flag for worker ${workerId}`, "debug");
        } catch (error) {
            this.log(`Error clearing launching flag: ${error.message || error}`, "error");
        }
    }

    // Generic async button action handler
    async handleAsyncButtonAction(button, action, successText, errorText, resetDelay = TIMEOUTS.BUTTON_RESET) {
        const originalText = button.textContent;
        const originalStyle = button.style.cssText;
        button.disabled = true;
        
        try {
            await action();
            button.textContent = successText;
            button.style.cssText = originalStyle;
            button.style.backgroundColor = BUTTON_STYLES.success;
            return true;
        } catch (error) {
            button.textContent = errorText || `Error: ${error.message}`;
            button.style.cssText = originalStyle;
            button.style.backgroundColor = BUTTON_STYLES.error;
            throw error;
        } finally {
            setTimeout(() => {
                button.textContent = originalText;
                button.style.cssText = originalStyle;
                button.disabled = false;
            }, resetDelay);
        }
    }

    /**
     * Cleanup method to stop intervals and listeners
     */
    cleanup() {
        if (this.statusCheckInterval) {
            clearInterval(this.statusCheckInterval);
            this.statusCheckInterval = null;
        }
        
        if (this.logAutoRefreshInterval) {
            clearInterval(this.logAutoRefreshInterval);
            this.logAutoRefreshInterval = null;
        }
        
        if (this.statusCheckTimeout) {
            clearTimeout(this.statusCheckTimeout);
            this.statusCheckTimeout = null;
        }
        
        this.log("Cleaned up intervals", "debug");
    }

    async loadManagedWorkers() {
        try {
            const result = await this.api.getManagedWorkers();
            
            // Check for launching workers
            for (const [workerId, info] of Object.entries(result.managed_workers)) {
                this.state.setWorkerManaged(workerId, info);
                
                // If worker is marked as launching, add to launchingWorkers set
                if (info.launching) {
                    this.state.setWorkerLaunching(workerId, true);
                    this.log(`Worker ${workerId} is in launching state`, "debug");
                }
            }
            
            // Update UI for all workers
            if (this.config?.workers) {
                this.config.workers.forEach(w => this.updateWorkerControls(w.id));
            }
        } catch (error) {
            this.log(`Error loading managed workers: ${error}`, "error");
        }
    }

    updateWorkerControls(workerId) {
        const controlsDiv = document.getElementById(`controls-${workerId}`);
        
        if (!controlsDiv) return;
        
        const worker = this.config.workers.find(w => w.id === workerId);
        if (!worker) return;
        
        // Skip button updates for remote workers
        if (this.isRemoteWorker(worker)) {
            return;
        }
        
        // Ensure we check for string ID
        const managedInfo = this.state.getWorker(workerId).managed;
        const status = this.state.getWorkerStatus(workerId);
        
        // Update button states - buttons are now inside a wrapper div
        const buttons = controlsDiv.querySelectorAll('button');
        const launchBtn = document.getElementById(`launch-${workerId}`);
        const stopBtn = document.getElementById(`stop-${workerId}`);
        const logBtn = document.getElementById(`log-${workerId}`);
        
        // Show log button immediately if we have log file info (even if worker is still starting)
        if (managedInfo?.log_file && logBtn) {
            logBtn.style.display = '';
        } else if (logBtn && !managedInfo) {
            logBtn.style.display = 'none';
        }
        
        if (status?.online || managedInfo) {
            // Worker is running or we just launched it
            launchBtn.style.display = 'none'; // Hide launch button when running
            
            if (managedInfo) {
                // Only show stop button if we manage this worker
                stopBtn.style.display = '';
                stopBtn.disabled = false;
                stopBtn.textContent = "Stop";
                stopBtn.style.backgroundColor = "#7c4a4a"; // Red when enabled
            } else {
                // Hide stop button for workers launched outside UI
                stopBtn.style.display = 'none';
            }
        } else {
            // Worker is not running
            launchBtn.style.display = ''; // Show launch button
            launchBtn.disabled = false;
            launchBtn.textContent = "Launch";
            launchBtn.style.backgroundColor = "#4a7c4a"; // Always green
            
            stopBtn.style.display = 'none'; // Hide stop button when not running
        }
    }

    async viewWorkerLog(workerId) {
        const managedInfo = this.state.getWorker(workerId).managed;
        if (!managedInfo?.log_file) return;
        
        const logBtn = document.getElementById(`log-${workerId}`);
        
        // Provide immediate feedback
        if (logBtn) {
            logBtn.disabled = true;
            logBtn.textContent = "Loading...";
            logBtn.style.backgroundColor = "#666";
        }
        
        try {
            // Fetch log content
            const data = await this.api.getWorkerLog(workerId, 1000);
            
            // Create modal dialog
            this.ui.showLogModal(this, workerId, data);
            
            // Restore button
            if (logBtn) {
                logBtn.disabled = false;
                logBtn.textContent = "View Log";
                logBtn.style.backgroundColor = "#685434"; // Keep the yellow color
            }
            
        } catch (error) {
            this.log('Error viewing log: ' + error.message, "error");
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Error",
                detail: `Failed to load log: ${error.message}`,
                life: 5000
            });
            
            // Flash error and restore button
            if (logBtn) {
                logBtn.style.backgroundColor = BUTTON_STYLES.error;
                logBtn.textContent = "Error";
                setTimeout(() => {
                    logBtn.disabled = false;
                    logBtn.textContent = "View Log";
                    logBtn.style.backgroundColor = "#685434"; // Keep the yellow color
                }, TIMEOUTS.FLASH_LONG);
            }
        }
    }

    async refreshLog(workerId, silent = false) {
        const logContent = document.getElementById('distributed-log-content');
        if (!logContent) return;
        
        try {
            const data = await this.api.getWorkerLog(workerId, 1000);
            
            // Update content
            const shouldAutoScroll = logContent.scrollTop + logContent.clientHeight >= logContent.scrollHeight - 50;
            logContent.textContent = data.content;
            
            // Auto-scroll if was at bottom
            if (shouldAutoScroll) {
                logContent.scrollTop = logContent.scrollHeight;
            }
            
            // Only show toast if not in silent mode (manual refresh)
            if (!silent) {
                app.extensionManager.toast.add({
                    severity: "success",
                    summary: "Log Refreshed",
                    detail: "Log content updated",
                    life: 2000
                });
            }
            
        } catch (error) {
            // Only show error toast if not in silent mode
            if (!silent) {
                app.extensionManager.toast.add({
                    severity: "error",
                    summary: "Refresh Failed",
                    detail: error.message,
                    life: 3000
                });
            }
        }
    }

    isRemoteWorker(worker) {
        // Check if explicitly marked as cloud worker
        if (worker.type === "cloud") {
            return true;
        }
        // Otherwise check by host (backward compatibility)
        const host = worker.host || window.location.hostname;
        return host !== "localhost" && host !== "127.0.0.1" && host !== window.location.hostname;
    }

    isCloudWorker(worker) {
        return worker.type === "cloud";
    }

    getMasterUrl() {
        // Always use the detected/configured master IP for consistency
        if (this.config?.master?.host) {
            const configuredHost = this.config.master.host;
            
            // If the configured host already includes protocol, use as-is
            if (configuredHost.startsWith('http://') || configuredHost.startsWith('https://')) {
                return configuredHost;
            }
            
            // For domain names (not IPs), default to HTTPS
            const isIP = /^(\d{1,3}\.){3}\d{1,3}$/.test(configuredHost);
            const isLocalhost = configuredHost === 'localhost' || configuredHost === '127.0.0.1';
            
            if (!isIP && !isLocalhost && configuredHost.includes('.')) {
                // It's a domain name, use HTTPS
                return `https://${configuredHost}`;
            } else {
                // For IPs and localhost, use current access method
                const port = window.location.port || (window.location.protocol === 'https:' ? '443' : '80');
                if ((window.location.protocol === 'https:' && port === '443') || 
                    (window.location.protocol === 'http:' && port === '80')) {
                    return `${window.location.protocol}//${configuredHost}`;
                }
                return `${window.location.protocol}//${configuredHost}:${port}`;
            }
        }
        
        // If no master IP is set but we're on a network address, use it
        const hostname = window.location.hostname;
        if (hostname !== 'localhost' && hostname !== '127.0.0.1') {
            return window.location.origin;
        }
        
        // Fallback warning - this won't work for remote workers
        this.log("No master host configured - remote workers won't be able to connect. " +
                     "Master host should be auto-detected on startup.", "debug");
        return window.location.origin;
    }

    async detectMasterIP() {
        try {
            // Detect if we're running on Runpod
            const isRunpod = window.location.hostname.endsWith('.proxy.runpod.net');
            if (isRunpod) {
                this.log("Detected Runpod environment", "info");
            }
            
            const data = await this.api.getNetworkInfo();
            this.log("Network info: " + JSON.stringify(data), "debug");
            
            // Store CUDA device info
            if (data.cuda_device !== null && data.cuda_device !== undefined) {
                this.masterCudaDevice = data.cuda_device;
                
                // Store persistently in config if not already set or changed
                if (!this.config.master) this.config.master = {};
                if (this.config.master.cuda_device === undefined || this.config.master.cuda_device !== data.cuda_device) {
                    this.config.master.cuda_device = data.cuda_device;
                    try {
                        await this.api.updateMaster({ cuda_device: data.cuda_device });
                        this.log(`Stored master CUDA device: ${data.cuda_device}`, "debug");
                    } catch (error) {
                        this.log(`Error storing master CUDA device: ${error.message}`, "error");
                    }
                }
                
                // Update the master display with CUDA info
                this.ui.updateMasterDisplay(this);
            }
            
            // Store CUDA device count for auto-population
            if (data.cuda_device_count > 0) {
                this.cudaDeviceCount = data.cuda_device_count;
                this.log(`Detected ${this.cudaDeviceCount} CUDA devices`, "info");
                
                // Auto-populate workers if conditions are met
                const shouldAutoPopulate = 
                    !this.config.settings.has_auto_populated_workers && // Never populated before
                    (!this.config.workers || this.config.workers.length === 0); // No workers exist
                
                this.log(`Auto-population check: has_populated=${this.config.settings.has_auto_populated_workers}, workers=${this.config.workers ? this.config.workers.length : 'null'}, should_populate=${shouldAutoPopulate}`, "debug");
                
                if (shouldAutoPopulate) {
                    this.log(`Auto-populating workers based on ${this.cudaDeviceCount} CUDA devices (excluding master on CUDA ${this.masterCudaDevice})`, "info");
                    
                    const newWorkers = [];
                    let workerNum = 1;
                    let portOffset = 0;
                    
                    for (let i = 0; i < this.cudaDeviceCount; i++) {
                        // Skip the CUDA device used by master
                        if (i === this.masterCudaDevice) {
                            this.log(`Skipping CUDA ${i} (used by master)`, "debug");
                            continue;
                        }
                        
                        const worker = {
                            id: crypto.randomUUID(),
                            name: `Worker ${workerNum}`,
                            host: isRunpod ? null : "localhost",
                            port: 8189 + portOffset,
                            cuda_device: i,
                            enabled: true,
                            extra_args: isRunpod ? "--listen" : ""
                        };
                        newWorkers.push(worker);
                        workerNum++;
                        portOffset++;
                    }
                    
                    // Only proceed if we have workers to add
                    if (newWorkers.length > 0) {
                        this.log(`Auto-populating ${newWorkers.length} workers`, "info");
                        
                        // Add workers to config
                        this.config.workers = newWorkers;
                        
                        // Set the flag to prevent future auto-population
                        this.config.settings.has_auto_populated_workers = true;
                        
                        // Save each worker using the update endpoint
                        for (const worker of newWorkers) {
                            try {
                                await this.api.updateWorker(worker.id, worker);
                            } catch (error) {
                                this.log(`Error saving worker ${worker.name}: ${error.message}`, "error");
                            }
                        }
                        
                        // Save the updated settings
                        try {
                            await this.api.updateSetting('has_auto_populated_workers', true);
                        } catch (error) {
                            this.log(`Error saving auto-population flag: ${error.message}`, "error");
                        }
                        
                        this.log(`Auto-populated ${newWorkers.length} workers and saved config`, "info");
                        
                        // Show success notification
                        if (app.extensionManager?.toast) {
                            app.extensionManager.toast.add({
                                severity: "success",
                                summary: "Workers Auto-populated",
                                detail: `Automatically created ${newWorkers.length} workers based on detected CUDA devices`,
                                life: 5000
                            });
                        }
                        
                        // Reload the config to include the new workers
                        await this.loadConfig();
                    } else {
                        this.log("No additional CUDA devices available for workers (all used by master)", "debug");
                    }
                }
            }
            
            // Check if we already have a master host configured
            if (this.config?.master?.host) {
                this.log(`Master host already configured: ${this.config.master.host}`, "debug");
                return;
            }
            
            // For Runpod, use the proxy hostname as master host
            if (isRunpod) {
                const runpodHost = window.location.hostname;
                this.log(`Setting Runpod master host: ${runpodHost}`, "info");
                
                // Save the Runpod host
                await this.api.updateMaster({ host: runpodHost });
                
                // Update local config
                if (!this.config.master) this.config.master = {};
                this.config.master.host = runpodHost;
                
                // Show notification
                if (app.extensionManager?.toast) {
                    app.extensionManager.toast.add({
                        severity: "info",
                        summary: "Runpod Auto-Configuration",
                        detail: `Master host set to ${runpodHost} with --listen flag for workers`,
                        life: 5000
                    });
                }
                return; // Skip regular IP detection for Runpod
            }
            
            // Use the recommended IP from the backend
            if (data.recommended_ip && data.recommended_ip !== '127.0.0.1') {
                this.log(`Auto-detected master IP: ${data.recommended_ip}`, "info");
                
                // Save the detected IP (pass true to suppress notification)
                await this.api.updateMaster({ host: data.recommended_ip });
                
                // Update local config immediately
                if (!this.config.master) this.config.master = {};
                this.config.master.host = data.recommended_ip;
            }
        } catch (error) {
            this.log("Error detecting master IP: " + error.message, "error");
        }
    }

    async saveWorkerSettings(workerId) {
        const worker = this.config.workers.find(w => w.id === workerId);
        if (!worker) return;
        
        // Get form values
        const name = document.getElementById(`name-${workerId}`).value;
        const workerType = document.getElementById(`worker-type-${workerId}`).value;
        const isRemote = workerType === 'remote' || workerType === 'cloud';
        const isCloud = workerType === 'cloud';
        const host = isRemote ? document.getElementById(`host-${workerId}`).value : window.location.hostname;
        const port = parseInt(document.getElementById(`port-${workerId}`).value);
        const cudaDevice = isRemote ? undefined : parseInt(document.getElementById(`cuda-${workerId}`).value);
        const extraArgs = isRemote ? undefined : document.getElementById(`args-${workerId}`).value;
        
        // Validate
        if (!name.trim()) {
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Validation Error",
                detail: "Worker name is required",
                life: 3000
            });
            return;
        }
        
        if ((workerType === 'remote' || workerType === 'cloud') && !host.trim()) {
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Validation Error",
                detail: "Host is required for remote workers",
                life: 3000
            });
            return;
        }
        
        if (!isCloud && (isNaN(port) || port < 1 || port > 65535)) {
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Validation Error",
                detail: "Port must be between 1 and 65535",
                life: 3000
            });
            return;
        }
        
        // Check for port conflicts
        // Remote workers can reuse ports, but local workers cannot share ports with each other or master
        if (!isRemote) {
            // Check if port conflicts with master
            const masterPort = parseInt(window.location.port) || (window.location.protocol === 'https:' ? 443 : 80);
            if (port === masterPort) {
                app.extensionManager.toast.add({
                    severity: "error",
                    summary: "Port Conflict",
                    detail: `Port ${port} is already in use by the master server`,
                    life: 3000
                });
                return;
            }
            
            // Check if port conflicts with other local workers
            const localPortConflict = this.config.workers.some(w => 
                w.id !== workerId && 
                w.port === port && 
                !w.host // local workers have no host or host is null
            );
            
            if (localPortConflict) {
                app.extensionManager.toast.add({
                    severity: "error",
                    summary: "Port Conflict",
                    detail: `Port ${port} is already in use by another local worker`,
                    life: 3000
                });
                return;
            }
        } else {
            // For remote workers, only check conflicts with other workers on the same host
            const sameHostConflict = this.config.workers.some(w => 
                w.id !== workerId && 
                w.port === port && 
                w.host === host.trim()
            );
            
            if (sameHostConflict) {
                app.extensionManager.toast.add({
                    severity: "error",
                    summary: "Port Conflict",
                    detail: `Port ${port} is already in use by another worker on ${host}`,
                    life: 3000
                });
                return;
            }
        }
        
        try {
            await this.api.updateWorker(workerId, {
                name: name.trim(),
                type: workerType,
                host: isRemote ? host.trim() : null,
                port: port,
                cuda_device: isRemote ? null : cudaDevice,
                extra_args: isRemote ? null : (extraArgs ? extraArgs.trim() : "")
            });
            
            // Update local config
            worker.name = name.trim();
            worker.type = workerType;
            if (isRemote) {
                worker.host = host.trim();
                delete worker.cuda_device;
                delete worker.extra_args;
            } else {
                delete worker.host;
                worker.cuda_device = cudaDevice;
                worker.extra_args = extraArgs ? extraArgs.trim() : "";
            }
            worker.port = port;
            
            // Sync to state
            this.state.updateWorker(workerId, { enabled: worker.enabled });
            
            app.extensionManager.toast.add({
                severity: "success",
                summary: "Settings Saved",
                detail: `Worker ${name} settings updated`,
                life: 3000
            });
            
            // Refresh the UI
            if (this.panelElement) {
                renderSidebarContent(this, this.panelElement);
            }
        } catch (error) {
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Save Failed",
                detail: error.message,
                life: 5000
            });
        }
    }

    cancelWorkerSettings(workerId) {
        // Collapse the settings panel
        this.toggleWorkerExpanded(workerId);
        
        // Reset form values to original
        const worker = this.config.workers.find(w => w.id === workerId);
        if (worker) {
            document.getElementById(`name-${workerId}`).value = worker.name;
            document.getElementById(`host-${workerId}`).value = worker.host || "";
            document.getElementById(`port-${workerId}`).value = worker.port;
            document.getElementById(`cuda-${workerId}`).value = worker.cuda_device || 0;
            document.getElementById(`args-${workerId}`).value = worker.extra_args || "";
            
            // Reset remote checkbox
            const remoteCheckbox = document.getElementById(`remote-${workerId}`);
            if (remoteCheckbox) {
                remoteCheckbox.checked = this.isRemoteWorker(worker);
            }
        }
    }

    async deleteWorker(workerId) {
        const worker = this.config.workers.find(w => w.id === workerId);
        if (!worker) return;
        
        // Confirm deletion
        if (!confirm(`Are you sure you want to delete worker "${worker.name}"?`)) {
            return;
        }
        
        try {
            await this.api.deleteWorker(workerId);
            
            // Remove from local config
            const index = this.config.workers.findIndex(w => w.id === workerId);
            if (index !== -1) {
                this.config.workers.splice(index, 1);
            }
            
            app.extensionManager.toast.add({
                severity: "success",
                summary: "Worker Deleted",
                detail: `Worker ${worker.name} has been removed`,
                life: 3000
            });
            
            // Refresh the UI
            if (this.panelElement) {
                renderSidebarContent(this, this.panelElement);
            }
        } catch (error) {
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Delete Failed",
                detail: error.message,
                life: 5000
            });
        }
    }

    async addNewWorker() {
        // Generate new worker ID using UUID (fallback for non-secure contexts)
        const newId = this.generateUUID();
        
        // Find next available port
        const usedPorts = this.config.workers.map(w => w.port);
        let nextPort = 8189;
        while (usedPorts.includes(nextPort)) {
            nextPort++;
        }
        
        // Create new worker object
        const newWorker = {
            id: newId,
            name: `Worker ${this.config.workers.length + 1}`,
            port: nextPort,
            cuda_device: this.config.workers.length,
            enabled: true,  // Default to enabled for convenience
            extra_args: ""
        };
        
        // Add to config
        this.config.workers.push(newWorker);
        
        // Save immediately
        try {
            await this.api.updateWorker(newId, {
                name: newWorker.name,
                port: newWorker.port,
                cuda_device: newWorker.cuda_device,
                extra_args: newWorker.extra_args,
                enabled: newWorker.enabled
            });
            
            // Sync to state
            this.state.updateWorker(newId, { enabled: true });
            
            app.extensionManager.toast.add({
                severity: "success",
                summary: "Worker Added",
                detail: `New worker created on port ${nextPort}`,
                life: 3000
            });
            
            // Refresh UI and expand the new worker
            this.state.setWorkerExpanded(newId, true);
            if (this.panelElement) {
                renderSidebarContent(this, this.panelElement);
            }
            
        } catch (error) {
            app.extensionManager.toast.add({
                severity: "error",
                summary: "Failed to Add Worker",
                detail: error.message,
                life: 5000
            });
        }
    }

    startLogAutoRefresh(workerId) {
        // Stop any existing auto-refresh
        this.stopLogAutoRefresh();
        
        // Refresh every 2 seconds
        this.logAutoRefreshInterval = setInterval(() => {
            this.refreshLog(workerId, true); // silent mode
        }, TIMEOUTS.LOG_REFRESH);
    }

    stopLogAutoRefresh() {
        if (this.logAutoRefreshInterval) {
            clearInterval(this.logAutoRefreshInterval);
            this.logAutoRefreshInterval = null;
        }
    }

    toggleWorkerExpanded(workerId) {
        const settingsDiv = document.getElementById(`settings-${workerId}`);
        const gpuDiv = settingsDiv.closest('[style*="margin-bottom: 12px"]');
        const settingsArrow = gpuDiv.querySelector('.settings-arrow');
        
        if (!settingsDiv) return;
        
        if (this.state.isWorkerExpanded(workerId)) {
            this.state.setWorkerExpanded(workerId, false);
            settingsDiv.classList.remove("expanded");
            if (settingsArrow) {
                settingsArrow.style.transform = "rotate(0deg)";
            }
            // Animate padding to 0
            settingsDiv.style.padding = "0 12px";
            settingsDiv.style.marginTop = "0";
            settingsDiv.style.marginBottom = "0";
        } else {
            this.state.setWorkerExpanded(workerId, true);
            settingsDiv.classList.add("expanded");
            if (settingsArrow) {
                settingsArrow.style.transform = "rotate(90deg)";
            }
            // Animate padding to full
            settingsDiv.style.padding = "12px";
            settingsDiv.style.marginTop = "8px";
            settingsDiv.style.marginBottom = "8px";
        }
    }

    _handleInterruptWorkers(button) {
        return handleInterruptWorkers(this, button);
    }

    _handleClearMemory(button) {
        return handleClearMemory(this, button);
    }

    // --- Deadline Integration Methods ---

    async updateDeadlineStatus() {
        const statusElement = document.getElementById('deadline-status');
        if (!statusElement) return;

        statusElement.textContent = "Loading Deadline status...";
        statusElement.style.color = "#888";

        try {
            const response = await fetch('/deadline/status');
            const status = await response.json();

            if (status.available) {
                const activeWorkersCount = status.active_workers ? status.active_workers.length : 0;
                const activeJobsCount = status.active_jobs || 0;
                
                let statusHTML = `<div style="color: #4CAF50;">✅ Deadline available</div>`;
                
                if (activeWorkersCount > 0) {
                    statusHTML += `<div>🎯 Active Workers: ${activeWorkersCount}</div>`;
                }
                
                if (activeJobsCount > 0) {
                    statusHTML += `<div>📋 Running Jobs: ${activeJobsCount}</div>`;
                } else {
                    statusHTML += `<div style="color: #888;">No workers claimed yet</div>`;
                }
                
                statusElement.innerHTML = statusHTML;
                statusElement.style.color = "#4CAF50";
                
                // Update active workers list if available
                if (status.active_workers && status.active_workers.length > 0) {
                    this.updateActiveWorkersList(status.active_workers);
                }
            } else {
                statusElement.innerHTML = `
                    <div style="color: #ff6b6b;">❌ Deadline not available</div>
                    <div style="font-size: 10px; margin-top: 4px;">${status.error || 'Unknown error'}</div>
                `;
                statusElement.style.color = "#ff6b6b";
            }
        } catch (error) {
            statusElement.innerHTML = `
                <div style="color: #ff6b6b;">❌ Connection error</div>
                <div style="font-size: 10px; margin-top: 4px;">${error.message}</div>
            `;
            statusElement.style.color = "#ff6b6b";
        }
    }

    async claimDeadlineWorkers(count = 4) {
        try {
            // Get master WS address from distributed config
            // Try to use the recommended IP from network detection, fallback to localhost
            const recommendedIP = this.config?.master?.host || 'localhost';
            const masterPort = this.config?.master?.port || '8188';
            const masterWS = `${recommendedIP}:${masterPort}`;
            
            this.log(`🎯 Claiming ${count} workers with master at ${masterWS}`, 'info');
            
            // Get deadline settings from config
            const deadlineSettings = this.config?.deadline || {};
            const priority = deadlineSettings.priority || 50;
            const pool = deadlineSettings.pool || "none";
            const group = deadlineSettings.group || "none";
            
            this.log(`📋 Using deadline settings: Priority=${priority}, Pool=${pool}, Group=${group}`, 'info');
            
            const response = await fetch('/deadline/claim_workers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    count: count,
                    master_ws: masterWS,
                    priority: priority,
                    pool: pool,
                    group: group
                })
            });

            const result = await response.json();

            if (result.success) {
                this.log(`✅ ${result.message}`, 'info');
                // Update status after claiming
                setTimeout(() => this.updateDeadlineStatus(), 1000);
            } else {
                this.log(`❌ Failed to claim workers: ${result.error}`, 'error');
            }
        } catch (error) {
            this.log(`❌ Error claiming workers: ${error.message}`, 'error');
        }
    }

    async releaseDeadlineWorkers() {
        try {
            const response = await fetch('/deadline/release_workers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });

            const result = await response.json();

            if (result.success) {
                this.log(`✅ ${result.message}`, 'info');
                // Update status after releasing
                setTimeout(() => this.updateDeadlineStatus(), 1000);
            } else {
                this.log(`❌ Failed to release workers: ${result.error}`, 'error');
            }
        } catch (error) {
            this.log(`❌ Error releasing workers: ${error.message}`, 'error');
        }
    }

    updateActiveWorkersList(workers) {
        // Find or create workers list container in the deadline section
        let workersList = document.getElementById('deadline-workers-list');
        if (!workersList) {
            // Create workers list container
            const deadlineSection = document.querySelector('#deadline-status').parentElement;
            workersList = document.createElement('div');
            workersList.id = 'deadline-workers-list';
            workersList.style.cssText = 'margin-top: 8px; font-size: 11px;';
            deadlineSection.appendChild(workersList);
        }

        if (workers.length === 0) {
            workersList.innerHTML = '<div style="color: #888; font-style: italic;">No active workers</div>';
            return;
        }

        // Create workers list
        const workersHTML = workers.map(worker => `
            <div style="padding: 4px; margin: 2px 0; background: #333; border-radius: 3px; border-left: 3px solid #ff6b35;">
                <div style="font-weight: bold; color: #ff6b35;">🖥️ ${worker.id}</div>
                <div style="color: #aaa;">📍 ${worker.ip}:${worker.port}</div>
                ${worker.job_id ? `<div style="color: #888; font-size: 10px;">Job: ${worker.job_id}</div>` : ''}
            </div>
        `).join('');

        workersList.innerHTML = `
            <div style="font-weight: bold; margin-bottom: 4px; color: #ff6b35;">Active Deadline Workers:</div>
            ${workersHTML}
        `;
    }
}

app.registerExtension({
    name: "Distributed.Panel",
    async setup() {
        new DistributedExtension();
    }
});