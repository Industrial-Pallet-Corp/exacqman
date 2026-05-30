/**
 * File Browser Component
 * 
 * Handles file display, sorting, filtering, and bulk operations for processed videos.
 */

import { confirmModal } from '../utils/confirm-modal.js';

class FileBrowser {
    constructor(apiClient, stateManager) {
        this.api = apiClient;
        this.state = stateManager;
        this.filesListElement = document.getElementById('files-list');
        this.refreshButton = document.getElementById('refresh-files');
        
        // Filter elements
        this.dateFromInput = null;
        this.dateToInput = null;
        this.cameraFilterSelect = null;
        this.filenameSearchInput = null;
        this.clearFiltersButton = null;
        
        // State
        this.files = [];
        this.filteredFiles = [];
        this.selectedFiles = new Set();
        this.isMobile = this.state.isMobile();
        
        this.init();
    }

    /**
     * Initialize the file browser
     */
    init() {
        if (!this.filesListElement) {
            console.warn('File browser element not found');
            return;
        }

        this.createFilterControls();
        this.setupEventListeners();
        this.setupStateListeners();
        
        // Force initial display with headers
        this.updateDisplay();
        
        this.loadFiles();
    }

    /**
     * Create filter controls
     */
    createFilterControls() {
        const filesPanel = document.getElementById('files-panel');
        if (!filesPanel) return;

        // Find the files header
        const filesHeader = filesPanel.querySelector('.files-header');
        if (!filesHeader) return;

        // Create filter container
        const filterContainer = document.createElement('div');
        filterContainer.className = 'file-filters';
        filterContainer.innerHTML = `
            <div class="filter-row">
                <div class="filter-group">
                    <label for="date-from">From Date:</label>
                    <input type="date" id="date-from" class="form-control filter-input">
                </div>
                <div class="filter-group">
                    <label for="date-to">To Date:</label>
                    <input type="date" id="date-to" class="form-control filter-input">
                </div>
                <div class="filter-group">
                    <label for="camera-filter">Camera:</label>
                    <select id="camera-filter" class="form-control filter-input">
                        <option value="">All Cameras</option>
                    </select>
                </div>
                <div class="filter-group">
                    <label for="filename-search">Search:</label>
                    <input type="text" id="filename-search" class="form-control filter-input" placeholder="Search filenames...">
                </div>
                <div class="filter-group">
                    <button id="clear-filters" class="btn btn-secondary">Clear Filters</button>
                </div>
            </div>
        `;

        // Insert after files header
        filesHeader.insertAdjacentElement('afterend', filterContainer);

        // Store references to filter elements
        this.dateFromInput = document.getElementById('date-from');
        this.dateToInput = document.getElementById('date-to');
        this.cameraFilterSelect = document.getElementById('camera-filter');
        this.filenameSearchInput = document.getElementById('filename-search');
        this.clearFiltersButton = document.getElementById('clear-filters');
    }

    /**
     * Set up event listeners
     */
    setupEventListeners() {
        // Refresh button
        if (this.refreshButton) {
            this.refreshButton.addEventListener('click', () => {
                this.loadFiles();
            });
        }

        // Filter inputs
        if (this.dateFromInput) {
            this.dateFromInput.addEventListener('change', () => this.applyFilters());
        }
        if (this.dateToInput) {
            this.dateToInput.addEventListener('change', () => this.applyFilters());
        }
        if (this.cameraFilterSelect) {
            this.cameraFilterSelect.addEventListener('change', () => this.applyFilters());
        }
        if (this.filenameSearchInput) {
            this.filenameSearchInput.addEventListener('input', () => this.applyFilters());
        }
        if (this.clearFiltersButton) {
            this.clearFiltersButton.addEventListener('click', () => this.clearFilters());
        }
    }

