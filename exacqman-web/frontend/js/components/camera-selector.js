/**
 * Camera Selector Component
 * 
 * Handles camera selection dropdown with real-time validation and updates.
 */

class CameraSelector {
    constructor(apiClient, stateManager) {
        this.api = apiClient;
        this.state = stateManager;
        this.selectElement = document.getElementById('camera-select');
        this.configSelect = document.getElementById('config-select');
        
        this.init();
    }

    /**
     * Initialize the camera selector
     */
    init() {
        console.log('CameraSelector init - selectElement:', this.selectElement);
        if (!this.selectElement) {
            console.warn('Camera selector element not found');
            return;
        }

        this.setupEventListeners();
        this.setupStateListeners();
        console.log('CameraSelector initialized successfully');
    }

    /**
     * Set up event listeners
     */
    setupEventListeners() {
        // Listen for config changes
        if (this.configSelect) {
            this.configSelect.addEventListener('change', (e) => {
                this.handleConfigChange(e.target.value);
            });
        }

        // Listen for camera selection changes
        this.selectElement.addEventListener('change', (e) => {
            this.handleCameraChange(e.target.value);
        });

        // Real-time validation
        this.selectElement.addEventListener('blur', () => {
            this.validateSelection();
        });
    }

    /**
     * Set up state listeners
     */
    setupStateListeners() {
        // The dropdown is filtered by the selected server, so re-render any
        // time the underlying camera list OR the selected server changes.
        // The full camera list is the source of truth in state; this
        // component derives a server-scoped view each time it renders.
        this.state.subscribe('cameras', () => this.renderCameras());
        this.state.subscribe('selectedServer', () => this.renderCameras());

        // Listen for loading state
        this.state.subscribe('isLoading', (isLoading) => {
            this.updateLoadingState(isLoading);
        });
    }

    /**
     * Handle configuration change
     */
    async handleConfigChange(configFile) {
        if (!configFile) {
            this.clearCameras();
            return;
        }

        try {
            this.state.setLoading(true);
            const cameras = await this.api.getCameras(configFile);
            this.state.updateCameras(cameras);
        } catch (error) {
            console.error('Failed to load cameras:', error);
            this.showError('Failed to load cameras for selected configuration');
            this.clearCameras();
        } finally {
            this.state.setLoading(false);
        }
    }

    /**
     * Handle camera selection change
     */
    handleCameraChange(cameraAlias) {
        if (cameraAlias) {
            this.state.set('selectedCamera', cameraAlias);
            this.clearError();
            
            // Save preference to localStorage
            window.LocalStorageService.savePreference('camera', cameraAlias);
        } else {
            this.state.set('selectedCamera', null);
        }
        
        this.validateSelection();
        this.updateExtractionButton();
    }

    /**
     * Render the camera dropdown filtered by the currently selected server.
     *
     * The full camera list (across all servers) lives in app state; this
     * method derives the visible subset each render. Labels drop the
     * "on <server>" suffix since every visible camera shares the same server.
     *
     * Selection priority after filtering:
     *   1. Sticky: keep the current selection if its alias still exists in
     *      the new filtered list. This makes "swap servers but keep the same
     *      alias" (e.g. ``dock-6`` on both ``gpa`` and ``ch``) feel natural.
     *   2. Saved preference: restore the last camera the user picked if it
     *      matches a camera on this server.
     *   3. Auto-select if there's only one camera on this server.
     *   4. Otherwise leave unselected and clear ``selectedCamera`` in state.
     */
    renderCameras() {
        if (!this.selectElement) return;

        const allCameras = this.state.get('cameras') || [];
        const selectedServer = this.state.get('selectedServer');

        // No cameras loaded yet (e.g. before a config is chosen).
        if (allCameras.length === 0) {
            this.selectElement.innerHTML = '<option value="">No cameras available</option>';
            this.selectElement.disabled = true;
            this.selectElement.required = false;
            this.state.set('selectedCamera', null);
            return;
        }

        // Cameras loaded but no server picked yet -- nudge the user.
        if (!selectedServer) {
            this.selectElement.innerHTML = '<option value="">Select a server first...</option>';
            this.selectElement.disabled = true;
            this.selectElement.required = false;
            this.state.set('selectedCamera', null);
            return;
        }

        const cameras = allCameras.filter(c => c.server === selectedServer);

        if (cameras.length === 0) {
            this.selectElement.innerHTML = '<option value="">No cameras for this server</option>';
            this.selectElement.disabled = true;
            this.selectElement.required = false;
            this.state.set('selectedCamera', null);
            return;
        }

        const currentValue = this.selectElement.value;

        this.selectElement.innerHTML = '<option value="">Select camera...</option>';
        cameras.forEach(camera => {
            const option = document.createElement('option');
            option.value = camera.alias;
            option.textContent = `${camera.alias} (ID: ${camera.id})`;
            option.dataset.cameraId = camera.id;
            this.selectElement.appendChild(option);
        });

        this.selectElement.disabled = false;
        this.selectElement.required = true;

        const savedCamera = window.LocalStorageService.loadPreference('camera', null);
        const preferredCamera = savedCamera && cameras.some(c => c.alias === savedCamera)
            ? savedCamera : null;

        if (currentValue && cameras.some(c => c.alias === currentValue)) {
            this.selectElement.value = currentValue;
            this.handleCameraChange(currentValue);
            console.log('Sticky camera selection across server switch:', currentValue);
        } else if (preferredCamera) {
            this.selectElement.value = preferredCamera;
            this.handleCameraChange(preferredCamera);
            console.log('Restored saved camera preference:', preferredCamera);
        } else if (cameras.length === 1) {
            this.selectElement.value = cameras[0].alias;
            this.handleCameraChange(cameras[0].alias);
            console.log('Auto-selected camera:', cameras[0].alias);
        } else {
            this.state.set('selectedCamera', null);
        }

        this.clearError();
    }

