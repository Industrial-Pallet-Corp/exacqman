/**
 * Caption Input Component
 *
 * Optional overlay text rendered below the timestamp by the CLI. The 25-char
 * limit is enforced on the backend (Pydantic max_length=25) and on the CLI;
 * this component mirrors that constraint with inline feedback:
 *
 *   - A live counter ("N chars remaining" / "N chars over") sits beside the
 *     input and turns red while over the limit.
 *   - The input itself gets the shared `.form-control.error` red-border
 *     styling while over the limit.
 *   - The value is persisted to localStorage so the user's last caption is
 *     restored on reload and across extractions (matching MultiplierSelector
 *     reset behavior).
 */

const CAPTION_MAX_LENGTH = 25;
const STORAGE_KEY = 'caption';

class CaptionInput {
    constructor(stateManager) {
        this.state = stateManager;
        this.inputElement = document.getElementById('caption-input');
        this.counterElement = document.getElementById('caption-counter');

        this.init();
    }

    init() {
        if (!this.inputElement || !this.counterElement) {
            console.warn('Caption input elements not found');
            return;
        }

        this.inputElement.addEventListener('input', () => this.handleChange());

        const saved = window.LocalStorageService.loadPreference(STORAGE_KEY, '') || '';
        this.inputElement.value = saved;
        this.handleChange({ persist: false });
    }

    handleChange({ persist = true } = {}) {
        const value = this.inputElement.value;
        const overBy = value.length - CAPTION_MAX_LENGTH;
        const remaining = CAPTION_MAX_LENGTH - value.length;
        const isOverLimit = overBy > 0;

        this.counterElement.textContent = isOverLimit
            ? `${overBy} char${overBy === 1 ? '' : 's'} over limit`
            : `${remaining} char${remaining === 1 ? '' : 's'} remaining`;
        this.counterElement.classList.toggle('over-limit', isOverLimit);
        this.inputElement.classList.toggle('error', isOverLimit);

        this.state.set('selectedCaption', value);
        this.state.set('captionValid', !isOverLimit);

        if (persist) {
            window.LocalStorageService.savePreference(STORAGE_KEY, value);
        }
    }

    /**
     * Returns the caption to submit (trimmed) or null when empty/whitespace.
     */
    getValue() {
        const trimmed = (this.inputElement?.value || '').trim();
        return trimmed.length ? trimmed : null;
    }

    /**
     * Submit-time validation. Empty is valid (caption is optional); too long
     * is the only failure mode.
     */
    isValid() {
        const length = (this.inputElement?.value || '').length;
        return length <= CAPTION_MAX_LENGTH;
    }

    /**
     * Reload the persisted caption (preserves the user's last value across
     * extractions, mirroring how MultiplierSelector.reset() works).
     */
    reset() {
        if (!this.inputElement) return;
        const saved = window.LocalStorageService.loadPreference(STORAGE_KEY, '') || '';
        this.inputElement.value = saved;
        this.handleChange({ persist: false });
    }
}

export default CaptionInput;
