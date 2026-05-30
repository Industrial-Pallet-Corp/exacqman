/**
 * Date and Time Utilities
 * 
 * Helper functions for date/time manipulation and formatting.
 */

class DateUtils {
    /**
     * Format date for datetime-local input
     * @param {Date} date - Date object
     * @returns {string} Formatted date string
     */
    static formatForInput(date) {
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        const hours = String(date.getHours()).padStart(2, '0');
        const minutes = String(date.getMinutes()).padStart(2, '0');
        
        return `${year}-${month}-${day}T${hours}:${minutes}`;
    }

    /**
     * Parse datetime-local input value
     * @param {string} value - Input value
     * @returns {Date} Date object
     */
    static parseFromInput(value) {
        return new Date(value);
    }

    /**
     * Get current date/time formatted for input
     * @returns {string} Current date/time
     */
    static now() {
        return this.formatForInput(new Date());
    }

    /**
     * Get date/time from specified hours ago
     * @param {number} hours - Hours ago
     * @returns {string} Date/time string
     */
    static hoursAgo(hours) {
        const date = new Date();
        date.setHours(date.getHours() - hours);
        return this.formatForInput(date);
    }

    /**
     * Validate date range
     * @param {string} startValue - Start date string
     * @param {string} endValue - End date string
     * @returns {Object} Validation result
     */
    static validateRange(startValue, endValue) {
        const start = this.parseFromInput(startValue);
        const end = this.parseFromInput(endValue);
        
        if (isNaN(start.getTime()) || isNaN(end.getTime())) {
            return { valid: false, message: 'Invalid date format' };
        }
        
        if (end <= start) {
            return { valid: false, message: 'End time must be after start time' };
        }
        
        const duration = end - start;
        const maxDuration = 4 * 60 * 60 * 1000; // 4 hours in milliseconds
        
        if (duration > maxDuration) {
            return { valid: false, message: 'Duration cannot exceed 4 hours' };
        }
        
        return { valid: true };
    }

    /**
     * Format duration in human-readable format
     * @param {number} milliseconds - Duration in milliseconds
     * @returns {string} Formatted duration
     */
    static formatDuration(milliseconds) {
        const seconds = Math.floor(milliseconds / 1000);
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);
        
        if (hours > 0) {
            return `${hours}h ${minutes % 60}m`;
        } else if (minutes > 0) {
            return `${minutes}m ${seconds % 60}s`;
        } else {
            return `${seconds}s`;
        }
    }

    /**
     * Get relative time string
     * @param {Date|string} date - Date object or ISO string
     * @returns {string} Relative time
     */
    static getRelativeTime(date) {
        const now = new Date();
        const target = new Date(date);
        const diff = now - target;
        
        const seconds = Math.floor(diff / 1000);
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);
        const days = Math.floor(hours / 24);
        
        if (days > 0) {
            return `${days} day${days > 1 ? 's' : ''} ago`;
        } else if (hours > 0) {
            return `${hours} hour${hours > 1 ? 's' : ''} ago`;
        } else if (minutes > 0) {
            return `${minutes} minute${minutes > 1 ? 's' : ''} ago`;
        } else {
            return 'Just now';
        }
    }

    /**
     * Check if date is today
     * @param {Date|string} date - Date to check
     * @returns {boolean} True if today
     */
    static isToday(date) {
        const target = new Date(date);
        const today = new Date();
        
        return target.getDate() === today.getDate() &&
               target.getMonth() === today.getMonth() &&
               target.getFullYear() === today.getFullYear();
    }

    /**
     * Check if date is yesterday
     * @param {Date|string} date - Date to check
     * @returns {boolean} True if yesterday
     */
    static isYesterday(date) {
        const target = new Date(date);
        const yesterday = new Date();
        yesterday.setDate(yesterday.getDate() - 1);
        
        return target.getDate() === yesterday.getDate() &&
               target.getMonth() === yesterday.getMonth() &&
               target.getFullYear() === yesterday.getFullYear();
    }

    /**
     * Format date for display
     * @param {Date|string} date - Date to format
     * @param {Object} options - Formatting options
     * @returns {string} Formatted date
     */
    static formatDisplay(date, options = {}) {
        const target = new Date(date);
        const now = new Date();
        
        const defaultOptions = {
            showTime: true,
            showRelative: true,
            ...options
        };
        
        if (defaultOptions.showRelative) {
            if (this.isToday(target)) {
                return `Today ${defaultOptions.showTime ? target.toLocaleTimeString() : ''}`;
            } else if (this.isYesterday(target)) {
                return `Yesterday ${defaultOptions.showTime ? target.toLocaleTimeString() : ''}`;
            }
        }
        
        return target.toLocaleString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: defaultOptions.showTime ? '2-digit' : undefined,
            minute: defaultOptions.showTime ? '2-digit' : undefined
        });
    }
}

// Export for ES6 module usage
export default DateUtils;
