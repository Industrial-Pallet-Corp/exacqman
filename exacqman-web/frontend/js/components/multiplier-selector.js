/**
 * Multiplier Selector Component
 * 
 * Handles timelapse multiplier selection with validation and preset options.
 */

class MultiplierSelector {
    constructor(stateManager) {
        this.state = stateManager;
        this.selectElement = document.getElementById('multiplier-select');
        
        this.multiplierOptions = [
            { value: 1, label: '1x (Real Time)', description: '1x speed - Real time playback' },
            { value: 2, label: '2x (Fast)', description: '2x speed - Fast timelapse' },
            { value: 5, label: '5x (Faster)', description: '5x speed - Faster timelapse' },
            { value: 10, label: '10x (Very Fast)', description: '10x speed - Very fast timelapse' },
            { value: 15, label: '15x (Ultra Fast)', description: '15x speed - Ultra fast timelapse' },
            { value: 20, label: '20x (Extreme)', description: '20x speed - Extreme timelapse' },
            { value: 25, label: '25x (Lightning)', description: '25x speed - Lightning fast' },
            { value: 30, label: '30x (Blazing)', description: '30x speed - Blazing fast' },
            { value: 40, label: '40x (Supersonic)', description: '40x speed - Supersonic timelapse' },
            { value: 50, label: '50x (Light Speed)', description: '50x speed - Light Speed timelapse' }
        ];
        
        this.init();
    }

    /**
     * Initialize the multiplier selector
     */
    init() {
        if (!this.selectElement) {
            console.warn('Multiplier selector element not found');
            return;
        }

        this.setupEventListeners();
        this.setupStateListeners();
        this.populateOptions();
        this.setDefaultValue();
    }

    /**
     * Set up event listeners
     */
    setupEventListeners() {
        // Multiplier change
        this.selectElement.addEventListener('change', (e) => {
            this.handleMultiplierChange(e.target.value);
        });

        // Real-time validation
        this.selectElement.addEventListener('blur', () => {
            this.validateSelection();
        });

        // Input events for real-time feedback
        this.selectElement.addEventListener('input', () => {
            this.clearError();
        });
    }

    /**
     * Set up state listeners
     */
    setupStateListeners() {
        // Listen for form validation state
        this.state.subscribe('selectedCamera', () => {
            this.updateExtractionButton();
        });
        this.state.subscribe('captionValid', () => {
            this.updateExtractionButton();
        });
    }

    /**
     * Populate multiplier options
     */
    populateOptions() {
        if (!this.selectElement) return;

        // Clear existing options
        this.selectElement.innerHTML = '';

        // Add options
        this.multiplierOptions.forEach(option => {
            const optionElement = document.createElement('option');
            optionElement.value = option.value;
            optionElement.textContent = option.label;
            optionElement.title = option.description;
            this.selectElement.appendChild(optionElement);
        });
    }

    /**
     * Set default value
     */
    setDefaultValue() {
        if (!this.selectElement) return;

        // Try to load saved preference first
        const savedMultiplier = window.LocalStorageService.loadPreference('timelapseMultiplier', 50);
        this.selectElement.value = savedMultiplier.toString();
        this.state.set('selectedMultiplier', savedMultiplier);
    }

    /**
     * Handle multiplier change
     */
    handleMultiplierChange(value) {
        const multiplier = parseInt(value);
        
        if (isNaN(multiplier)) {
            this.state.set('selectedMultiplier', null);
            return;
        }

        this.state.set('selectedMultiplier', multiplier);
        
        // Save preference to localStorage
        window.LocalStorageService.savePreference('timelapseMultiplier', multiplier);
        
        this.validateSelection();
        this.updateExtractionButton();
        this.updateDurationEstimate();
    }

