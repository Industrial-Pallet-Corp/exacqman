/**
 * Validation Utilities
 * 
 * Helper functions for form validation and data validation.
 */

class ValidationUtils {
    /**
     * Validate extraction form data
     * @param {Object} data - Form data
     * @returns {Object} Validation result
     */
    static validateExtractionData(data) {
        const errors = [];

        // Required fields
        if (!data.camera_alias) {
            errors.push('Camera selection is required');
        }

        if (!data.start_datetime) {
            errors.push('Start date and time is required');
        }

        if (!data.end_datetime) {
            errors.push('End date and time is required');
        }

        if (!data.config_file) {
            errors.push('Configuration file is required');
        }

        // Date validation
        if (data.start_datetime && data.end_datetime) {
            const startDate = new Date(data.start_datetime);
            const endDate = new Date(data.end_datetime);

            if (isNaN(startDate.getTime())) {
                errors.push('Invalid start date format');
            }

            if (isNaN(endDate.getTime())) {
                errors.push('Invalid end date format');
            }

            if (!isNaN(startDate.getTime()) && !isNaN(endDate.getTime())) {
                if (endDate <= startDate) {
                    errors.push('End time must be after start time');
                }

                // Check duration (max 4 hours)
                const duration = endDate - startDate;
                const maxDuration = 4 * 60 * 60 * 1000; // 4 hours

                if (duration > maxDuration) {
                    errors.push('Duration must not exceed 4 hours');
                }

                // Check if dates are in the future
                const now = new Date();
                if (startDate > now) {
                    errors.push('Start time cannot be in the future');
                }
            }
        }

        // Timelapse multiplier validation
        if (data.timelapse_multiplier !== undefined) {
            const multiplier = parseInt(data.timelapse_multiplier);
            if (isNaN(multiplier) || multiplier < 1 || multiplier > 50) {
                errors.push('Timelapse multiplier must be between 1 and 50');
            }
        }

        return {
            valid: errors.length === 0,
            errors: errors
        };
    }

    /**
     * Validate camera alias
     * @param {string} alias - Camera alias
     * @returns {boolean} True if valid
     */
    static validateCameraAlias(alias) {
        if (!alias || typeof alias !== 'string') {
            return false;
        }

        // Basic validation: alphanumeric, underscore, hyphen
        const pattern = /^[a-zA-Z0-9_-]+$/;
        return pattern.test(alias) && alias.length <= 50;
    }

    /**
     * Validate configuration file path
     * @param {string} path - Config file path
     * @returns {boolean} True if valid
     */
    static validateConfigPath(path) {
        if (!path || typeof path !== 'string') {
            return false;
        }

        // Basic validation: should end with .config
        return path.endsWith('.config') && path.length <= 100;
    }

    /**
     * Validate server name
     * @param {string} server - Server name
     * @returns {boolean} True if valid
     */
    static validateServerName(server) {
        if (!server || typeof server !== 'string') {
            return false;
        }

        // Basic validation: alphanumeric, underscore, hyphen
        const pattern = /^[a-zA-Z0-9_-]+$/;
        return pattern.test(server) && server.length <= 50;
    }

    /**
     * Sanitize filename
     * @param {string} filename - Original filename
     * @returns {string} Sanitized filename
     */
    static sanitizeFilename(filename) {
        if (!filename || typeof filename !== 'string') {
            return '';
        }

        // Remove or replace unsafe characters
        return filename
            .replace(/[^a-zA-Z0-9._-]/g, '_')
            .replace(/_{2,}/g, '_')
            .replace(/^_|_$/g, '');
    }

    /**
     * Validate file extension
     * @param {string} filename - Filename to validate
     * @param {Array} allowedExtensions - Allowed extensions
     * @returns {boolean} True if valid
     */
    static validateFileExtension(filename, allowedExtensions = ['.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv']) {
        if (!filename || typeof filename !== 'string') {
            return false;
        }

        const extension = filename.toLowerCase().substring(filename.lastIndexOf('.'));
        return allowedExtensions.includes(extension);
    }

    /**
     * Validate job ID format
     * @param {string} jobId - Job ID to validate
     * @returns {boolean} True if valid
     */
    static validateJobId(jobId) {
        if (!jobId || typeof jobId !== 'string') {
            return false;
        }

        // UUID v4 format validation
        const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
        return uuidPattern.test(jobId);
    }

    /**
     * Validate datetime string
     * @param {string} datetime - Datetime string
     * @returns {boolean} True if valid
     */
    static validateDateTime(datetime) {
        if (!datetime || typeof datetime !== 'string') {
            return false;
        }

        const date = new Date(datetime);
        return !isNaN(date.getTime());
    }

    /**
     * Get validation error message
     * @param {string} field - Field name
     * @param {string} error - Error type
     * @returns {string} Error message
     */
    static getErrorMessage(field, error) {
        const messages = {
            required: `${field} is required`,
            invalid: `Invalid ${field} format`,
            tooLong: `${field} is too long`,
            tooShort: `${field} is too short`,
            outOfRange: `${field} is out of valid range`,
            future: `${field} cannot be in the future`,
            past: `${field} cannot be in the past`
        };

        return messages[error] || `Invalid ${field}`;
    }

    /**
     * Validate form field in real-time
     * @param {HTMLElement} field - Form field element
     * @param {string} type - Validation type
     * @returns {Object} Validation result
     */
    static validateField(field, type) {
        const value = field.value.trim();
        let valid = true;
        let message = '';

        switch (type) {
            case 'required':
                valid = value.length > 0;
                message = valid ? '' : 'This field is required';
                break;

            case 'datetime':
                valid = this.validateDateTime(value);
                message = valid ? '' : 'Invalid date/time format';
                break;

            case 'camera':
                valid = this.validateCameraAlias(value);
                message = valid ? '' : 'Invalid camera alias';
                break;

            case 'config':
                valid = this.validateConfigPath(value);
                message = valid ? '' : 'Invalid configuration file path';
                break;

            case 'server':
                valid = this.validateServerName(value);
                message = valid ? '' : 'Invalid server name';
                break;

            case 'multiplier':
                const multiplier = parseInt(value);
                valid = !isNaN(multiplier) && multiplier >= 2 && multiplier <= 50;
                message = valid ? '' : 'Must be between 2 and 50';
                break;

            default:
                valid = true;
                message = '';
        }

        return { valid, message };
    }

    /**
     * Show field validation error
     * @param {HTMLElement} field - Form field
     * @param {string} message - Error message
     */
    static showFieldError(field, message) {
        // Remove existing error styling
        field.classList.remove('error');
        
        // Remove existing error message
        const existingError = field.parentNode.querySelector('.field-error');
        if (existingError) {
            existingError.remove();
        }

        if (message) {
            // Add error styling
            field.classList.add('error');
            
            // Add error message
            const errorElement = document.createElement('div');
            errorElement.className = 'field-error';
            errorElement.textContent = message;
            errorElement.style.color = 'var(--error-color)';
            errorElement.style.fontSize = 'var(--font-size-sm)';
            errorElement.style.marginTop = 'var(--spacing-1)';
            
            field.parentNode.appendChild(errorElement);
        }
    }

    /**
     * Clear field validation error
     * @param {HTMLElement} field - Form field
     */
    static clearFieldError(field) {
        field.classList.remove('error');
        
        const existingError = field.parentNode.querySelector('.field-error');
        if (existingError) {
            existingError.remove();
        }
    }
}

// Export for ES6 module usage
export default ValidationUtils;
