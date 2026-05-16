/**
 * ExacqMan Web Application
 * 
 * Main application entry point and orchestration.
 * Coordinates between UI components, state management, and API client.
 */

import { ExacqManAPI, APIError } from './api.js';
import AppState from './utils/state.js';
import ValidationUtils from './utils/validation.js';
import CameraSelector from './components/camera-selector.js';
import DateTimePicker from './components/datetime-picker.js';
import MultiplierSelector from './components/multiplier-selector.js';
import BoundedTextInput from './components/bounded-text-input.js';
import JobStatus from './components/job-status.js';
import FileBrowser from './components/file-browser.js';

class ExacqManApp {
    constructor() {
        this.api = new ExacqManAPI();
        this.state = new AppState();
        this.jobPoller = null;
        this.isInitialized = false;
        
        // Initialize components
        this.cameraSelector = null;
        this.dateTimePicker = null;
        this.multiplierSelector = null;
        this.captionInput = null;
        this.filenameInput = null;
        this.jobStatus = null;
        this.fileBrowser = null;
        
        // Bind methods to preserve context
        this.handleConfigChange = this.handleConfigChange.bind(this);
        this.handleServerChange = this.handleServerChange.bind(this);
        this.handleExtractionSubmit = this.handleExtractionSubmit.bind(this);
        this.handleFileDownload = this.handleFileDownload.bind(this);
        this.handleFileDelete = this.handleFileDelete.bind(this);
        this.removeJob = this.removeJob.bind(this);
        
        this.init();
    }

    /**
     * Initialize the application
     */
    async init() {
        try {
            console.log('Initializing ExacqMan Web Application...');
            
            // Initialize components
            this.initializeComponents();
            
            // Set up event listeners
            this.setupEventListeners();
            
            // Set up state listeners
            this.setupStateListeners();
            
            // Load saved preferences
            this.loadPreferences();
            
            // Test API connection
            await this.testConnection();
            
            // Load initial data
            await this.loadInitialData();
            
            this.isInitialized = true;
            console.log('Application initialized successfully');
            
        } catch (error) {
            console.error('Failed to initialize application:', error);
            this.showError('Failed to initialize application. Please refresh the page.');
        }
    }

    /**
     * Initialize UI components
     */
    initializeComponents() {
        this.cameraSelector = new CameraSelector(this.api, this.state);
        this.dateTimePicker = new DateTimePicker(this.state);
        this.multiplierSelector = new MultiplierSelector(this.state);
        this.captionInput = new BoundedTextInput(this.state, {
            inputId: 'caption-input',
            counterId: 'caption-counter',
            maxLength: 30,
            valueStateKey: 'selectedCaption',
            validStateKey: 'captionValid',
            storageKey: 'caption',
        });
        this.filenameInput = new BoundedTextInput(this.state, {
            inputId: 'filename-input',
            counterId: 'filename-counter',
            maxLength: 30,
            valueStateKey: 'selectedFilename',
            validStateKey: 'filenameValid',
            // Intentionally no storageKey: filename is per-run; clearing it
            // after each extraction lets the backend auto-generate a fresh
            // {date}_{time}_{server}_{camera}_{multiplier}x name.
        });
        this.jobStatus = new JobStatus(this.api, this.state);
        this.fileBrowser = new FileBrowser(this.api, this.state);
    }

    /**
     * Set up DOM event listeners
     */
    setupEventListeners() {
        // Configuration change
        const configSelect = document.getElementById('config-select');
        if (configSelect) {
            configSelect.addEventListener('change', this.handleConfigChange);
        }

        // Server selection change
        const serverSelect = document.getElementById('server-select');
        if (serverSelect) {
            serverSelect.addEventListener('change', this.handleServerChange);
        }

        // Extraction form submission
        const extractionForm = document.getElementById('extraction-form');
        if (extractionForm) {
            extractionForm.addEventListener('submit', this.handleExtractionSubmit);
        }


        // Set default datetime values
        this.setDefaultDateTimeValues();
    }