    /**
     * Clear camera list
     */
    clearCameras() {
        if (!this.selectElement) return;

        this.selectElement.innerHTML = '<option value="">Waiting for configuration...</option>';
        this.selectElement.disabled = true;
        this.selectElement.required = false;
        this.state.set('selectedCamera', null);
    }

    /**
     * Validate current selection
     */
    validateSelection() {
        const selectedCamera = this.selectElement.value;
        const isValid = selectedCamera && selectedCamera !== '';
        
        if (!isValid && this.selectElement.value === '') {
            this.showError('Please select a camera');
        } else {
            this.clearError();
        }

        return isValid;
    }

    /**
     * Update extraction button state
     */
    updateExtractionButton() {
        const extractButton = document.getElementById('extract-button');
        if (!extractButton) return;

        const isFormValid = this.isFormReady();
        extractButton.disabled = !isFormValid;
    }

    /**
     * Check if form is ready for extraction
     */
    isFormReady() {
        const configSelected = this.configSelect && this.configSelect.value;
        const cameraSelected = this.selectElement && this.selectElement.value;
        
        return configSelected && cameraSelected;
    }

    /**
     * Update loading state
     */
    updateLoadingState(isLoading) {
        if (!this.selectElement) return;

        // Only update loading state if we're actually loading cameras, not files
        // Check if we have cameras loaded - if we do, don't corrupt the dropdown
        const cameras = this.state.get('cameras');
        if (cameras && cameras.length > 0) {
            // We have cameras loaded, don't corrupt the dropdown for file loading
            return;
        }

        this.selectElement.disabled = isLoading || !cameras.length;
        
        if (isLoading) {
            this.selectElement.innerHTML = '<option value="">Waiting for configuration...</option>';
        }
    }

    /**
     * Show error message
     */
    showError(message) {
        this.clearError();
        
        const errorElement = document.createElement('div');
        errorElement.className = 'field-error';
        errorElement.textContent = message;
        errorElement.style.color = 'var(--error-color)';
        errorElement.style.fontSize = 'var(--font-size-sm)';
        errorElement.style.marginTop = 'var(--spacing-1)';
        
        this.selectElement.parentNode.appendChild(errorElement);
        this.selectElement.classList.add('error');
    }

    /**
     * Clear error message
     */
    clearError() {
        const existingError = this.selectElement.parentNode.querySelector('.field-error');
        if (existingError) {
            existingError.remove();
        }
        this.selectElement.classList.remove('error');
    }

    /**
     * Get selected camera information
     */
    getSelectedCamera() {
        console.log('CameraSelector getSelectedCamera - selectElement:', this.selectElement);
        if (!this.selectElement) {
            console.log('CameraSelector getSelectedCamera - no selectElement');
            return null;
        }
        
        const selectedValue = this.selectElement.value;
        console.log('CameraSelector getSelectedCamera - selectedValue:', selectedValue);
        if (!selectedValue) {
            console.log('CameraSelector getSelectedCamera - no selectedValue');
            return null;
        }

        const selectedOption = this.selectElement.querySelector(`option[value="${selectedValue}"]`);
        console.log('CameraSelector getSelectedCamera - selectedOption:', selectedOption);
        if (!selectedOption) {
            console.log('CameraSelector getSelectedCamera - no selectedOption');
            return null;
        }

        const result = {
            alias: selectedValue,
            id: selectedOption.dataset.cameraId,
        };
        console.log('CameraSelector getSelectedCamera - result:', result);
        return result;
    }

    /**
     * Reset to default state
     */
    reset() {
        this.selectElement.value = '';
        this.clearError();
        this.state.set('selectedCamera', null);
    }
}

// Export for ES6 module usage
export default CameraSelector;
