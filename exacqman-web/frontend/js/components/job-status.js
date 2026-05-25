/**
 * Job Status Component
 *
 * Renders the Job Status panel from the session-scoped list of jobs this
 * client has observed since page load. Polling is owned by ``QueuePoller``;
 * this component is a pure renderer that re-runs whenever ``sessionJobs``
 * changes.
 *
 * Job ordering (top to bottom): the currently-running job, then waiting
 * jobs in queue order, then terminal (completed / failed) jobs newest
 * first. Terminal jobs persist for the lifetime of this page load --
 * a refresh wipes them.
 */

class JobStatus {
    constructor(apiClient, stateManager) {
        this.api = apiClient;
        this.state = stateManager;
        this.jobListElement = document.getElementById('job-list');

        this.init();
    }

    init() {
        if (!this.jobListElement) {
            console.warn('Job list element not found');
            return;
        }
        this.setupStateListeners();
        this.updateDisplay();
    }

    setupStateListeners() {
        // sessionJobs covers every update -- the snapshot poll always
        // upserts at least one job (or no-op) on each tick.
        this.state.subscribe('sessionJobs', () => this.updateDisplay());
        // queue also changes on every snapshot; subscribing here too
        // means a queue-only mutation (e.g. position shifts) still
        // triggers a re-render.
        this.state.subscribe('queue', () => this.updateDisplay());
    }

    updateDisplay() {
        if (!this.jobListElement) return;

        const jobs = this.state.getSessionJobsForDisplay();

        if (jobs.length === 0) {
            this.jobListElement.innerHTML = '<div class="no-jobs">No active jobs</div>';
            return;
        }

        this.jobListElement.innerHTML = jobs.map(job => this.createJobElement(job)).join('');
    }

    createJobElement(job) {
        const status = job.status || 'unknown';
        const statusClass = this.getStatusClass(status);
        const progressBar = this.createProgressBar(job);
        const actions = this.createJobActions(job);
        const created = this.formatDate(job.created_at);
        const completed = job.completed_at ? this.formatDate(job.completed_at) : null;

        return `
            <div class="job-item ${statusClass}" data-job-id="${job.id}">
                <div class="job-header">
                    <div class="job-info">
                        <span class="job-created">${created}</span>
                    </div>
                    <div class="job-status">
                        <span class="job-status-badge ${statusClass}">${this.formatStatus(job)}</span>
                        ${actions}
                    </div>
                </div>

                ${progressBar}

                <div class="job-details">
                    <div class="job-message">${this.escape(job.message || '')}</div>
                    ${this.createJobMetadata(job)}
                </div>

                ${completed ? `<div class="job-completed">Completed: ${completed}</div>` : ''}
            </div>
        `;
    }

    /**
     * Progress bar is only shown while a job is actively running. Queued
     * jobs show their position instead of a 0% bar (less alarming UX),
     * and terminal jobs show nothing.
     */
    createProgressBar(job) {
        if (job.status === 'processing') {
            const progress = Math.max(0, Math.min(100, job.progress || 0));
            return `
                <div class="job-progress">
                    <div class="job-progress-bar" style="width: ${progress}%"></div>
                </div>
            `;
        }
        return '';
    }

    createJobActions(job) {
        if (job.status === 'completed' && job.result?.filename) {
            // window.app is wired in app.js; this matches the existing pattern.
            return `<div class="job-actions">
                <button class="btn btn-sm btn-primary" onclick="app.handleFileDownload('${this.escapeAttr(job.result.filename)}')">
                    Download
                </button>
            </div>`;
        }
        // Failed jobs show their friendly summary in the message; the
        // raw technical detail lives in the per-job log snippet which
        // we expose as a plain anchor with the `download` attribute so
        // the browser saves it without any JS roundtrip.
        if (job.status === 'failed' && job.log_available && this.api?.getJobLogURL) {
            const url = this.escapeAttr(this.api.getJobLogURL(job.id));
            return `<div class="job-actions">
                <a class="btn btn-sm btn-secondary" download href="${url}">
                    Download log
                </a>
            </div>`;
        }
        return '';
    }

    createJobMetadata(job) {
        const meta = [];
        const req = job.request || {};

        if (req.camera_alias) {
            meta.push(`Camera: ${this.escape(req.camera_alias)}`);
        }
        if (req.timelapse_multiplier) {
            meta.push(`Speed: ${req.timelapse_multiplier}x`);
        }
        if (req.start_datetime && req.end_datetime) {
            const duration = new Date(req.end_datetime) - new Date(req.start_datetime);
            meta.push(`Duration: ${this.formatDuration(duration)}`);
        }
        if (job.result?.filename) {
            meta.push(`File: ${this.escape(job.result.filename)}`);
        }
        // The raw error is intentionally not surfaced here -- the friendly
        // status message and the downloadable log together replace it.
        return meta.length > 0 ? `<div class="job-metadata">${meta.join(' \u2022 ')}</div>` : '';
    }

    getStatusClass(status) {
        switch (status) {
            case 'queued': return 'queued';
            case 'processing': return 'processing';
            case 'completed': return 'completed';
            case 'failed': return 'failed';
            default: return 'unknown';
        }
    }

    /**
     * Status badge text. Queued jobs include their queue position so users
     * can see "you're #2 of 3" at a glance.
     */
    formatStatus(job) {
        switch (job.status) {
            case 'queued':
                return job.queue_position ? `Queued (#${job.queue_position})` : 'Queued';
            case 'processing': return 'Processing';
            case 'completed': return 'Completed';
            case 'failed': return 'Failed';
            default: return 'Unknown';
        }
    }

    formatDate(dateString) {
        if (!dateString) return 'Unknown';
        const date = new Date(dateString);
        const diff = Date.now() - date.getTime();
        if (diff < 60 * 1000) return 'Just now';
        if (diff < 60 * 60 * 1000) return `${Math.floor(diff / 60000)}m ago`;
        if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / 3600000)}h ago`;
        return date.toLocaleString('en-US', {
            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
        });
    }

    formatDuration(milliseconds) {
        const seconds = Math.floor(milliseconds / 1000);
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);
        if (hours > 0) return `${hours}h ${minutes % 60}m`;
        if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
        return `${seconds}s`;
    }

    /** Basic textContent-safe escape for embedding strings in innerHTML. */
    escape(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    /** Stricter escape for use inside HTML attribute values (single-quoted). */
    escapeAttr(value) {
        return this.escape(value).replace(/'/g, '&#39;').replace(/"/g, '&quot;');
    }
}

export default JobStatus;