    /**
     * Set up state change listeners
     */
    setupStateListeners() {

        // Loading state
        this.state.subscribe('isLoading', (isLoading) => {
            this.updateLoadingState(isLoading);
        });

        // Error state
        this.state.subscribe('currentError', (error) => {
            if (error) {
                this.showError(error.getUserMessage ? error.getUserMessage() : error.message);
            }
        });

        // Active jobs
        this.state.subscribe('activeJobs', (jobs) => {
            this.updateJobDisplay();
        });

        // Processed videos - handled by FileBrowser component

        // Filename placeholder mirrors the backend's auto-generated stem,
        // rebuilt whenever any of its inputs change. All four inputs are
        // state-published (selectedServer is mirrored from server-select by
        // populateServerSelect / handleServerChange / clearServerSelection)
        // so whichever update settles last triggers the build with all
        // pieces present -- no init-time nudges required.
        this.state.subscribe('selectedCamera', () => this.updateFilenamePlaceholder());
        this.state.subscribe('selectedMultiplier', () => this.updateFilenamePlaceholder());
        this.state.subscribe('startDateTime', () => this.updateFilenamePlaceholder());
        this.state.subscribe('selectedServer', () => this.updateFilenamePlaceholder());
    }

    /**
     * Rebuild the Filename input's placeholder from the current selections.
     *
     * Mirrors backend ``ExacqManService._generate_output_filename``:
     * ``{YYYY-MM-DD}_{HHMM}_{server}_{camera}_{N}x`` (HHMM is 24-hour).
     * Leaves the existing placeholder in place until all four inputs are
     * populated so the user sees a coherent example rather than a stem with
     * ``?`` placeholders during initial page load.
     */
    updateFilenamePlaceholder() {
        const filenameInput = document.getElementById('filename-input');
        if (!filenameInput) return;

        const camera = this.state.get('selectedCamera');
        const multiplier = this.state.get('selectedMultiplier');
        const startDateTime = this.state.get('startDateTime');
        const server = this.state.get('selectedServer');

        if (!camera || !multiplier || !startDateTime || !server) return;

        const date = new Date(startDateTime);
        if (Number.isNaN(date.getTime())) return;

        const yyyy = date.getFullYear();
        const mm = String(date.getMonth() + 1).padStart(2, '0');
        const dd = String(date.getDate()).padStart(2, '0');

        const hh = String(date.getHours()).padStart(2, '0');
        const min = String(date.getMinutes()).padStart(2, '0');
        const timeStr = `${hh}${min}`;

        const sanitize = (s) => String(s).toLowerCase().replace(/\s+/g, '-');

        filenameInput.placeholder =
            `${yyyy}-${mm}-${dd}_${timeStr}_${sanitize(server)}_${sanitize(camera)}_${multiplier}x`;
    }

    /**
     * Test API connection
     */
    async testConnection() {
        try {
            this.state.setLoading(true);
            const isConnected = await this.api.testConnection();
            
            if (!isConnected) {
                throw new Error('Unable to connect to server');
            }
            
        } catch (error) {
            console.error('Connection test failed:', error);
            throw error;
        } finally {
            this.state.setLoading(false);
        }
    }

    /**
     * Load initial application data
     */
    async loadInitialData() {
        try {
            this.state.setLoading(true);
            
            // Load configuration files from API
            const configFiles = await this.api.getAvailableConfigs();
            const configs = configFiles.map(file => ({
                name: file,
                path: file
            }));
            
            this.state.updateConfigs(configs);
            this.populateConfigSelect(configs);
            
            // Load processed videos
            await this.loadProcessedVideos();
            
        } catch (error) {
            console.error('Failed to load initial data:', error);
            throw error;
        } finally {
            this.state.setLoading(false);
        }
    }

    /**
     * Set default datetime values
     */
    setDefaultDateTimeValues() {
        const startInput = document.getElementById('start-datetime');
        const endInput = document.getElementById('end-datetime');
        
        if (startInput && endInput) {
            startInput.value = this.state.get('defaultStartTime');
            endInput.value = this.state.get('defaultEndTime');
        }
    }

    /**
     * Load saved preferences from localStorage
     */
    loadPreferences() {
        try {
            const preferences = window.LocalStorageService.loadPreferences();
            
            // Note: Config preference is loaded in populateConfigSelect()
            // Server and camera preferences are loaded when their respective
            // components are populated (in populateServerSelect and CameraSelector.renderCameras)
            // Multiplier preference is loaded in MultiplierSelector.setDefaultValue()
            
        } catch (error) {
            console.error('Failed to load preferences:', error);
        }
    }

