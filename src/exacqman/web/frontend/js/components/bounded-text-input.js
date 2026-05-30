/**
 * Bounded Text Input Component
 *
 * Reusable single-line text input with a live character-budget counter and
 * optional localStorage persistence. Used for the Caption and Filename
 * fields, both of which share the same UX:
 *
 *   - Counter sits beside the input ("N chars remaining" / "N chars over
 *     limit"). Turns red while over the limit.
 *   - The input gets the shared `.form-control.error` red-border styling
 *     while over the limit.
 *   - The submit-time max is enforced server-side too (Pydantic + CLI).
 *
 * Construction:
 *
 *   new BoundedTextInput(stateManager, {
 *       inputId,         // DOM id of the <input>
 *       counterId,       // DOM id of the counter <span>
 *       maxLength,       // character budget
 *       valueStateKey,   // state key to publish the current trimmed value
 *       validStateKey,   // state key to publish "<= maxLength" boolean
 *       storageKey,      // optional localStorage key (omit = no persistence)
 *   })
 */

class BoundedTextInput {
    constructor(stateManager, config) {
        this.state = stateManager;
        this.config = {
            inputId: null,
            counterId: null,
            maxLength: 30,
            valueStateKey: null,
            validStateKey: null,
            storageKey: null,
            ...config,
        };
        this.inputElement = document.getElementById(this.config.inputId);
        this.counterElement = document.getElementById(this.config.counterId);

        this.init();
    }

    init() {
        if (!this.inputElement || !this.counterElement) {
            console.warn(`BoundedTextInput: elements not found for ${this.config.inputId}`);
            return;
        }

        this.inputElement.addEventListener('input', () => this.handleChange());

        if (this.config.storageKey) {
            const saved = window.LocalStorageService.loadPreference(this.config.storageKey, '') || '';
            this.inputElement.value = saved;
        }
        this.handleChange({ persist: false });
    }

    handleChange({ persist = true } = {}) {
        const value = this.inputElement.value;
        const { maxLength, valueStateKey, validStateKey, storageKey } = this.config;
        const overBy = value.length - maxLength;
        const remaining = maxLength - value.length;
        const isOverLimit = overBy > 0;

        this.counterElement.textContent = isOverLimit
            ? `${overBy} char${overBy === 1 ? '' : 's'} over limit`
            : `${remaining} char${remaining === 1 ? '' : 's'} remaining`;
        this.counterElement.classList.toggle('over-limit', isOverLimit);
        this.inputElement.classList.toggle('error', isOverLimit);

        if (valueStateKey) this.state.set(valueStateKey, value);
        if (validStateKey) this.state.set(validStateKey, !isOverLimit);

        if (persist && storageKey) {
            window.LocalStorageService.savePreference(storageKey, value);
        }
    }

    /** Trimmed submit value, or null when empty/whitespace. */
    getValue() {
        const trimmed = (this.inputElement?.value || '').trim();
        return trimmed.length ? trimmed : null;
    }

    /** True while the current value fits the character budget. */
    isValid() {
        const length = (this.inputElement?.value || '').length;
        return length <= this.config.maxLength;
    }

    /**
     * Reload the persisted value (if any). For non-persisted inputs this
     * clears the field, which is desirable for per-run values like filename.
     */
    reset() {
        if (!this.inputElement) return;
        const saved = this.config.storageKey
            ? (window.LocalStorageService.loadPreference(this.config.storageKey, '') || '')
            : '';
        this.inputElement.value = saved;
        this.handleChange({ persist: false });
    }
}

export default BoundedTextInput;
