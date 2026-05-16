/**
 * DateTime Picker Component
 * 
 * Handles start and end datetime selection with validation and smart defaults.
 */

import DateUtils from '../utils/date-utils.js';

class DateTimePicker {
    constructor(stateManager) {
        this.state = stateManager;
        this.startInput = document.getElementById('start-datetime');
        this.endInput = document.getElementById('end-datetime');
        
        this.init();
    }

    /**
     * Initialize the datetime picker
     */
    init() {
        if (!this.startInput || !this.endInput) {
            console.warn('DateTime picker elements not found');
            return;
        }

        this.setupEventListeners();
        this.setupStateListeners();
        this.setDefaultValues();
    }

    /**
     * Set up event listeners
     */
    setupEventListeners() {
        // Start datetime change
        this.startInput.addEventListener('change', () => {
            this.handleStartChange();
        });

        // End datetime change
        this.endInput.addEventListener('change', () => {
            this.handleEndChange();
        });

        // Real-time validation
        this.startInput.addEventListener('blur', () => {
            this.validateStartTime();
        });

        this.endInput.addEventListener('blur', () => {
            this.validateEndTime();
        });

        // Input events for real-time feedback
        this.startInput.addEventListener('input', () => {
            this.clearError(this.startInput);
        });

        this.endInput.addEventListener('input', () => {
            this.clearError(this.endInput);
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
    }

    /**
     * Set default datetime values
     */
    setDefaultValues() {
        const now = new Date();
        const fifteenMinutesAgo = new Date(now.getTime() - 15 * 60 * 1000);

        this.startInput.value = DateUtils.formatForInput(fifteenMinutesAgo);
        this.endInput.value = DateUtils.formatForInput(now);

        this.updateState();
    }

    /**
     * Handle start datetime change
     */
    handleStartChange() {
        this.validateStartTime();
        this.validateBoth(); // Also validate the range
        this.updateState();
        this.updateExtractionButton();
    }

    /**
     * Handle end datetime change
     */
    handleEndChange() {
        this.validateEndTime();
        this.validateBoth(); // Also validate the range
        this.updateState();
        this.updateExtractionButton();
    }

    /**
     * Validate start datetime
     */
    validateStartTime() {
        const value = this.startInput.value;
        
        if (!value) {
            this.showError(this.startInput, 'Start time is required');
            return false;
        }

        const startDate = DateUtils.parseFromInput(value);
        const now = new Date();

        if (isNaN(startDate.getTime())) {
            this.showError(this.startInput, 'Invalid start time format');
            return false;
        }

        if (startDate > now) {
            this.showError(this.startInput, 'Start time cannot be in the future');
            return false;
        }

        // Check if end time is valid and start is before end
        const endValue = this.endInput.value;
        if (endValue) {
            const endDate = DateUtils.parseFromInput(endValue);
            if (!isNaN(endDate.getTime()) && endDate <= startDate) {
                this.showError(this.startInput, 'Start time must be before end time');
                return false;
            }
        }

        this.clearError(this.startInput);
        return true;
    }

    /**
     * Validate end datetime
     */
    validateEndTime() {
        const value = this.endInput.value;
        
        if (!value) {
            this.showError(this.endInput, 'End time is required');
            return false;
        }

        const endDate = DateUtils.parseFromInput(value);
        const now = new Date();

        if (isNaN(endDate.getTime())) {
            this.showError(this.endInput, 'Invalid end time format');
            return false;
        }

        if (endDate > now) {
            this.showError(this.endInput, 'End time cannot be in the future');
            return false;
        }

        // Check if start time is valid and end is after start
        const startValue = this.startInput.value;
        if (startValue) {
            const startDate = DateUtils.parseFromInput(startValue);
            if (!isNaN(startDate.getTime()) && endDate <= startDate) {
                this.showError(this.endInput, 'End time must be after start time');
                return false;
            }
        }

        this.clearError(this.endInput);
        return true;
    }

    /**
     * Validate both datetime inputs
     */
    validateBoth() {
        const startValid = this.validateStartTime();
        const endValid = this.validateEndTime();

        if (startValid && endValid) {
            // Final validation of the range
            const validation = DateUtils.validateRange(this.startInput.value, this.endInput.value);
            
            if (!validation.valid) {
                this.showError(this.endInput, validation.message);
                return false;
            }
        }
        
        return startValid && endValid;
    }

    /**
     * Update state with current values
     */
    updateState() {
        const startValue = this.startInput.value;
        const endValue = this.endInput.value;
        
        this.state.set('startDateTime', startValue);
        this.state.set('endDateTime', endValue);
        
        if (startValue && endValue) {
            const startDate = DateUtils.parseFromInput(startValue);
            const endDate = DateUtils.parseFromInput(endValue);
            const duration = endDate - startDate;
            
            this.state.set('extractionDuration', duration);
            this.state.set('extractionDurationFormatted', DateUtils.formatDuration(duration));
        }
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
        
        // Check if fields have values and are valid without calling validation methods
        // (which would clear errors)
        const startValue = this.startInput.value;
        const endValue = this.endInput.value;
        
        // Check individual field validity without clearing errors
        const startValid = startValue && this.isStartTimeValid();
        const endValid = endValue && this.isEndTimeValid();
        
        // Also check range validation if both times are valid
        let rangeValid = true;
        if (startValid && endValid) {
            const validation = DateUtils.validateRange(this.startInput.value, this.endInput.value);
            rangeValid = validation.valid;
        }

        const formReady = configSelected && cameraSelected && startValid && endValid && rangeValid;


        return formReady;
    }

    /**
     * Check if start time is valid without clearing errors
     */
    isStartTimeValid() {
        const startValue = this.startInput.value;
        if (!startValue) return false;

        const startDate = DateUtils.parseFromInput(startValue);
        if (isNaN(startDate.getTime())) return false;

        const now = new Date();
        if (startDate > now) return false;

        // Check if end time is valid and start is before end
        const endValue = this.endInput.value;
        if (endValue) {
            const endDate = DateUtils.parseFromInput(endValue);
            if (!isNaN(endDate.getTime()) && endDate <= startDate) return false;
        }

        return true;
    }

    /**
     * Check if end time is valid without clearing errors
     */
    isEndTimeValid() {
        const endValue = this.endInput.value;
        if (!endValue) return false;

        const endDate = DateUtils.parseFromInput(endValue);
        if (isNaN(endDate.getTime())) return false;

        const now = new Date();
        if (endDate > now) return false;

        // Check if start time is valid and end is after start
        const startValue = this.startInput.value;
        if (startValue) {
            const startDate = DateUtils.parseFromInput(startValue);
            if (!isNaN(startDate.getTime()) && endDate <= startDate) return false;
        }

        return true;
    }

    /**
     * Get current datetime values
     */
    getValues() {
        return {
            start_datetime: this.startInput.value,
            end_datetime: this.endInput.value
        };
    }

    /**
     * Set datetime values
     */
    setValues(startDateTime, endDateTime) {
        if (startDateTime) {
            this.startInput.value = startDateTime;
        }
        if (endDateTime) {
            this.endInput.value = endDateTime;
        }
        this.updateState();
    }

    /**
     * Show error message for input
     */
    showError(input, message) {
        this.clearError(input);
        
        const errorElement = document.createElement('div');
        errorElement.className = 'field-error';
        errorElement.textContent = message;
        errorElement.style.color = 'var(--error-color)';
        errorElement.style.fontSize = 'var(--font-size-sm)';
        errorElement.style.marginTop = 'var(--spacing-1)';
        
        input.parentNode.appendChild(errorElement);
        input.classList.add('error');
    }

    /**
     * Clear error message for input
     */
    clearError(input) {
        const existingError = input.parentNode.querySelector('.field-error');
        if (existingError) {
            existingError.remove();
        }
        input.classList.remove('error');
    }

    /**
     * Reset to default values
     */
    reset() {
        this.setDefaultValues();
        this.clearError(this.startInput);
        this.clearError(this.endInput);
    }

    /**
     * Set quick preset ranges
     */
    setPreset(preset) {
        const now = new Date();
        let startTime, endTime;

        switch (preset) {
            case 'last-hour':
                startTime = new Date(now.getTime() - 60 * 60 * 1000);
                endTime = now;
                break;
            case 'last-2-hours':
                startTime = new Date(now.getTime() - 2 * 60 * 60 * 1000);
                endTime = now;
                break;
            case 'last-4-hours':
                startTime = new Date(now.getTime() - 4 * 60 * 60 * 1000);
                endTime = now;
                break;
            case 'last-8-hours':
                startTime = new Date(now.getTime() - 8 * 60 * 60 * 1000);
                endTime = now;
                break;
            case 'last-24-hours':
                startTime = new Date(now.getTime() - 24 * 60 * 60 * 1000);
                endTime = now;
                break;
            default:
                return;
        }

        this.startInput.value = DateUtils.formatForInput(startTime);
        this.endInput.value = DateUtils.formatForInput(endTime);
        this.updateState();
        this.validateBoth();
    }

    /**
     * Get duration information
     */
    getDurationInfo() {
        const startValue = this.startInput.value;
        const endValue = this.endInput.value;
        
        if (!startValue || !endValue) {
            return null;
        }

        const startDate = DateUtils.parseFromInput(startValue);
        const endDate = DateUtils.parseFromInput(endValue);
        const duration = endDate - startDate;

        return {
            start: startDate,
            end: endDate,
            duration: duration,
            durationFormatted: DateUtils.formatDuration(duration),
            isValid: !isNaN(duration) && duration > 0
        };
    }
}

// Export for ES6 module usage
export default DateTimePicker;
