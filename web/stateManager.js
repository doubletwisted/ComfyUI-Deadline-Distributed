export function createStateManager() {
    const state = {
        workers: new Map(), // Unified worker state: { status, managed, launching, expanded, ... }
        masterStatus: 'online',
    };
    
    return {
        // Worker state management
        getWorker(workerId) {
            return state.workers.get(String(workerId)) || {};
        },
        
        updateWorker(workerId, updates) {
            const id = String(workerId);
            const current = state.workers.get(id) || {};
            state.workers.set(id, { ...current, ...updates });
            return state.workers.get(id);
        },
        
        setWorkerStatus(workerId, status) {
            return this.updateWorker(workerId, { status });
        },
        
        setWorkerManaged(workerId, info) {
            return this.updateWorker(workerId, { managed: info });
        },
        
        setWorkerLaunching(workerId, launching) {
            return this.updateWorker(workerId, { launching });
        },
        
        setWorkerExpanded(workerId, expanded) {
            return this.updateWorker(workerId, { expanded });
        },

        setWorkerProgress(workerId, progress) {
            return this.updateWorker(workerId, { progress });
        },

        getWorkerProgress(workerId) {
            return this.getWorker(workerId).progress || { value: 0, max: 0, node: '' };
        },
        
        isWorkerLaunching(workerId) {
            return this.getWorker(workerId).launching || false;
        },
        
        isWorkerExpanded(workerId) {
            return this.getWorker(workerId).expanded || false;
        },
        
        isWorkerManaged(workerId) {
            return !!this.getWorker(workerId).managed;
        },
        
        getWorkerStatus(workerId) {
            return this.getWorker(workerId).status || {};
        },
        
        // Master state
        setMasterStatus(status) {
            state.masterStatus = status;
        },
        
        getMasterStatus() {
            return state.masterStatus;
        }
    };
}