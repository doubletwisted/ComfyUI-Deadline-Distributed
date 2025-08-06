import { TIMEOUTS } from './constants.js';

export function createApiClient(baseUrl) {
    const request = async (endpoint, options = {}, retries = TIMEOUTS.MAX_RETRIES) => {
        let lastError;
        let delay = TIMEOUTS.RETRY_DELAY; // Initial delay for exponential backoff

        for (let attempt = 0; attempt < retries; attempt++) {
            try {
                const response = await fetch(`${baseUrl}${endpoint}`, {
                    headers: { 'Content-Type': 'application/json' },
                    ...options
                });
                
                if (!response.ok) {
                    const error = await response.json().catch(() => ({ message: 'Request failed' }));
                    throw new Error(error.message || `HTTP ${response.status}`);
                }
                
                return await response.json();
            } catch (error) {
                lastError = error;
                console.log(`API Error (attempt ${attempt + 1}/${retries}): ${endpoint} - ${error.message}`);
                if (attempt < retries - 1) {
                    await new Promise(resolve => setTimeout(resolve, delay));
                    delay *= 2; // Exponential backoff
                }
            }
        }
        throw lastError;
    };
    
    return {
        // Config endpoints
        async getConfig() {
            return request('/distributed/config');
        },
        
        async updateWorker(workerId, data) {
            return request('/distributed/config/update_worker', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId, ...data })
            });
        },
        
        async deleteWorker(workerId) {
            return request('/distributed/config/delete_worker', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId })
            });
        },
        
        async updateSetting(key, value) {
            return request('/distributed/config/update_setting', {
                method: 'POST',
                body: JSON.stringify({ key, value })
            });
        },
        
        async updateMaster(data) {
            return request('/distributed/config/update_master', {
                method: 'POST',
                body: JSON.stringify(data)
            });
        },
        
        async updateDeadline(data) {
            return request('/distributed/config/update_deadline', {
                method: 'POST',
                body: JSON.stringify(data)
            });
        },
        
        async getDeadlinePools() {
            return request('/distributed/deadline/pools', {
                method: 'GET'
            });
        },
        
        async getDeadlineGroups() {
            return request('/distributed/deadline/groups', {
                method: 'GET'
            });
        },
        
        // Worker management endpoints
        async launchWorker(workerId) {
            return request('/distributed/launch_worker', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId })
            });
        },
        
        async stopWorker(workerId) {
            return request('/distributed/stop_worker', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId })
            });
        },
        
        async getManagedWorkers() {
            return request('/distributed/managed_workers');
        },
        
        async getWorkerLog(workerId, lines = 1000) {
            return request(`/distributed/worker_log/${workerId}?lines=${lines}`);
        },
        
        async clearLaunchingFlag(workerId) {
            return request('/distributed/worker/clear_launching', {
                method: 'POST',
                body: JSON.stringify({ worker_id: workerId })
            });
        },
        
        // Job preparation
        async prepareJob(multiJobId) {
            return request('/distributed/prepare_job', {
                method: 'POST',
                body: JSON.stringify({ multi_job_id: multiJobId })
            });
        },
        
        // Image loading
        async loadImage(imagePath) {
            return request('/distributed/load_image', {
                method: 'POST',
                body: JSON.stringify({ image_path: imagePath })
            });
        },
        
        // Network info
        async getNetworkInfo() {
            return request('/distributed/network_info');
        },
        
        // Status checking (with timeout)
        async checkStatus(url, timeout = TIMEOUTS.DEFAULT_FETCH) {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), timeout);
            
            try {
                const response = await fetch(url, {
                    method: 'GET',
                    mode: 'cors',
                    signal: controller.signal
                });
                clearTimeout(timeoutId);
                
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return await response.json();
            } catch (error) {
                clearTimeout(timeoutId);
                throw error;
            }
        },
        
        // Batch status checking
        async checkMultipleStatuses(urls) {
            return Promise.allSettled(
                urls.map(url => this.checkStatus(url))
            );
        }
    };
}