    /**
     * Validate current selection
     */
    validateSelection() {
        const value = this.selectElement.value;
        const multiplier = parseInt(value);
        
        if (!value || isNaN(multiplier)) {
            this.showError('Please select a timelapse multiplier');
            return false;
        }

        // Since this is a dropdown with predefined valid options, no range validation needed
        this.clearError();
        return true;
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
        const configSelected = this.state.get('currentConfig');
        const cameraSelected = this.state.get('selectedCamera');
        const multiplierSelected = this.state.get('selectedMultiplier');
        const captionValid = this.state.get('captionValid');

        return (
            configSelected &&
            cameraSelected &&
            multiplierSelected &&
            captionValid !== false
        );
    }

    /**
     * Update duration estimate based on multiplier
     */
    updateDurationEstimate() {
        const multiplier = this.state.get('selectedMultiplier');
        const duration = this.state.get('extractionDuration');
        
        if (multiplier && duration) {
            const estimatedDuration = duration / multiplier;
            this.state.set('estimatedVideoDuration', estimatedDuration);
            this.state.set('estimatedVideoDurationFormatted', this.formatDuration(estimatedDuration));
        }
    }

    /**
     * Format duration in human-readable format
     */
    formatDuration(milliseconds) {
        const seconds = Math.floor(milliseconds / 1000);
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);
        
        if (hours > 0) {
            return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
        } else if (minutes > 0) {
            return `${minutes}m ${seconds % 60}s`;
        } else {
            return `${seconds}s`;
        }
    }

    /**
     * Get current multiplier value
     */
    getValue() {
        return this.state.get('selectedMultiplier');
    }

    /**
     * Set multiplier value
     */
    setValue(multiplier) {
        if (this.multiplierOptions.find(opt => opt.value === multiplier)) {
            this.selectElement.value = multiplier;
            this.state.set('selectedMultiplier', multiplier);
            this.updateDurationEstimate();
        }
    }

    /**
     * Get multiplier information
     */
    getMultiplierInfo() {
        const multiplier = this.state.get('selectedMultiplier');
        const option = this.multiplierOptions.find(opt => opt.value === multiplier);
        
        return {
            value: multiplier,
            label: option ? option.label : 'Unknown',
            description: option ? option.description : '',
            isValid: multiplier && multiplier >= 1 && multiplier <= 50
        };
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
     * Reset to default value
     */
    reset() {
        this.setDefaultValue();
        this.clearError();
    }

    /**
     * Get all available options
     */
    getOptions() {
        return [...this.multiplierOptions];
    }

    /**
     * Add custom multiplier option
     */
    addCustomOption(value, label, description) {
        if (value >= 1 && value <= 50 && !this.multiplierOptions.find(opt => opt.value === value)) {
            const option = { value, label, description };
            this.multiplierOptions.push(option);
            this.multiplierOptions.sort((a, b) => a.value - b.value);
            this.populateOptions();
        }
    }

    /**
     * Remove custom multiplier option
     */
    removeCustomOption(value) {
        const index = this.multiplierOptions.findIndex(opt => opt.value === value);
        if (index > -1 && value !== 50) { // Don't remove default 50x
            this.multiplierOptions.splice(index, 1);
            this.populateOptions();
        }
    }

    /**
     * Get recommended multiplier based on duration
     */
    getRecommendedMultiplier(durationMs) {
        const durationHours = durationMs / (1000 * 60 * 60);
        
        if (durationHours <= 1) {
            return 2; // Fast for short durations
        } else if (durationHours <= 4) {
            return 10; // Very fast for medium durations
        } else if (durationHours <= 12) {
            return 20; // Extreme for long durations
        } else {
            return 30; // Blazing for very long durations
        }
    }

    /**
     * Auto-select recommended multiplier
     */
    autoSelectRecommended() {
        const duration = this.state.get('extractionDuration');
        if (duration) {
            const recommended = this.getRecommendedMultiplier(duration);
            this.setValue(recommended);
        }
    }
}

// Export for ES6 module usage
export default MultiplierSelector;
