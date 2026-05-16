/**
 * Application State Management
 * 
 * Simple state management system for the ExacqMan web application.
 * Provides reactive state updates and event handling.
 */

class AppState {
    constructor() {
        this.state = {
            // Configuration data
            configs: [],
            cameras: [],
            servers: {},
            currentConfig: null,
            
            // Job management
            activeJobs: new Map(),
            jobHistory: [],
            
            // File management
            processedVideos: [],
            lastFileRefresh: null,
            
            // UI state
            isLoading: false,
            currentError: null
        };
        
        this.listeners = new Map();
        this.initializeState();
    }

    /**
     * Initialize default state
     */
    initializeState() {
        // Set default datetime values (1 hour ago to now) in local time
        const now = new Date();
        const fifteenMinutesAgo = new Date(now.getTime() - 15 * 60 * 1000);

        // Format for datetime-local input (YYYY-MM-DDTHH:MM)
        this.state.defaultStartTime = this.formatDateTimeLocal(fifteenMinutesAgo);
        this.state.defaultEndTime = this.formatDateTimeLocal(now);
    }

    /**
     * Format date for datetime-local input
     * @param {Date} date - Date object
     * @returns {string} Formatted date string
     */
    formatDateTimeLocal(date) {
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        const hours = String(date.getHours()).padStart(2, '0');
        const minutes = String(date.getMinutes()).padStart(2, '0');
        
        return `${year}-${month}-${day}T${hours}:${minutes}`;
    }

    /**
     * Detect if the current device is mobile
     * @returns {boolean} True if mobile device
     */
    isMobile() {
        return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
    }

    /**
     * Get current state
     * @returns {Object} Current state
     */
    getState() {
        return { ...this.state };
    }

    /**
     * Get specific state property
     * @param {string} key - State property key
     * @returns {*} State property value
     */
    get(key) {
        return this.state[key];
    }

    /**
     * Set state property and notify listeners
     * @param {string} key - State property key
     * @param {*} value - New value
     */
    set(key, value) {
        const oldValue = this.state[key];
        this.state[key] = value;
        
        // Notify listeners
        this.notifyListeners(key, value, oldValue);
    }

    /**
     * Update multiple state properties at once
     * @param {Object} updates - State updates
     */
    update(updates) {
        const oldState = { ...this.state };
        
        Object.keys(updates).forEach(key => {
            this.state[key] = updates[key];
        });
        
        // Notify listeners for each changed property
        Object.keys(updates).forEach(key => {
            this.notifyListeners(key, updates[key], oldState[key]);
        });
    }

    /**
     * Subscribe to state changes
     * @param {string} key - State property to watch
     * @param {Function} callback - Callback function
     * @returns {Function} Unsubscribe function
     */
    subscribe(key, callback) {
        if (!this.listeners.has(key)) {
            this.listeners.set(key, new Set());
        }
        
        this.listeners.get(key).add(callback);
        
        // Return unsubscribe function
        return () => {
            const keyListeners = this.listeners.get(key);
            if (keyListeners) {
                keyListeners.delete(callback);
                if (keyListeners.size === 0) {
                    this.listeners.delete(key);
                }
            }
        };
    }

    /**
     * Notify listeners of state changes
     * @param {string} key - Changed property
     * @param {*} newValue - New value
     * @param {*} oldValue - Old value
     */
    notifyListeners(key, newValue, oldValue) {
        const keyListeners = this.listeners.get(key);
        if (keyListeners) {
            keyListeners.forEach(callback => {
                try {
                    callback(newValue, oldValue, key);
                } catch (error) {
                    console.error(`Error in state listener for ${key}:`, error);
                }
            });
        }
    }

    // Configuration management

    /**
     * Update configuration data
     * @param {Array} configs - Available configurations
     */
    updateConfigs(configs) {
        this.set('configs', configs);
    }

    /**
     * Update cameras for current config
     * @param {Array} cameras - Available cameras
     */
    updateCameras(cameras) {
        this.set('cameras', cameras);
    }

    /**
     * Update servers for current config
     * @param {Object} servers - Available servers
     */
    updateServers(servers) {
        this.set('servers', servers);
    }

    /**
     * Set current configuration
     * @param {string} configFile - Configuration file path
     */
    setCurrentConfig(configFile) {
        this.set('currentConfig', configFile);
    }

    // Job management