    /**
     * Set up state listeners
     */
    setupStateListeners() {
        // Listen for processed videos updates
        this.state.subscribe('processedVideos', (videos) => {
            this.files = videos || [];
            this.applyFilters();
            this.updateCameraFilter();
        });

        // Auto-refresh the file list when a new job lands in a
        // terminal "completed" state. We track which job ids we've
        // already counted so back-to-back polls of the same terminal
        // entry (within the server's TTL window) don't re-fire.
        this._refreshedTerminalIds = new Set();
        this.state.subscribe('sessionJobs', (jobs) => {
            let sawNewCompletion = false;
            jobs.forEach((job, id) => {
                if (job.status === 'completed' && !this._refreshedTerminalIds.has(id)) {
                    this._refreshedTerminalIds.add(id);
                    sawNewCompletion = true;
                }
            });
            if (sawNewCompletion) {
                // Small delay so the file move/cleanup has settled before
                // we re-list the exports directory.
                setTimeout(() => this.loadFiles(), 2000);
            }
        });
    }

    /**
     * Load files from API
     */
    async loadFiles() {
        try {
            this.state.setLoading(true);
            const files = await this.api.getProcessedVideos();
            this.state.updateProcessedVideos(files);
        } catch (error) {
            console.error('Failed to load files:', error);
            this.showError('Failed to load video files');
        } finally {
            this.state.setLoading(false);
        }
    }

    /**
     * Apply filters to files
     */
    applyFilters() {
        
        let filtered = [...this.files];

        // Date range filter
        const fromDate = this.dateFromInput?.value;
        const toDate = this.dateToInput?.value;
        
        if (fromDate) {
            const from = new Date(fromDate);
            filtered = filtered.filter(file => new Date(file.created_at) >= from);
        }
        
        if (toDate) {
            const to = new Date(toDate);
            to.setHours(23, 59, 59, 999); // End of day
            filtered = filtered.filter(file => new Date(file.created_at) <= to);
        }

        // Camera filter
        const selectedCamera = this.cameraFilterSelect?.value;
        if (selectedCamera) {
            filtered = filtered.filter(file => file.camera_alias === selectedCamera);
        }

        // Filename search
        const searchTerm = this.filenameSearchInput?.value.toLowerCase();
        if (searchTerm) {
            filtered = filtered.filter(file => 
                file.filename.toLowerCase().includes(searchTerm)
            );
        }

        this.filteredFiles = filtered;
        
        // Clean up selectedFiles to only include files that are currently visible
        const visibleFilenames = new Set(filtered.map(file => file.filename));
        const newSelectedFiles = new Set();
        for (const filename of this.selectedFiles) {
            if (visibleFilenames.has(filename)) {
                newSelectedFiles.add(filename);
            }
        }
        this.selectedFiles = newSelectedFiles;
        
        this.updateDisplay();
    }

    /**
     * Clear all filters
     */
    clearFilters() {
        if (this.dateFromInput) this.dateFromInput.value = '';
        if (this.dateToInput) this.dateToInput.value = '';
        if (this.cameraFilterSelect) this.cameraFilterSelect.value = '';
        if (this.filenameSearchInput) this.filenameSearchInput.value = '';
        
        this.applyFilters();
    }

    /**
     * Update camera filter options
     */
    updateCameraFilter() {
        
        if (!this.cameraFilterSelect) return;

        // Get unique cameras from files
        const cameras = [...new Set(this.files.map(file => file.camera_alias).filter(Boolean))];
        
        // Clear existing options except "All Cameras"
        this.cameraFilterSelect.innerHTML = '<option value="">All Cameras</option>';
        
        // Add camera options
        cameras.forEach(camera => {
            const option = document.createElement('option');
            option.value = camera;
            option.textContent = camera;
            this.cameraFilterSelect.appendChild(option);
        });
        
    }


