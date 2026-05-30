/**
 * Confirm Modal
 *
 * Promise-based replacement for ``window.confirm`` so destructive actions
 * can use an in-app dialog styled like the rest of the UI instead of the
 * browser's native alert.
 *
 * Usage:
 *
 *     const ok = await confirmModal({
 *         title: 'Delete file?',
 *         message: `Are you sure you want to delete "${name}"?`,
 *         confirmLabel: 'Delete',
 *         danger: true,
 *     });
 *     if (!ok) return;
 *
 * The component reuses a single ``#confirm-modal`` element baked into
 * index.html so we never thrash the DOM. Calls serialize naturally: while
 * a modal is open, any concurrent call resolves ``false`` immediately
 * (chosen over queuing because a stacked destructive confirmation flow
 * is almost never what the user wants).
 *
 * Keyboard:
 *   - Esc cancels
 *   - Enter confirms
 *   - Tab cycles between Cancel and the confirm button
 * Mouse:
 *   - Click on the backdrop cancels
 *   - Click on the buttons does the obvious thing
 *
 * Focus is restored to whatever element had it before the modal opened
 * so the page doesn't jump around after a Cancel.
 */

let isOpen = false;

/**
 * @param {Object} opts
 * @param {string} [opts.title]         Optional bold title at the top of the card.
 * @param {string} opts.message         Required body text.
 * @param {string} [opts.confirmLabel]  Label for the confirm button. Default 'OK'.
 * @param {string} [opts.cancelLabel]   Label for the cancel button. Default 'Cancel'.
 * @param {boolean} [opts.danger]       When true, styles confirm as .btn-danger
 *                                      and focuses Cancel first (safer default
 *                                      for destructive actions). Default false.
 * @returns {Promise<boolean>} Resolves true on confirm, false on cancel.
 */
export function confirmModal({
    title = '',
    message,
    confirmLabel = 'OK',
    cancelLabel = 'Cancel',
    danger = false,
} = {}) {
    if (isOpen) {
        // Avoid stacking. Caller can re-try after the current modal closes.
        return Promise.resolve(false);
    }

    const modal = document.getElementById('confirm-modal');
    const backdrop = modal?.querySelector('.confirm-modal-backdrop');
    const titleEl = modal?.querySelector('.confirm-modal-title');
    const messageEl = modal?.querySelector('.confirm-modal-message');
    const confirmBtn = modal?.querySelector('.confirm-modal-confirm');
    const cancelBtn = modal?.querySelector('.confirm-modal-cancel');

    if (!modal || !backdrop || !titleEl || !messageEl || !confirmBtn || !cancelBtn) {
        // Element missing -- fall back to native confirm so callers don't
        // silently bypass a destructive prompt.
        console.warn('confirmModal: #confirm-modal markup missing, falling back to window.confirm');
        return Promise.resolve(window.confirm(message));
    }

    // Title is optional; collapse the slot when blank so the card stays tight.
    if (title) {
        titleEl.textContent = title;
        titleEl.style.display = '';
    } else {
        titleEl.textContent = '';
        titleEl.style.display = 'none';
    }
    messageEl.textContent = message;
    confirmBtn.textContent = confirmLabel;
    cancelBtn.textContent = cancelLabel;

    // Swap the confirm button between the two visual variants without
    // creating a new element (so listeners attached below stay valid).
    confirmBtn.classList.toggle('btn-danger', !!danger);
    confirmBtn.classList.toggle('btn-primary', !danger);

    const previouslyFocused = document.activeElement;

    return new Promise((resolve) => {
        const cleanup = () => {
            modal.style.display = 'none';
            document.body.classList.remove('confirm-modal-open');
            document.removeEventListener('keydown', onKeyDown, true);
            backdrop.removeEventListener('click', onCancel);
            cancelBtn.removeEventListener('click', onCancel);
            confirmBtn.removeEventListener('click', onConfirm);
            isOpen = false;
            // Hand focus back so screen readers / keyboard users don't lose
            // their place. Guard for elements that may have been removed.
            if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
                try { previouslyFocused.focus(); } catch (_) { /* noop */ }
            }
        };
        const onCancel = () => { cleanup(); resolve(false); };
        const onConfirm = () => { cleanup(); resolve(true); };
        const onKeyDown = (e) => {
            if (e.key === 'Escape') {
                e.stopPropagation();
                onCancel();
            } else if (e.key === 'Enter') {
                e.stopPropagation();
                onConfirm();
            } else if (e.key === 'Tab') {
                // Two-element focus trap: bounce focus between Cancel and Confirm.
                e.preventDefault();
                const next = document.activeElement === confirmBtn ? cancelBtn : confirmBtn;
                next.focus();
            }
        };

        document.addEventListener('keydown', onKeyDown, true);
        backdrop.addEventListener('click', onCancel);
        cancelBtn.addEventListener('click', onCancel);
        confirmBtn.addEventListener('click', onConfirm);

        isOpen = true;
        document.body.classList.add('confirm-modal-open');
        modal.style.display = 'flex';
        // Defer focus to next frame so the browser actually applies it
        // after the display change.
        requestAnimationFrame(() => {
            (danger ? cancelBtn : confirmBtn).focus();
        });
    });
}

export default confirmModal;