    /**
     * Handle server selection change
     */
    handleServerChange(event) {
        const server = event.target.value;
        // Publish to state for reactive consumers (filename placeholder,
        // validators, form data). Empty selection -> null so subscribers can
        // treat "no server" uniformly.
        this.state.set('selectedServer', server || null);
        if (server) {
            window.LocalStorageService.savePreference('server', server);
        }
    }

    /**
     * Handle configuration file change
     */
    async handleConfigChange(event) {
        const configFile = event.target.value;
        console.log('Config changed to:', configFile);
        
        if (!configFile) {
            this.state.updateCameras([]);
            this.state.updateServers({});
            this.state.setCurrentConfig(null);
            // CameraSelector component will handle camera dropdown via state subscription
            this.clearServerSelection();
            return;
        }

        try {
            this.state.setLoading(true);
            this.state.setCurrentConfig(configFile);
            
            // Save preference to localStorage
            window.LocalStorageService.savePreference('configFile', configFile);
            
            console.log('Loading cameras and config for:', configFile);
            
            // Load cameras and servers for selected config
            const [cameras, configInfo] = await Promise.all([
                this.api.getCameras(configFile),
                this.api.getConfigInfo(configFile)
            ]);
            
            console.log('Loaded cameras:', cameras);
            console.log('Loaded config info:', configInfo);
            
            this.state.updateCameras(cameras);
            this.state.updateServers(configInfo.servers || {});
            
            // CameraSelector component will handle camera dropdown via state subscription
            this.populateServerSelect(configInfo.servers || {});
            
            console.log('Camera select populated with', cameras.length, 'cameras');
            
        } catch (error) {
            console.error('Failed to load configuration:', error);
            this.showError('Failed to load configuration. Please try again.');
        } finally {
            this.state.setLoading(false);
        }
    }

    /**
     * Handle extraction form submission
     */
    async handleExtractionSubmit(event) {
        event.preventDefault();
        
        try {
            // Validate form using components
            if (!this.validateForm()) {
                return;
            }

            this.state.setLoading(true);
            
            // Get form data from components
            const formData = this.getFormData();
            console.log('Form data being sent:', formData);
            
            // Submit extraction request
            const response = await this.api.extractVideo(formData);
            
            if (response.success && response.data?.job_id) {
                // Add job to tracking
                this.state.addJob(response.data.job_id, {
                    status: 'queued',
                    message: 'Job queued for processing',
                    progress: 0,
                    request: formData
                });
                
                // Start polling for job status
                this.jobStatus.startPolling(response.data.job_id);
                
                this.showSuccess('Video extraction started successfully');
                
                // Reset form AFTER successful submission
                this.resetExtractionForm();
                
            } else {
                throw new Error('Invalid response from server');
            }
            
        } catch (error) {
            console.error('Extraction failed:', error);
            this.showError(error.getUserMessage ? error.getUserMessage() : 'Failed to start extraction');
        } finally {
            this.state.setLoading(false);
        }
    }


    /**
     * Handle file download
     */
    async handleFileDownload(filename) {
        try {
            const url = this.api.getDownloadURL(filename);
            const link = document.createElement('a');
            link.href = url;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        } catch (error) {
            console.error('Download failed:', error);
            this.showError('Failed to download file');
        }
    }

    /**
     * Handle file deletion
     */
    async handleFileDelete(filename) {
        if (!confirm(`Are you sure you want to delete "${filename}"?`)) {
            return;
        }

        try {
            this.state.setLoading(true);
            await this.api.deleteVideo(filename);
            this.state.removeProcessedVideo(filename);
            this.showSuccess('File deleted successfully');
        } catch (error) {
            console.error('Delete failed:', error);
            this.showError('Failed to delete file');
        } finally {
            this.state.setLoading(false);
        }
    }

    // Helper methods

    /**
     * Validate form using components
     */
    validateForm() {
        const configValid = this.validateConfigSelection();
        const cameraValid = this.validateCameraSelection();
        const datetimeValid = this.dateTimePicker?.validateBoth();
        const multiplierValid = this.multiplierSelector?.validateSelection();
        const serverValid = this.validateServerSelection();
        const captionValid = this.captionInput?.isValid() ?? true;
        const filenameValid = this.filenameInput?.isValid() ?? true;

        if (!captionValid) {
            this.showError('Caption is too long. Maximum 30 characters.');
        } else if (!filenameValid) {
            this.showError('Filename is too long. Maximum 30 characters.');
        }

        console.log('Form validation:', {
            configValid,
            cameraValid,
            datetimeValid,
            multiplierValid,
            serverValid,
            captionValid,
            filenameValid,
            cameraSelectValue: this.cameraSelector?.getSelectedCamera()?.alias
        });

        return configValid && cameraValid && datetimeValid && multiplierValid &&
            serverValid && captionValid && filenameValid;
    }