    /**
     * Update file display
     */
    updateDisplay() {
        if (!this.filesListElement) return;

        // Create mobile-friendly file list
        const listHTML = `
            <div class="file-list">
                <div class="file-list-header">
                    <div class="file-list-header-content">
                        <div class="file-selection-controls">
                            <input type="checkbox" id="select-all" class="file-checkbox-input">
                            <span id="selection-count">Select All (0 files selected)</span>
                        </div>
                    </div>
                </div>
                <div class="file-list-body">
                    ${this.filteredFiles.length === 0 
                        ? '<div class="no-files">No files found</div>'
                        : this.filteredFiles.map(file => this.createFileItem(file)).join('')
                    }
                </div>
                    <div class="file-list-footer">
                        <div class="file-bulk-actions">
                            <button id="bulk-delete" class="btn btn-secondary" disabled>
                                Delete Selected
                            </button>
                            <button id="bulk-download" class="btn btn-primary" disabled>
                                Download Selected
                            </button>
                        </div>
                        <div class="mobile-download-note" style="display: none;">Bulk downloads not supported on mobile</div>
                    </div>
            </div>
        `;

        this.filesListElement.innerHTML = listHTML;

        // Set up event listeners for new elements
        this.setupListEventListeners();
        
        // Update selection display to sync checkbox states
        this.updateSelectionDisplay();
        
        // Update mobile download note visibility
        this.updateMobileDownloadNote();
        
        // Reset all aria-expanded states to false for new items
        document.querySelectorAll('.file-item').forEach(item => {
            item.setAttribute('aria-expanded', 'false');
        });
    }