    /**
     * Add new job
     * @param {string} jobId - Job identifier
     * @param {Object} jobData - Job data
     */
    addJob(jobId, jobData) {
        const jobs = new Map(this.state.activeJobs);
        jobs.set(jobId, {
            ...jobData,
            id: jobId,
            createdAt: new Date().toISOString()
        });
        this.set('activeJobs', jobs);
    }

    /**
     * Update job status
     * @param {string} jobId - Job identifier
     * @param {Object} statusData - Status data
     */
    updateJobStatus(jobId, statusData) {
        const jobs = new Map(this.state.activeJobs);
        const job = jobs.get(jobId);
        
        if (job) {
            const updatedJob = { ...job, ...statusData };
            jobs.set(jobId, updatedJob);
            this.set('activeJobs', jobs);
            
            // Move completed/failed jobs to history
            if (statusData.status === 'completed' || statusData.status === 'failed') {
                this.moveJobToHistory(jobId, updatedJob);
            }
        }
    }

    /**
     * Move job to history
     * @param {string} jobId - Job identifier
     * @param {Object} jobData - Job data
     */
    moveJobToHistory(jobId, jobData) {
        const jobs = new Map(this.state.activeJobs);
        jobs.delete(jobId);
        this.set('activeJobs', jobs);
        
        const history = [...this.state.jobHistory, jobData];
        this.set('jobHistory', history.slice(-50)); // Keep last 50 jobs
    }

    /**
     * Remove job
     * @param {string} jobId - Job identifier
     */
    removeJob(jobId) {
        const jobs = new Map(this.state.activeJobs);
        jobs.delete(jobId);
        this.set('activeJobs', jobs);
    }

    /**
     * Get active jobs
     * @returns {Array} Active jobs
     */
    getActiveJobs() {
        return Array.from(this.state.activeJobs.values());
    }

    /**
     * Get job by ID
     * @param {string} jobId - Job identifier
     * @returns {Object|null} Job data
     */
    getJob(jobId) {
        return this.state.activeJobs.get(jobId) || null;
    }

    // File management

    /**
     * Update processed videos
     * @param {Array} videos - Video files
     */
    updateProcessedVideos(videos) {
        this.set('processedVideos', videos);
        this.set('lastFileRefresh', new Date().toISOString());
    }

    /**
     * Add new video to list
     * @param {Object} video - Video data
     */
    addProcessedVideo(video) {
        const videos = [...this.state.processedVideos];
        const existingIndex = videos.findIndex(v => v.filename === video.filename);
        
        if (existingIndex >= 0) {
            videos[existingIndex] = video;
        } else {
            videos.unshift(video); // Add to beginning
        }
        
        this.set('processedVideos', videos);
    }

    /**
     * Remove video from list
     * @param {string} filename - Video filename
     */
    removeProcessedVideo(filename) {
        const videos = this.state.processedVideos.filter(v => v.filename !== filename);
        this.set('processedVideos', videos);
    }

    // UI state management


    /**
     * Set loading state
     * @param {boolean} isLoading - Loading state
     */
    setLoading(isLoading) {
        this.set('isLoading', isLoading);
    }

    /**
     * Set current error
     * @param {Error|null} error - Error object
     */
    setError(error) {
        this.set('currentError', error);
    }

    /**
     * Clear current error
     */
    clearError() {
        this.set('currentError', null);
    }

    // Utility methods

    /**
     * Reset state to initial values
     */
    reset() {
        this.state = {
            configs: [],
            cameras: [],
            servers: {},
            currentConfig: null,
            activeJobs: new Map(),
            jobHistory: [],
            processedVideos: [],
            lastFileRefresh: null,
            isLoading: false,
            currentError: null
        };
        this.initializeState();
        
        // Notify all listeners
        this.listeners.forEach((listeners, key) => {
            listeners.forEach(callback => {
                try {
                    callback(this.state[key], undefined, key);
                } catch (error) {
                    console.error(`Error in state reset listener for ${key}:`, error);
                }
            });
        });
    }

    /**
     * Get state summary for debugging
     * @returns {Object} State summary
     */
    getSummary() {
        return {
            configsCount: this.state.configs.length,
            camerasCount: this.state.cameras.length,
            activeJobsCount: this.state.activeJobs.size,
            historyJobsCount: this.state.jobHistory.length,
            videosCount: this.state.processedVideos.length,
            isLoading: this.state.isLoading,
            hasError: !!this.state.currentError
        };
    }
}

// Export for ES6 module usage
export default AppState;