    /**
     * Validate server selection
     */
    validateServerSelection() {
        const serverSelect = document.getElementById('server-select');
        if (!serverSelect) return false;

        // Value lives in state; the DOM element is only needed as the anchor
        // for inline error styling via ValidationUtils.
        const selectedServer = this.state.get('selectedServer');
        if (!selectedServer || !selectedServer.trim()) {
            ValidationUtils.showFieldError(serverSelect, 'Please select a server');
            return false;
        }

        ValidationUtils.clearFieldError(serverSelect);
        return true;
    }

    /**
     * Validate configuration selection
     */
    validateConfigSelection() {
        const configSelect = document.getElementById('config-select');
        if (!configSelect) return false;
        
        const selectedConfig = configSelect.value;
        if (!selectedConfig || selectedConfig.trim() === '') {
            ValidationUtils.showFieldError(configSelect, 'Please select a configuration file');
            return false;
        }
        
        ValidationUtils.clearFieldError(configSelect);
        return true;
    }

    /**
     * Validate camera selection
     */
    validateCameraSelection() {
        const cameraSelect = document.getElementById('camera-select');
        if (!cameraSelect) return false;
        
        const selectedCamera = cameraSelect.value;
        if (!selectedCamera || selectedCamera.trim() === '') {
            ValidationUtils.showFieldError(cameraSelect, 'Please select a camera');
            return false;
        }
        
        ValidationUtils.clearFieldError(cameraSelect);
        return true;
    }

    /**
     * Get form data from components
     */
    getFormData() {
        const configFile = this.state.get('currentConfig');
        
        // Get camera selection from CameraSelector component
        const cameraInfo = this.cameraSelector?.getSelectedCamera();
        const selectedCameraAlias = cameraInfo?.alias || null;
        
        const datetimeValues = this.dateTimePicker?.getValues();
        const multiplier = this.multiplierSelector?.getValue();
        const server = this.state.get('selectedServer');
        const caption = this.captionInput?.getValue() ?? null;
        const filename = this.filenameInput?.getValue() ?? null;

        console.log('Form data components:', {
            configFile,
            cameraInfo,
            selectedCameraAlias,
            datetimeValues,
            multiplier,
            server,
            caption,
            filename
        });

        return {
            camera_alias: selectedCameraAlias || null,  // Convert undefined to null
            start_datetime: datetimeValues?.start_datetime,
            end_datetime: datetimeValues?.end_datetime,
            timelapse_multiplier: multiplier,
            config_file: configFile,
            server: server,  // Server is now required, so no fallback to null
            caption: caption,
            filename: filename
        };
    }


    /**
     * Load processed videos
     */
    async loadProcessedVideos() {
        try {
            const videos = await this.api.getProcessedVideos();
            this.state.updateProcessedVideos(videos);
        } catch (error) {
            console.error('Failed to load videos:', error);
            this.showError('Failed to load video files');
        }
    }


    // UI update methods


    /**
     * Update loading state
     */
    updateLoadingState(isLoading) {
        const extractButton = document.getElementById('extract-button');
        if (extractButton) {
            extractButton.disabled = isLoading;
            const btnText = extractButton.querySelector('.btn-text');
            const btnLoading = extractButton.querySelector('.btn-loading');
            
            if (btnText && btnLoading) {
                btnText.style.display = isLoading ? 'none' : 'inline';
                btnLoading.style.display = isLoading ? 'inline' : 'none';
            }
        }
    }

    /**
     * Populate configuration select
     */
    populateConfigSelect(configs) {
        const select = document.getElementById('config-select');
        if (!select) return;
        
        select.innerHTML = '<option value="">Select configuration...</option>';
        configs.forEach(config => {
            const option = document.createElement('option');
            option.value = config.path;
            option.textContent = config.name;
            select.appendChild(option);
        });
        select.disabled = false;
        select.required = true;
        
        // Try to restore saved preference first
        const savedConfig = window.LocalStorageService.loadPreference('configFile', null);
        const preferredConfig = savedConfig && configs.some(config => config.path === savedConfig) ? savedConfig : null;
        
        // Auto-select if only one configuration
        if (configs.length === 1) {
            select.value = configs[0].path;
            this.handleConfigChange({ target: { value: configs[0].path } });
            console.log('Auto-selected configuration:', configs[0].name);
        }
        // Use saved preference if available and valid
        else if (preferredConfig) {
            select.value = preferredConfig;
            this.handleConfigChange({ target: { value: preferredConfig } });
            console.log('Restored saved config preference:', preferredConfig);
        }
    }


