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
 *       validStateKey,   // state key to publish "valid" boolean
 *       storageKey,      // optional localStorage key (omit = no persistence)
 *       validate,        // optional (value) => errorMessage|null content check
 *       errorId,         // optional DOM id of a .field-error subtext element
 *   })
 *
 * When `validate` is supplied it runs in addition to the length check: the
 * value is only "valid" when it both fits the budget and passes `validate`
 * (which returns an error message string when invalid, or null/'' when ok).
 * The message is shown in the `errorId` element (red subtext), and the input
 * gets the shared `.form-control.error` border for either failure.
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
            validate: null,
            errorId: null,
            ...config,
        };
        this.inputElement = document.getElementById(this.config.inputId);
        this.counterElement = document.getElementById(this.config.counterId);
        this.errorElement = this.config.errorId
            ? document.getElementById(this.config.errorId)
            : null;

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
        const { maxLength, valueStateKey, validStateKey, storageKey, validate } = this.config;
        const overBy = value.length - maxLength;
        const remaining = maxLength - value.length;
        const isOverLimit = overBy > 0;

        // Optional content validation (e.g. reject path separators in a
        // filename). Returns an error message when invalid, else null/''.
        const contentError = typeof validate === 'function' ? (validate(value) || null) : null;
        const isInvalid = isOverLimit || !!contentError;

        this.counterElement.textContent = isOverLimit
            ? `${overBy} char${overBy === 1 ? '' : 's'} over limit`
            : `${remaining} char${remaining === 1 ? '' : 's'} remaining`;
        this.counterElement.classList.toggle('over-limit', isOverLimit);
        this.inputElement.classList.toggle('error', isInvalid);

        // Length status lives in the counter; the subtext element carries
        // the content-validation message (when one is configured).
        if (this.errorElement) {
            this.errorElement.textContent = contentError || '';
            this.errorElement.hidden = !contentError;
        }

        if (valueStateKey) this.state.set(valueStateKey, value);
        if (validStateKey) this.state.set(validStateKey, !isInvalid);

        if (persist && storageKey) {
            window.LocalStorageService.savePreference(storageKey, value);
        }
    }

    /** Trimmed submit value, or null when empty/whitespace. */
    getValue() {
        const trimmed = (this.inputElement?.value || '').trim();
        return trimmed.length ? trimmed : null;
    }

    /** True while the value fits the budget and passes content validation. */
    isValid() {
        const value = this.inputElement?.value || '';
        if (value.length > this.config.maxLength) return false;
        if (typeof this.config.validate === 'function') {
            return !this.config.validate(value);
        }
        return true;
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