    /**
     * Create file item HTML for mobile-friendly list
     */
    createFileItem(file) {
        const isSelected = this.selectedFiles.has(file.filename);
        const fileSize = this.api.formatFileSize(file.size);
        const createdDate = this.api.formatDate(file.created_at);
        
        return `
            <div class="file-item ${isSelected ? 'selected' : ''}" data-filename="${file.filename}">
                <div class="file-item-collapsed">
                    <div class="file-item-checkbox">
                        <input type="checkbox" class="file-checkbox-input" ${isSelected ? 'checked' : ''} 
                               data-filename="${file.filename}">
                    </div>
                    <div class="file-item-filename" data-filename="${file.filename}">
                        <span class="filename-text">${file.filename}</span>
                    </div>
                </div>
                <div class="file-item-expanded" style="display: none;">
                    <div class="file-item-details">
                        <div class="file-detail-row">
                            <span class="file-detail-label">Camera:</span>
                            <span class="file-detail-value">${file.camera_alias || 'Unknown'}</span>
                        </div>
                        <div class="file-detail-row">
                            <span class="file-detail-label">Size:</span>
                            <span class="file-detail-value">${fileSize}</span>
                        </div>
                        <div class="file-detail-row">
                            <span class="file-detail-label">Date Created:</span>
                            <span class="file-detail-value">${createdDate}</span>
                        </div>
                    </div>
                    <div class="file-item-actions">
                        <button class="btn btn-sm btn-secondary delete-btn" data-filename="${file.filename}">
                            Delete
                        </button>
                        <button class="btn btn-sm btn-primary download-btn" data-filename="${file.filename}">
                            Download
                        </button>
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * Set up list event listeners
     */
    setupListEventListeners() {
        // Select all checkbox
        const selectAllCheckbox = document.getElementById('select-all');
        if (selectAllCheckbox) {
            // Clone the node to remove all event listeners
            const newCheckbox = selectAllCheckbox.cloneNode(true);
            selectAllCheckbox.parentNode.replaceChild(newCheckbox, selectAllCheckbox);
            
            // Add fresh event listener
            newCheckbox.addEventListener('change', (e) => {
                this.handleSelectAll(e.target.checked);
            });
        }

        // Individual file checkboxes
        document.querySelectorAll('.file-item .file-checkbox-input[data-filename]').forEach(checkbox => {
            checkbox.addEventListener('change', (e) => {
                const filename = e.target.dataset.filename;
                this.handleFileSelection(filename, e.target.checked);
            });
        });

        // File item click handlers for expand/collapse (entire row except checkbox)
        document.querySelectorAll('.file-item').forEach(fileItem => {
            // Make file items focusable for keyboard navigation
            fileItem.setAttribute('tabindex', '0');
            fileItem.setAttribute('role', 'button');
            fileItem.setAttribute('aria-expanded', 'false');
            
            fileItem.addEventListener('click', (e) => {
                // Don't trigger if clicking on checkbox or action buttons
                if (e.target.classList.contains('file-checkbox-input') || 
                    e.target.classList.contains('download-btn') || 
                    e.target.classList.contains('delete-btn') ||
                    e.target.closest('.file-item-actions')) {
                    return;
                }
                
                const filename = fileItem.dataset.filename;
                this.toggleFileExpansion(filename);
            });
            
            // Keyboard support
            fileItem.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    const filename = fileItem.dataset.filename;
                    this.toggleFileExpansion(filename);
                }
            });
        });

        // Download buttons
        document.querySelectorAll('.download-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation(); // Prevent row expansion
                const filename = e.target.dataset.filename;
                this.handleDownload(filename);
            });
        });

        // Delete buttons
        document.querySelectorAll('.delete-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation(); // Prevent row expansion
                const filename = e.target.dataset.filename;
                this.handleDelete(filename);
            });
        });

            // Bulk actions
            const bulkDownloadBtn = document.getElementById('bulk-download');
            const bulkDeleteBtn = document.getElementById('bulk-delete');
            
            if (bulkDownloadBtn) {
                bulkDownloadBtn.addEventListener('click', () => this.handleBulkDownload());
            }
            if (bulkDeleteBtn) {
                bulkDeleteBtn.addEventListener('click', () => this.handleBulkDelete());
            }
    }

    /**
     * Toggle file item expansion
     */
    toggleFileExpansion(filename) {
        const fileItem = document.querySelector(`[data-filename="${filename}"]`);
        if (!fileItem) return;

        const expandedSection = fileItem.querySelector('.file-item-expanded');
        
        if (!expandedSection) return;

        const isExpanded = expandedSection.style.display !== 'none';
        
        if (isExpanded) {
            expandedSection.style.display = 'none';
            fileItem.setAttribute('aria-expanded', 'false');
        } else {
            expandedSection.style.display = 'block';
            fileItem.setAttribute('aria-expanded', 'true');
        }
    }


    /**
     * Handle select all checkbox
     */
    handleSelectAll(checked) {
        if (checked) {
            this.filteredFiles.forEach(file => {
                this.selectedFiles.add(file.filename);
            });
        } else {
            this.selectedFiles.clear();
        }
        
        this.updateSelectionDisplay();
        this.updateBulkActions();
        this.updateMobileDownloadNote();
    }

    /**
     * Handle individual file selection
     */
    handleFileSelection(filename, selected) {
        if (selected) {
            this.selectedFiles.add(filename);
        } else {
            this.selectedFiles.delete(filename);
        }
        
        this.updateSelectionDisplay();
        this.updateBulkActions();
        this.updateMobileDownloadNote();
    }

    /**
     * Update mobile download note visibility
     */
    updateMobileDownloadNote() {
        const mobileNote = document.querySelector('.mobile-download-note');
        if (mobileNote) {
            if (this.isMobile && this.selectedFiles.size > 1) {
                mobileNote.style.display = 'block';
            } else {
                mobileNote.style.display = 'none';
            }
        }
    }

    /**
     * Update selection display
     */
    updateSelectionDisplay() {
        const selectionCount = document.getElementById('selection-count');
        if (selectionCount) {
            const count = this.selectedFiles.size;
            selectionCount.textContent = `Select All (${count} file${count !== 1 ? 's' : ''} selected)`;
        }
        
        // Update select all checkbox state
        const selectAllCheckbox = document.getElementById('select-all');
        if (selectAllCheckbox) {
            const totalFiles = this.filteredFiles.length;
            const selectedCount = this.selectedFiles.size;
            
            if (selectedCount === 0) {
                selectAllCheckbox.checked = false;
                selectAllCheckbox.indeterminate = false;
            } else if (selectedCount === totalFiles) {
                selectAllCheckbox.checked = true;
                selectAllCheckbox.indeterminate = false;
            } else {
                selectAllCheckbox.checked = false;
                selectAllCheckbox.indeterminate = true;
            }
        }
        
        // Update individual file checkboxes
        document.querySelectorAll('.file-item .file-checkbox-input[data-filename]').forEach(checkbox => {
            const filename = checkbox.dataset.filename;
            checkbox.checked = this.selectedFiles.has(filename);
            
            // Update the row's selected class
            const fileItem = checkbox.closest('.file-item');
            if (fileItem) {
                if (this.selectedFiles.has(filename)) {
                    fileItem.classList.add('selected');
                } else {
                    fileItem.classList.remove('selected');
                }
            }
        });
    }

    /**
     * Update bulk action buttons
     */
    updateBulkActions() {
        const hasSelection = this.selectedFiles.size > 0;
        const isMultipleSelection = this.selectedFiles.size > 1;
        const bulkDownloadBtn = document.getElementById('bulk-download');
        const bulkDeleteBtn = document.getElementById('bulk-delete');
        
        if (bulkDownloadBtn) {
            // Disable if no selection, or if mobile with multiple files selected
            bulkDownloadBtn.disabled = !hasSelection || (this.isMobile && isMultipleSelection);
            
            // Update visual state and tooltip for mobile multiple selection
            if (this.isMobile && isMultipleSelection) {
                bulkDownloadBtn.style.opacity = '0.5';
                bulkDownloadBtn.title = 'Bulk downloads not supported on mobile devices';
            } else {
                bulkDownloadBtn.style.opacity = '1';
                bulkDownloadBtn.title = '';
            }
        }
        if (bulkDeleteBtn) {
            bulkDeleteBtn.disabled = !hasSelection;
        }
    }

    /**
     * Handle individual file download
     */
    handleDownload(filename) {
        try {
            const url = this.api.getDownloadURL(filename);
            const link = document.createElement('a');
            link.href = url;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        } catch (error) {
            console.error('Download failed:', error);
            this.showError('Failed to download file');
        }
    }

    /**
     * Handle individual file deletion
     */
    async handleDelete(filename) {
        const ok = await confirmModal({
            title: 'Delete file?',
            message: `Are you sure you want to delete "${filename}"?`,
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!ok) {
            return;
        }

        try {
            this.state.setLoading(true);
            await this.api.deleteVideo(filename);
            this.state.removeProcessedVideo(filename);
            this.selectedFiles.delete(filename);
            this.updateSelectionDisplay();
            this.updateBulkActions();
            this.showSuccess('File deleted successfully');
        } catch (error) {
            console.error('Delete failed:', error);
            this.showError('Failed to delete file');
        } finally {
            this.state.setLoading(false);
        }
    }

    /**
     * Handle bulk download
     */
    async handleBulkDownload() {
        const selectedFilenames = Array.from(this.selectedFiles);
        if (selectedFilenames.length === 0) return;

        try {
            // For now, download files individually
            // In a real implementation, you'd create a ZIP file on the server
            for (const filename of selectedFilenames) {
                this.handleDownload(filename);
                // Small delay between downloads
                await new Promise(resolve => setTimeout(resolve, 100));
            }
        } catch (error) {
            console.error('Bulk download failed:', error);
            this.showError('Failed to download some files');
        }
    }

    /**
     * Handle bulk deletion
     */
    async handleBulkDelete() {
        const selectedFilenames = Array.from(this.selectedFiles);
        if (selectedFilenames.length === 0) return;

        const ok = await confirmModal({
            title: selectedFilenames.length === 1 ? 'Delete file?' : 'Delete files?',
            message:
                selectedFilenames.length === 1
                    ? `Are you sure you want to delete "${selectedFilenames[0]}"?`
                    : `Are you sure you want to delete ${selectedFilenames.length} files?`,
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!ok) {
            return;
        }

        try {
            this.state.setLoading(true);
            
            // Delete files one by one
            for (const filename of selectedFilenames) {
                await this.api.deleteVideo(filename);
                this.state.removeProcessedVideo(filename);
            }
            
            this.selectedFiles.clear();
            this.updateSelectionDisplay();
            this.updateBulkActions();
            this.showSuccess(`${selectedFilenames.length} file(s) deleted successfully`);
        } catch (error) {
            console.error('Bulk delete failed:', error);
            this.showError('Failed to delete some files');
        } finally {
            this.state.setLoading(false);
        }
    }

    /**
     * Show error message
     */
    showError(message) {
        // Use the app's toast system if available
        if (window.app && window.app.showError) {
            window.app.showError(message);
        } else {
            console.error(message);
        }
    }

    /**
     * Show success message
     */
    showSuccess(message) {
        // Use the app's toast system if available
        if (window.app && window.app.showSuccess) {
            window.app.showSuccess(message);
        } else {
            console.log(message);
        }
    }
}

// Export for ES6 module usage
export default FileBrowser;