    /**
     * Populate server select
     */
    populateServerSelect(servers) {
        const select = document.getElementById('server-select');
        if (!select) return;
        
        select.innerHTML = '<option value="">Select server...</option>';
        const serverEntries = Object.entries(servers);
        
        serverEntries.forEach(([name, ip]) => {
            const option = document.createElement('option');
            option.value = name;
            option.textContent = `${name} (${ip})`;
            select.appendChild(option);
        });
        
        select.disabled = serverEntries.length === 0;
        select.required = serverEntries.length > 0;
        
        // Try to load saved preference first
        const savedServer = window.LocalStorageService.loadPreference('server', null);
        const preferredServer = savedServer && serverEntries.some(([name]) => name === savedServer) ? savedServer : null;
        
        // Auto-select if only one server
        if (serverEntries.length === 1) {
            select.value = serverEntries[0][0];
            console.log('Auto-selected server:', serverEntries[0][0]);
        }
        // Use saved preference if available and valid
        else if (preferredServer) {
            select.value = preferredServer;
            console.log('Restored saved server preference:', preferredServer);
        }

        // Programmatic <select>.value assignments do not fire `change`, so
        // publish to state explicitly. This is the moment downstream
        // subscribers (e.g. the filename placeholder) get the final piece
        // they need to render on initial page load.
        this.state.set('selectedServer', select.value || null);
    }

    /**
     * Clear server selection
     */
    clearServerSelection() {
        const select = document.getElementById('server-select');
        if (!select) return;

        select.innerHTML = '<option value="">Waiting for configuration...</option>';
        select.disabled = true;
        select.required = false;
        this.state.set('selectedServer', null);
    }

    /**
     * Update job display
     */
    updateJobDisplay() {
        const jobList = document.getElementById('job-list');
        if (!jobList) return;
        
        const jobs = this.state.getActiveJobs();
        
        if (jobs.length === 0) {
            jobList.innerHTML = '<div class="no-jobs">No active jobs</div>';
            return;
        }
        
        jobList.innerHTML = jobs.map(job => this.createJobElement(job)).join('');
    }

    /**
     * Create job element HTML
     */
    createJobElement(job) {
        return `
            <div class="job-item ${job.status}">
                <div class="job-header">
                    <span class="job-status ${job.status}">${job.status}</span>
                </div>
                <div class="job-progress">
                    <div class="job-progress-bar" style="width: ${job.progress || 0}%"></div>
                </div>
                <div class="job-message">${job.message || ''}</div>
            </div>
        `;
    }


    /**
     * Remove job from tracking
     */
    removeJob(jobId) {
        this.state.removeJob(jobId);
    }

    /**
     * Reset extraction form
     */
    resetExtractionForm() {
        // Reset form first (this clears any validation errors)
        const form = document.getElementById('extraction-form');
        if (form) {
            form.reset();
        }
        
        // Then set default values and restore preferences
        this.dateTimePicker?.setDefaultValues();

        // Reset multiplier to saved preference (this will load from localStorage)
        this.multiplierSelector?.reset();

        // Restore caption from saved preference (form.reset() wipes the input)
        this.captionInput?.reset();

        // Filename has no storage key, so reset() just clears it back to
        // empty so the next extraction auto-generates a fresh name.
        this.filenameInput?.reset();
    }


    /**
     * Show success message
     */
    showSuccess(message) {
        this.showToast(message, 'success');
    }

    /**
     * Show error message
     */
    showError(message) {
        this.showToast(message, 'error');
    }

    /**
     * Show toast notification
     */
    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        if (!container) return;
        
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;
        
        container.appendChild(toast);
        
        // Trigger animation
        setTimeout(() => toast.classList.add('show'), 100);
        
        // Auto remove
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => container.removeChild(toast), 300);
        }, 5000);
    }
}


// Initialize application when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.app = new ExacqManApp();
});
