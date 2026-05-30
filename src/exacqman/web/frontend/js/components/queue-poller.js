/**
 * Queue Poller
 *
 * Single always-on client poll loop for the server-side job queue.
 *
 * Lifecycle: ``start()`` from page-load, ``stop()`` is rarely needed
 * (the loop ends with the page). One instance per app -- not per job --
 * so every client polls the same shared endpoint at the same cadence.
 *
 * Each tick fetches ``/api/jobs?since=<lastPollTime>``. The initial
 * ``since`` is the moment the poller started, so the very first poll
 * skips any pre-existing terminal jobs (matching the rule that completed
 * jobs are only visible to clients that observed them transition).
 *
 * Subsequent polls send the ``server_time`` from the previous successful
 * response so a single transition is reported exactly once across two
 * polls even with non-trivial clock drift between client and server.
 *
 * Errors back off exponentially (1s -> 2s -> 5s, capped at 10s) and
 * recover on the first success so transient network blips don't melt the
 * UI.
 */

const NORMAL_INTERVAL_MS = 1000;
const ERROR_BACKOFF_STEPS_MS = [2000, 5000, 10000];

class QueuePoller {
    constructor(apiClient, stateManager) {
        this.api = apiClient;
        this.state = stateManager;
        this._timer = null;
        this._stopped = true;
        this._consecutiveErrors = 0;
    }

    /**
     * Begin polling. Idempotent. Seeds ``since`` to "now" so a fresh page
     * load never sees stale terminal jobs from before the user arrived.
     */
    start() {
        if (!this._stopped) return;
        this._stopped = false;
        this._consecutiveErrors = 0;
        this.state.set('lastPollTime', new Date().toISOString());
        this._scheduleNext(0);
    }

    /** Stop the loop. Safe to call multiple times. */
    stop() {
        this._stopped = true;
        if (this._timer) {
            clearTimeout(this._timer);
            this._timer = null;
        }
    }

    _scheduleNext(delayMs) {
        if (this._stopped) return;
        this._timer = setTimeout(() => this._tick(), delayMs);
    }

    async _tick() {
        if (this._stopped) return;
        const since = this.state.get('lastPollTime');
        try {
            const snapshot = await this.api.getJobsSnapshot(since);
            this.state.updateFromSnapshot(snapshot);
            this._consecutiveErrors = 0;
            this._scheduleNext(NORMAL_INTERVAL_MS);
        } catch (err) {
            // Don't bubble: the poller is fire-and-forget. Just log and
            // back off so we don't hammer a dying server.
            console.warn('Queue poll failed:', err);
            const step = Math.min(
                this._consecutiveErrors,
                ERROR_BACKOFF_STEPS_MS.length - 1,
            );
            this._consecutiveErrors += 1;
            this._scheduleNext(ERROR_BACKOFF_STEPS_MS[step]);
        }
    }
}

export default QueuePoller;
