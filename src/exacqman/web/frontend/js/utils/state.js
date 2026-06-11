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
            cameras: [],
            servers: {},
            currentConfig: null,

            // Per-server reachability for the current config, refreshed on
            // config change and on demand. Shape: {servers: {name: {reachable,
            // detail}}, summary: 'all'|'some'|'none'|null}.
            connectivity: { servers: {}, summary: null },

            // Job queue (server-authoritative, refreshed by the queue poller)
            //
            //   queue.running:  the single Job currently being processed (or null)
            //   queue.waiting:  FIFO list of queued Jobs (max 3)
            //   queueFull:      derived flag, true when running != null && waiting.length >= 3
            //   sessionJobs:    Map<jobId, Job> of every job this client has
            //                   observed since page load. Persists local copies
            //                   of jobs that have aged out of the server snapshot
            //                   (so terminal results stay visible until refresh).
            //   lastPollTime:   server_time from the latest snapshot; sent as
            //                   ?since= on the next poll to scope terminal jobs
            //                   to transitions that occurred since then.
            queue: { running: null, waiting: [] },
            queueFull: false,
            sessionJobs: new Map(),
            lastPollTime: null,

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

    // Job queue management

    /**
     * Integrate the latest server snapshot into local state.
     *
     * Called by the queue poller on each successful tick. The snapshot is
     * server-authoritative for running + waiting; terminal entries are
     * merged into sessionJobs so they stick around until page refresh
     * even after the server's TTL expires.
     *
     * @param {{running: Object|null, waiting: Object[], terminal: Object[], server_time: string}} snapshot
     */
    updateFromSnapshot(snapshot) {
        const running = snapshot.running || null;
        const waiting = Array.isArray(snapshot.waiting) ? snapshot.waiting : [];
        const terminal = Array.isArray(snapshot.terminal) ? snapshot.terminal : [];

        // Upsert every job we just observed into sessionJobs so the
        // client retains its own copy after the server forgets the
        // terminal entry. Iterate terminal LAST so its later-stamped
        // status wins when a job appears in both lists during a race.
        const sessionJobs = new Map(this.state.sessionJobs);
        const stamp = (job) => sessionJobs.set(job.id, { ...job });
        if (running) stamp(running);
        waiting.forEach(stamp);
        terminal.forEach(stamp);

        // The two cap-related signals we publish: queue.waiting.length
        // and queueFull. Form-gating components subscribe to queueFull.
        const queueFull = !!running && waiting.length >= 3;

        this.update({
            queue: { running, waiting },
            queueFull,
            sessionJobs,
            lastPollTime: snapshot.server_time || new Date().toISOString(),
        });
    }

    /**
     * All jobs this client has ever observed this session, sorted for display
     * in chronological top-to-bottom order:
     *   1. waiting jobs, most-recently-queued on top (so the newest enqueued
     *      job sits at the very top and the next-to-run job sits just above
     *      the active one);
     *   2. the single running ("active") job in the middle;
     *   3. terminal (completed / failed) jobs newest-first, so the oldest
     *      finished job lands at the very bottom.
     * @returns {Object[]}
     */
    getSessionJobsForDisplay() {
        const sessionJobs = this.state.sessionJobs;
        const { running, waiting } = this.state.queue;

        const result = [];
        const taken = new Set();

        // Queued jobs at the top. `waiting` is in FIFO/queue order (index 0 is
        // next to run, i.e. the earliest-queued); reverse it so the most
        // recently queued job is on top and the next-to-run job sits closest
        // to the active job below.
        for (let i = waiting.length - 1; i >= 0; i--) {
            const job = waiting[i];
            if (sessionJobs.has(job.id) && !taken.has(job.id)) {
                result.push(sessionJobs.get(job.id));
                taken.add(job.id);
            }
        }

        // Active job in the middle.
        if (running && sessionJobs.has(running.id) && !taken.has(running.id)) {
            result.push(sessionJobs.get(running.id));
            taken.add(running.id);
        }

        // Any remaining jobs in sessionJobs are terminal (or jobs the
        // current snapshot didn't include, which means they aged out
        // server-side). Sort newest-completed first so the oldest finished
        // job sits at the very bottom of the list.
        const terminal = [];
        sessionJobs.forEach((job, id) => {
            if (!taken.has(id)) terminal.push(job);
        });
        terminal.sort((a, b) => {
            const aTime = new Date(a.completed_at || a.created_at).getTime();
            const bTime = new Date(b.completed_at || b.created_at).getTime();
            return bTime - aTime;
        });
        return result.concat(terminal);
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
            cameras: [],
            servers: {},
            currentConfig: null,
            connectivity: { servers: {}, summary: null },
            queue: { running: null, waiting: [] },
            queueFull: false,
            sessionJobs: new Map(),
            lastPollTime: null,
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
        const { running, waiting } = this.state.queue;
        return {
            camerasCount: this.state.cameras.length,
            runningJobs: running ? 1 : 0,
            waitingJobs: waiting.length,
            sessionJobsCount: this.state.sessionJobs.size,
            queueFull: this.state.queueFull,
            videosCount: this.state.processedVideos.length,
            isLoading: this.state.isLoading,
            hasError: !!this.state.currentError
        };
    }
}

// Export for ES6 module usage
export default AppState;
