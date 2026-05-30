/**
 * Local Storage Service for ExacqMan Web Preferences
 * Handles saving and loading user preferences using browser localStorage
 */

class LocalStorageService {
    constructor() {
        this.storageKey = 'exacqman_preferences';
        this.defaultPreferences = {
            configFile: null,
            server: null,
            camera: null,
            timelapseMultiplier: 50, // Default to 50x
            caption: '',
            lastUpdated: null
        };
    }

    /**
     * Save all preferences to localStorage
     * @param {Object} preferences - Preferences object to save
     */
    savePreferences(preferences) {
        try {
            const preferencesToSave = {
                ...preferences,
                lastUpdated: new Date().toISOString()
            };
            localStorage.setItem(this.storageKey, JSON.stringify(preferencesToSave));
            console.log('Preferences saved:', preferencesToSave);
        } catch (error) {
            console.error('Failed to save preferences:', error);
        }
    }

    /**
     * Load all preferences from localStorage
     * @returns {Object} Preferences object with defaults for missing values
     */
    loadPreferences() {
        try {
            const stored = localStorage.getItem(this.storageKey);
            if (!stored) {
                return { ...this.defaultPreferences };
            }
            
            const preferences = JSON.parse(stored);
            return {
                ...this.defaultPreferences,
                ...preferences
            };
        } catch (error) {
            console.error('Failed to load preferences:', error);
            return { ...this.defaultPreferences };
        }
    }

    /**
     * Save a single preference
     * @param {string} key - Preference key
     * @param {*} value - Preference value
     */
    savePreference(key, value) {
        try {
            const currentPreferences = this.loadPreferences();
            currentPreferences[key] = value;
            this.savePreferences(currentPreferences);
        } catch (error) {
            console.error(`Failed to save preference ${key}:`, error);
        }
    }

    /**
     * Load a single preference
     * @param {string} key - Preference key
     * @param {*} defaultValue - Default value if not found
     * @returns {*} Preference value or default
     */
    loadPreference(key, defaultValue = null) {
        try {
            const preferences = this.loadPreferences();
            return preferences[key] !== undefined ? preferences[key] : defaultValue;
        } catch (error) {
            console.error(`Failed to load preference ${key}:`, error);
            return defaultValue;
        }
    }

    /**
     * Clear all preferences
     */
    clearPreferences() {
        try {
            localStorage.removeItem(this.storageKey);
            console.log('All preferences cleared');
        } catch (error) {
            console.error('Failed to clear preferences:', error);
        }
    }

    /**
     * Check if preferences exist
     * @returns {boolean} True if preferences exist
     */
    hasPreferences() {
        try {
            const stored = localStorage.getItem(this.storageKey);
            return stored !== null;
        } catch (error) {
            console.error('Failed to check preferences:', error);
            return false;
        }
    }

    /**
     * Get preferences age in hours
     * @returns {number} Hours since last update, or null if no preferences
     */
    getPreferencesAge() {
        try {
            const preferences = this.loadPreferences();
            if (!preferences.lastUpdated) {
                return null;
            }
            
            const lastUpdated = new Date(preferences.lastUpdated);
            const now = new Date();
            const diffMs = now - lastUpdated;
            return diffMs / (1000 * 60 * 60); // Convert to hours
        } catch (error) {
            console.error('Failed to get preferences age:', error);
            return null;
        }
    }

    /**
     * Validate preferences structure
     * @param {Object} preferences - Preferences to validate
     * @returns {boolean} True if valid
     */
    validatePreferences(preferences) {
        if (!preferences || typeof preferences !== 'object') {
            return false;
        }

        // Check for required structure (but allow null values)
        const requiredKeys = ['configFile', 'server', 'camera', 'timelapseMultiplier'];
        return requiredKeys.every(key => preferences.hasOwnProperty(key));
    }
}

// Create singleton instance
const localStorageService = new LocalStorageService();

// Export for use in other modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = localStorageService;
} else {
    window.LocalStorageService = localStorageService;
    window.localStorageService = localStorageService; // Also expose as instance
}
