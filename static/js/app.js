// HLS Downloader - Frontend JavaScript

let socket;
let currentSessionId = null;
let autoScroll = true;
let availableEpisodes = [];
let parsedUrlData = null;
let cachedSeriesData = null; // Cache for all seasons and episodes
let currentSelectedSeason = null;
let currentSelectedType = 'season'; // 'season' or 'extra'
let seriesCoverUrl = null; // Series cover image URL
let seriesDescription = null; // Series description text

// Multi-season selection storage
let selectedEpisodesPerSeason = {}; // { seasonNum: [1, 2, 3, ...] }
let availableSeasonsData = []; // List of available seasons

// ==========================================
// Performance Optimization: Card Cache
// ==========================================
// Maps session_id (first 8 chars) to DOM element for O(1) lookups
const downloadCardCache = new Map();

// Debounced statistics update to prevent excessive recalculations
let statisticsUpdatePending = false;
const STATISTICS_DEBOUNCE_MS = 300;

function scheduleStatisticsUpdate() {
    if (statisticsUpdatePending) return;
    statisticsUpdatePending = true;
    setTimeout(() => {
        statisticsUpdatePending = false;
        updateOverallStatistics();
    }, STATISTICS_DEBOUNCE_MS);
}

// Find download card/container by session ID (uses cache for performance)
function findDownloadCard(sessionId) {
    const shortId = sessionId.substring(0, 8);

    // Check cache first
    if (downloadCardCache.has(shortId)) {
        const cached = downloadCardCache.get(shortId);
        // Verify it's still in DOM
        if (cached && cached.isConnected) {
            return cached;
        }
        // Remove stale entry
        downloadCardCache.delete(shortId);
    }

    // Search DOM for new series container structure
    const seriesContainers = document.querySelectorAll('.download-series-container');
    for (const container of seriesContainers) {
        if (container.dataset.session === shortId) {
            downloadCardCache.set(shortId, container);
            return container;
        }
    }

    // Fallback: Search for old download-card structure
    const downloadCards = document.querySelectorAll('.download-card');
    for (const card of downloadCards) {
        const sessionSpan = card.querySelector('.download-session-id');
        if (sessionSpan && sessionSpan.textContent.trim() === `Session: ${shortId}...`) {
            downloadCardCache.set(shortId, card);
            return card;
        }
    }

    return null;
}

// Clear card cache (call when download list is re-rendered)
function clearDownloadCardCache() {
    downloadCardCache.clear();
}

// ==========================================
// Toast Notification System
// ==========================================

/**
 * Show a toast notification
 * @param {string} message - The main message to display
 * @param {string} type - Type of toast: 'success', 'error', 'warning', 'info'
 * @param {number} duration - Duration in milliseconds (default: 4000, 0 = permanent)
 * @param {string} title - Optional title for the toast
 */
function showToast(message, type = 'info', duration = 4000, title = null) {
    const container = document.getElementById('toastContainer');
    if (!container) {
        console.error('Toast container not found');
        return;
    }

    // Create toast element
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    // Icon based on type
    const icons = {
        success: '✅',
        error: '❌',
        warning: '⚠️',
        info: 'ℹ️'
    };

    // Default titles
    const defaultTitles = {
        success: 'Success',
        error: 'Error',
        warning: 'Warning',
        info: 'Information'
    };

    const icon = icons[type] || icons.info;
    const toastTitle = title || defaultTitles[type] || defaultTitles.info;

    // Build toast HTML
    toast.innerHTML = `
        <div class="toast-icon">${icon}</div>
        <div class="toast-content">
            <div class="toast-title">${escapeHtml(toastTitle)}</div>
            <div class="toast-message">${escapeHtml(message)}</div>
        </div>
        <button class="toast-close" onclick="closeToast(this)">✕</button>
    `;

    // Add to container
    container.appendChild(toast);

    // Auto-close after duration
    if (duration > 0) {
        setTimeout(() => {
            closeToast(toast.querySelector('.toast-close'));
        }, duration);
    }
}

/**
 * Close a toast notification
 * @param {HTMLElement} closeBtn - The close button element or toast element
 */
function closeToast(closeBtn) {
    const toast = closeBtn.classList.contains('toast') ? closeBtn : closeBtn.closest('.toast');
    if (!toast) return;

    toast.classList.add('hiding');
    setTimeout(() => {
        toast.remove();
    }, 300); // Match CSS animation duration
}

// ==========================================
// Custom Confirmation Modal
// ==========================================

let confirmModalResolve = null;

/**
 * Show a confirmation modal (replaces window.confirm)
 * @param {string} message - The confirmation message
 * @param {string} title - Optional modal title (default: 'Confirmation')
 * @param {string} okText - Text for OK button (default: 'OK')
 * @param {string} cancelText - Text for Cancel button (default: 'Cancel')
 * @returns {Promise<boolean>} Promise that resolves to true if confirmed, false if cancelled
 */
function showConfirm(message, title = 'Confirmation', okText = 'OK', cancelText = 'Cancel') {
    return new Promise((resolve) => {
        const modal = document.getElementById('confirmModal');
        const titleEl = document.getElementById('confirmModalTitle');
        const messageEl = document.getElementById('confirmModalMessage');
        const okBtn = document.getElementById('confirmModalOkBtn');

        // Set content
        titleEl.textContent = title;
        messageEl.textContent = message;
        okBtn.textContent = okText;

        // Find cancel button and update text
        const cancelBtn = modal.querySelector('.btn-secondary');
        if (cancelBtn) {
            cancelBtn.textContent = cancelText;
        }

        // Show modal
        modal.style.display = 'flex';

        // Store resolve function
        confirmModalResolve = resolve;

        // Focus OK button for keyboard accessibility
        setTimeout(() => okBtn.focus(), 100);

        // Add keyboard support
        const handleKeyPress = (e) => {
            if (e.key === 'Enter') {
                closeConfirmModal(true);
                document.removeEventListener('keydown', handleKeyPress);
            } else if (e.key === 'Escape') {
                closeConfirmModal(false);
                document.removeEventListener('keydown', handleKeyPress);
            }
        };
        document.addEventListener('keydown', handleKeyPress);
    });
}

/**
 * Close the confirmation modal
 * @param {boolean} confirmed - Whether the user confirmed or cancelled
 */
function closeConfirmModal(confirmed) {
    const modal = document.getElementById('confirmModal');
    modal.style.display = 'none';

    // Resolve the promise
    if (confirmModalResolve) {
        confirmModalResolve(confirmed);
        confirmModalResolve = null;
    }
}

// Close modal when clicking outside
document.addEventListener('DOMContentLoaded', () => {
    const modal = document.getElementById('confirmModal');
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                closeConfirmModal(false);
            }
        });
    }
});

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    initializeWebSocket();
    loadPreferences();
    initializeMaxConcurrentControl();
    checkActiveDownloadsOnLoad();
});

// Check for active downloads on page load and show section if needed
async function checkActiveDownloadsOnLoad() {
    try {
        const response = await fetch('/api/queue');
        const data = await response.json();

        // If there are any items in queue (processing, queued, etc.), show downloads section
        const activeItems = data.items.filter(item =>
            item.status === 'processing' || item.status === 'queued'
        );

        if (activeItems.length > 0 || data.items.length > 0) {
            console.log(`📥 Found ${data.items.length} items in queue (${activeItems.length} active)`);
            document.getElementById('downloadsSection').style.display = 'block';
            startQueuePolling();
        }
    } catch (error) {
        console.error('Error checking active downloads:', error);
    }
}

// Initialize WebSocket connection
function initializeWebSocket() {
    socket = io();

    socket.on('connect', function() {
        console.log('Connected to server');
        addLog('✅ Connected to server', 'success');
    });

    socket.on('disconnect', function() {
        console.log('Disconnected from server');
        addLog('⚠️ Disconnected from server', 'warning');
    });

    socket.on('log', function(data) {
        if (data.session_id === currentSessionId) {
            addLog(data.message, data.level, data.update_last, data.is_progress);
        }
    });

    socket.on('status', function(data) {
        if (data.session_id === currentSessionId) {
            updateStatus(data);
        }
    });

    socket.on('progress', function(data) {
        // Update the main status section if this is the current session
        if (data.session_id === currentSessionId) {
            updateProgress(data);
        }

        // For parallel downloads: only update progress when we have a 'completed' count
        // This prevents the progress from jumping around when episodes START processing
        if (data.completed !== undefined) {
            // Episode completed - update series-level progress
            updateQueueItemProgress(data.session_id, data.completed, data.total);
        }
        // Note: We ignore progress events without 'completed' field for the queue display
        // to avoid showing misleading progress during parallel downloads
    });

    socket.on('download_progress', function(data) {
        // Update the main status section if this is the current session
        if (data.session_id === currentSessionId) {
            updateDownloadProgress(data);
        }

        // Update queue item with download percentage
        updateQueueItemDownloadProgress(data.session_id, data.percent);
    });

    // Handler for aggregated progress during parallel downloads
    socket.on('aggregated_progress', function(data) {
        // Update queue item with smooth averaged progress
        updateQueueItemAggregatedProgress(data);

        // Update overall statistics if this is a processing download
        updateOverallAggregatedProgress();
    });

    // Handler for individual episode status initialization
    socket.on('episode_status_init', function(data) {
        console.log('📋 Episode status init:', data);
        const { session_id, episodes } = data;
        const shortSessionId = session_id.substring(0, 8);
        const container = document.getElementById(`episodes-${shortSessionId}`);
        if (container) {
            container.innerHTML = renderEpisodeCards(episodes, shortSessionId, session_id);
        }
    });

    // Handler for individual episode status updates (real-time)
    socket.on('episode_status_update', function(data) {
        const { session_id, episode_key, status, progress } = data;
        const shortSessionId = session_id.substring(0, 8);

        // Find the specific episode row (new horizontal layout)
        const episodeRow = document.querySelector(
            `.episode-row[data-episode="${episode_key}"][data-session="${shortSessionId}"]`
        );

        if (episodeRow) {
            // Update status class
            const oldClasses = Array.from(episodeRow.classList).filter(c => !c.startsWith('episode-status-'));
            episodeRow.className = [...oldClasses, `episode-status-${status}`].join(' ');

            // Update status icon
            const statusIcon = episodeRow.querySelector('.episode-status-icon');
            if (statusIcon) {
                statusIcon.textContent = getEpisodeStatusIcon(status);
            }

            // Update progress bar
            const progressFill = episodeRow.querySelector('.episode-progress-fill');
            if (progressFill) {
                progressFill.style.width = `${progress}%`;
            }

            // Update progress text
            const progressText = episodeRow.querySelector('.episode-progress-text');
            if (progressText) {
                progressText.textContent = `${progress.toFixed(0)}%`;
            }

            // Update action button based on new status
            const actionsDiv = episodeRow.querySelector('.episode-actions');
            if (actionsDiv) {
                if (status === 'downloading') {
                    actionsDiv.innerHTML = `<button class="episode-action-btn btn-stop" onclick="stopEpisode('${session_id}', '${episode_key}', event)" title="Stop episode">⏹</button>`;
                } else if (status === 'queued') {
                    actionsDiv.innerHTML = `<button class="episode-action-btn btn-cancel" onclick="cancelEpisode('${session_id}', '${episode_key}', event)" title="Cancel episode">✕</button>`;
                } else {
                    actionsDiv.innerHTML = '';  // Completed or failed - no action
                }
            }

            // Update draggable attribute
            if (status === 'queued') {
                episodeRow.setAttribute('draggable', 'true');
                if (!episodeRow.querySelector('.episode-drag-handle')) {
                    episodeRow.insertAdjacentHTML('afterbegin', '<span class="episode-drag-handle" title="Ziehen zum Neuordnen">⋮⋮</span>');
                }
            } else {
                episodeRow.removeAttribute('draggable');
                const handle = episodeRow.querySelector('.episode-drag-handle');
                if (handle) handle.remove();
            }
        }
    });
}

// Start download
async function startDownload() {
    const url = document.getElementById('url').value.trim();

    if (!url) {
        showToast('Please enter a URL!', 'warning');
        return;
    }

    // Save current season selection before starting
    saveCurrentSeasonSelection();

    console.log('🚀 startDownload called');
    console.log('📦 selectedEpisodesPerSeason:', JSON.stringify(selectedEpisodesPerSeason));

    // Build options from visual selector
    let seasonsString = '';
    let episodesPerSeasonData = {};
    let episodesString = '';

    if (Object.keys(selectedEpisodesPerSeason).length > 0) {
        // Use visual selector data - copy the data to avoid reference issues
        episodesPerSeasonData = JSON.parse(JSON.stringify(selectedEpisodesPerSeason));

        // Extract season numbers for seasonsString (filter out extras)
        const selectedSeasons = Object.keys(selectedEpisodesPerSeason)
            .filter(s => selectedEpisodesPerSeason[s].length > 0)
            .filter(s => !String(s).startsWith('extra_'))  // Exclude extras from season string
            .map(s => parseInt(s))
            .filter(s => !isNaN(s))
            .sort((a, b) => a - b);

        if (selectedSeasons.length > 0) {
            seasonsString = episodesToRangeString(selectedSeasons);
        }

        console.log('📤 episodesPerSeasonData to send:', JSON.stringify(episodesPerSeasonData));
        console.log('📋 seasonsString:', seasonsString);
    } else {
        // Fallback to manual input
        seasonsString = document.getElementById('seasons').value.trim();
        episodesString = document.getElementById('episodes').value.trim();
    }

    // Validate: user must select something
    if (!seasonsString && Object.keys(episodesPerSeasonData).length === 0) {
        showToast('Please select at least one episode or enter seasons/episodes manually!', 'warning');
        return;
    }

    // Collect options
    const maxConcurrentInput = document.getElementById('max_concurrent');
    const maxConcurrent = parseInt(maxConcurrentInput.value) || 3;
    const maxLimit = parseInt(maxConcurrentInput.max) || 20;  // Read max from HTML attribute
    const options = {
        seasons: seasonsString,
        episodes: episodesString,  // Fallback episodes string
        episodes_per_season: episodesPerSeasonData,  // Send per-season selection
        parallel: Math.min(Math.max(1, maxConcurrent), maxLimit),  // Between 1 and maxLimit
        wait: parseInt(document.getElementById('wait').value),
        quality: document.getElementById('quality').value,
        format: document.getElementById('format').value,
        codec: document.getElementById('codec')?.value || 'auto',  // Video codec preference
        series_display: document.getElementById('series_display').value.trim() || null,
        adblock: true,  // Ad-Blocking is always enabled
        english_title: document.getElementById('english_title').checked,
        force: document.getElementById('force').checked,
        audio_only: document.getElementById('audio_only')?.checked ?? false,  // Audio only mode
        audio_format: document.getElementById('audioFormat')?.value || 'mp3',  // Audio format (mp3, flac, etc.)
        audio_bitrate: document.getElementById('audioBitrate')?.value || '0',  // Audio bitrate (0 = best)
        language: document.getElementById('language')?.value || ''  // Selected language key
    };

    // DEBUG: Log language selection
    console.log('🌐 Language element:', document.getElementById('language'));
    console.log('🌐 Language value:', document.getElementById('language')?.value);
    console.log('🌐 Options being sent:', options);

    // Save preferences
    savePreferences(options);

    // Disable start button
    document.getElementById('startBtn').disabled = true;
    document.getElementById('startBtn').textContent = '⏳ Starting...';

    try {
        const response = await fetch('/api/start', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                url,
                options,
                episodes_per_season: episodesPerSeasonData  // Send separately for backend
            })
        });

        const data = await response.json();

        if (response.ok) {
            currentSessionId = data.session_id;

            // Show downloads and log sections (with null checks)
            const downloadsSection = document.getElementById('downloadsSection');
            const logSection = document.getElementById('logSection');
            const startBtn = document.getElementById('startBtn');

            if (downloadsSection) downloadsSection.style.display = 'block';
            if (logSection) logSection.style.display = 'block';

            // Re-enable start button for adding more downloads to queue
            if (startBtn) {
                startBtn.disabled = false;
                startBtn.innerHTML = '<span class="btn-icon-text">🚀</span><span>Start Download</span>';
            }

            // Clear previous logs
            clearLogs();

            // Check if episodes were merged into existing queue item
            if (data.merged_into_existing) {
                // Episodes were added to an existing download
                const addedCount = data.total_new || 0;
                const alreadyCount = data.already_exists?.length || 0;

                if (addedCount > 0) {
                    showToast(`${addedCount} episode(s) added to "${data.series_name}"`, 'success', 4000);
                    addLog(`🔗 ${addedCount} episode(s) added to existing series`, 'success');

                    if (data.added_episodes && data.added_episodes.length > 0) {
                        addLog(`   Added: ${data.added_episodes.join(', ')}`, 'info');
                    }
                } else if (alreadyCount > 0) {
                    showToast(`All episodes are already in the queue`, 'warning', 4000);
                    addLog(`⚠️ All selected episodes are already in the queue`, 'warning');
                }

                if (alreadyCount > 0 && addedCount > 0) {
                    addLog(`   Already exists: ${data.already_exists.join(', ')}`, 'warning');
                }
            } else {
                // New queue entry created
                showToast(`"${options.series_display || 'Series'}" added to queue`, 'success', 3000);
                addLog('🚀 Download added to queue!', 'success');
            }

            // Start queue polling to get real-time updates
            startQueuePolling();

            // Update queue display
            updateQueueStatus();

        } else {
            showToast(data.error || 'Unknown error', 'error', 5000);
            resetUI();
        }
    } catch (error) {
        console.error('Error starting download:', error);
        showToast('Failed to start download: ' + error.message, 'error', 5000);
        resetUI();
    }
}

// Cancel download
async function cancelDownload() {
    if (!currentSessionId) return;

    const confirmed = await showConfirm(
        'Do you really want to cancel the download?',
        'Cancel Download',
        'Yes, cancel',
        'No'
    );

    if (!confirmed) {
        return;
    }

    try {
        await fetch(`/api/cancel/${currentSessionId}`, {
            method: 'POST'
        });

        addLog('🛑 Download cancelled by user', 'warning');
        updateStatusBadge('cancelled', 'Download abgebrochen');

    } catch (error) {
        console.error('Error cancelling download:', error);
        showToast('Failed to cancel download: ' + error.message, 'error', 5000);
    }
}

// Update status
function updateStatus(data) {
    updateStatusBadge(data.status, data.message);

    if (data.successful !== undefined) {
        const successCount = document.getElementById('successCount');
        if (successCount) {
            successCount.textContent = data.successful;
        }
    }

    if (data.failed !== undefined) {
        let failedText = data.failed;

        // Add failed episode numbers if available
        if (data.failed_episodes && data.failed_episodes.length > 0) {
            const episodesList = data.failed_episodes.map(ep => `E${ep}`).join(', ');
            failedText = `${data.failed} (${episodesList})`;
        }

        const failedCount = document.getElementById('failedCount');
        if (failedCount) {
            failedCount.textContent = failedText;
        }
    }

    // If completed or error, reset UI
    if (data.status === 'completed' || data.status === 'error' || data.status === 'cancelled') {
        setTimeout(resetUI, 3000);
    }
}

// Update status badge
function updateStatusBadge(status, message) {
    const badge = document.getElementById('statusBadge');
    const messageEl = document.getElementById('statusMessage');

    // Null checks - these elements may not exist after UI redesign
    if (!badge || !messageEl) {
        return;
    }

    badge.className = 'status-badge ' + status;

    const statusTexts = {
        'queued': 'Queued',
        'processing': 'Processing',
        'completed': 'Completed',
        'error': 'Error',
        'cancelled': 'Cancelled'
    };

    badge.textContent = statusTexts[status] || status;
    messageEl.textContent = message;
}

// Update progress
function updateProgress(data) {
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');
    const currentEpisode = document.getElementById('currentEpisode');

    // Null checks - these elements may not exist after UI redesign
    if (progressFill) {
        const percentage = (data.current / data.total) * 100;
        progressFill.style.width = percentage + '%';
    }
    if (progressText) {
        progressText.textContent = `${data.current} / ${data.total}`;
    }
    if (currentEpisode) {
        currentEpisode.textContent = data.episode;
    }
}

// Update download progress (shows FFmpeg percentage in "Aktuell" field)
function updateDownloadProgress(data) {
    const currentEpisodeEl = document.getElementById('currentEpisode');

    // Null check - this element may not exist after UI redesign
    if (!currentEpisodeEl) {
        return;
    }

    const currentText = currentEpisodeEl.textContent;

    // Extract episode number (e.g., "S01E05" from current text)
    const episodeMatch = currentText.match(/S\d+E\d+/);
    if (episodeMatch) {
        currentEpisodeEl.textContent = `${episodeMatch[0]} - ${data.percent.toFixed(1)}%`;
    } else {
        currentEpisodeEl.textContent = `Downloading... ${data.percent.toFixed(1)}%`;
    }
}

// Add log entry
function addLog(message, level = 'info', updateLast = false, isProgress = false) {
    const logContainer = document.getElementById('logContainer');

    // If this is a progress update, update the last progress entry
    if (updateLast && isProgress) {
        // Find the last progress entry
        const entries = logContainer.querySelectorAll('.log-entry.progress');
        if (entries.length > 0) {
            const lastEntry = entries[entries.length - 1];
            // Update timestamp and message
            const now = new Date();
            const timestamp = now.toTimeString().split(' ')[0];
            lastEntry.textContent = `[${timestamp}] ${message}`;
            lastEntry.className = 'log-entry progress ' + level;

            // Auto-scroll to bottom
            if (autoScroll) {
                logContainer.scrollTop = logContainer.scrollHeight;
            }
            return;
        }
    }

    // Create new log entry
    const entry = document.createElement('div');
    entry.className = 'log-entry ' + level;
    if (isProgress) {
        entry.className += ' progress';
    }

    // Add timestamp
    const now = new Date();
    const timestamp = now.toTimeString().split(' ')[0];
    entry.textContent = `[${timestamp}] ${message}`;

    logContainer.appendChild(entry);

    // Auto-scroll to bottom
    if (autoScroll) {
        logContainer.scrollTop = logContainer.scrollHeight;
    }
}

// Clear logs
function clearLogs() {
    document.getElementById('logContainer').innerHTML = '';
}

// Toggle auto-scroll
function toggleAutoScroll() {
    autoScroll = !autoScroll;
    const icon = document.getElementById('autoScrollIcon');
    icon.textContent = autoScroll ? '📌' : '📍';
}

// Toggle logs collapse/expand
let logsCollapsed = false;
function toggleLogsCollapse() {
    const logContainer = document.getElementById('logContainer');
    const collapseIcon = document.getElementById('logsCollapseIcon');

    logsCollapsed = !logsCollapsed;

    if (logsCollapsed) {
        logContainer.style.display = 'none';
        collapseIcon.style.transform = 'rotate(-90deg)';
        collapseIcon.textContent = '▶';
    } else {
        logContainer.style.display = 'block';
        collapseIcon.style.transform = 'rotate(0deg)';
        collapseIcon.textContent = '▼';
    }
}

// Toggle advanced options
function toggleAdvanced() {
    const advancedOptions = document.getElementById('advanced-options');
    const toggleIcon = document.getElementById('advancedToggleIcon');

    if (advancedOptions.style.display === 'none') {
        advancedOptions.style.display = 'block';
        if (toggleIcon) toggleIcon.textContent = '▲';
    } else {
        advancedOptions.style.display = 'none';
        if (toggleIcon) toggleIcon.textContent = '▼';
    }
}

// Toggle Audio Only mode - switches between video and audio options
function toggleAudioOnlyMode() {
    const audioOnlyCheckbox = document.getElementById('audio_only');
    const codecItem = document.getElementById('codecItem');

    // Video elements
    const qualitySelect = document.getElementById('quality');
    const formatSelect = document.getElementById('format');

    // Audio elements
    const audioBitrateSelect = document.getElementById('audioBitrate');
    const audioFormatSelect = document.getElementById('audioFormat');

    // Labels
    const qualityLabel = document.getElementById('qualityLabel');
    const formatLabel = document.getElementById('formatLabel');

    if (audioOnlyCheckbox && audioOnlyCheckbox.checked) {
        // Audio-only mode: show audio options, hide video options
        if (qualitySelect) qualitySelect.style.display = 'none';
        if (formatSelect) formatSelect.style.display = 'none';
        if (audioBitrateSelect) audioBitrateSelect.style.display = 'block';
        if (audioFormatSelect) audioFormatSelect.style.display = 'block';

        // Hide video codec entirely
        if (codecItem) codecItem.style.display = 'none';

        // Update labels
        if (qualityLabel) qualityLabel.textContent = 'Bitrate';
        if (formatLabel) formatLabel.textContent = 'Audio-Format';
    } else {
        // Normal mode: show video options, hide audio options
        if (qualitySelect) qualitySelect.style.display = 'block';
        if (formatSelect) formatSelect.style.display = 'block';
        if (audioBitrateSelect) audioBitrateSelect.style.display = 'none';
        if (audioFormatSelect) audioFormatSelect.style.display = 'none';

        // Show video codec
        if (codecItem) codecItem.style.display = 'flex';

        // Restore labels
        if (qualityLabel) qualityLabel.textContent = 'Quality';
        if (formatLabel) formatLabel.textContent = 'Format';
    }

    // Save preference
    const prefs = JSON.parse(localStorage.getItem('hlsDownloaderPrefs') || '{}');
    prefs.audio_only = audioOnlyCheckbox?.checked ?? false;
    localStorage.setItem('hlsDownloaderPrefs', JSON.stringify(prefs));
}

// Update language selector with available languages
function updateLanguageSelector(languages) {
    const languageItem = document.getElementById('languageItem');
    const languageSelect = document.getElementById('language');

    if (!languageSelect || !languageItem) return;

    // IMPORTANT: Save the currently selected value BEFORE clearing
    const previouslySelectedValue = languageSelect.value;
    console.log(`🌐 updateLanguageSelector called, preserving selection: "${previouslySelectedValue}"`);

    // Clear existing options except the default
    languageSelect.innerHTML = '<option value="" selected>Standard</option>';

    if (!languages || languages.length === 0) {
        // Hide language selector if no languages available
        languageItem.style.display = 'none';
        return;
    }

    // Show language selector
    languageItem.style.display = 'flex';

    // Add language options
    console.log('🌐 Adding language options:', languages);
    languages.forEach(lang => {
        const option = document.createElement('option');
        option.value = lang.key || '';
        console.log(`   Adding option: key="${lang.key}" title="${lang.title}" alt="${lang.alt}" srcFile="${lang.srcFile}"`);

        // Build display name from available attributes
        // IMPORTANT: The site's title attributes are often incomplete (e.g. "Mit deutschem Untertitel"
        // instead of "Japanisch mit deutschem Untertitel"), so we use srcFile for combined languages
        let displayName = 'Unknown';

        // Comprehensive mapping based on srcFile (most reliable for anime sites)
        const srcFileMap = {
            'german': 'Deutsch',
            'de': 'Deutsch',
            'english': 'Englisch',
            'en': 'Englisch',
            'japanese': 'Japanisch',
            'jp': 'Japanisch',
            'japanese-german': 'Japanisch (Deutsche UT)',
            'japanese-english': 'Japanisch (Englische UT)',
            'english-german': 'Englisch (Deutsche UT)',
            'engger': 'Englisch (Deutsche UT)',
            'gersub': 'Mit deutschen Untertiteln',
            'engsub': 'Mit englischen Untertiteln',
            'japger': 'Japanisch (Deutsche UT)',
            'japeng': 'Japanisch (Englische UT)'
        };

        const srcLower = (lang.srcFile || '').toLowerCase();

        // For combined languages (contain "-"), always use our mapping because site titles are incomplete
        if (srcLower && srcLower.includes('-') && srcFileMap[srcLower]) {
            displayName = srcFileMap[srcLower];
        }
        // For simple languages, prefer the site's title if available
        else if (lang.title && lang.title.trim() && !lang.title.includes('/')) {
            // Use title but skip if it contains "/" (like "Deutsch/German")
            displayName = lang.title.trim();
        }
        else if (srcLower && srcFileMap[srcLower]) {
            // Use our mapping for known srcFiles
            displayName = srcFileMap[srcLower];
        }
        else if (lang.name && lang.name.trim()) {
            displayName = lang.name.trim();
        }
        else if (lang.key) {
            displayName = `Sprache ${lang.key}`;
        }

        option.textContent = displayName;

        // Mark as selected if it was the default on the page
        if (lang.selected) {
            option.selected = true;
        }

        languageSelect.appendChild(option);
    });

    // IMPORTANT: Restore the previously selected value if it still exists in the new options
    if (previouslySelectedValue) {
        // Check if the previous value is still available in the options
        const optionExists = Array.from(languageSelect.options).some(opt => opt.value === previouslySelectedValue);
        if (optionExists) {
            languageSelect.value = previouslySelectedValue;
            console.log(`🌐 Restored previous selection: "${previouslySelectedValue}"`);
        } else {
            console.log(`🌐 Previous selection "${previouslySelectedValue}" no longer available, keeping default`);
        }
    }

    console.log(`🌐 Language selector updated with ${languages.length} languages, current value: "${languageSelect.value}"`);
}

// Hide language selector (called when URL input is cleared)
function hideLanguageSelector() {
    const languageItem = document.getElementById('languageItem');
    if (languageItem) {
        languageItem.style.display = 'none';
    }
}

// Update language selector based on selected episodes (shows common languages)
function updateLanguageSelectorForSelectedEpisodes() {
    if (!cachedSeriesData) return;

    // Get all selected episodes across all seasons
    const selectedEpisodes = [];
    Object.keys(selectedEpisodesPerSeason).forEach(seasonKey => {
        const episodes = selectedEpisodesPerSeason[seasonKey];
        if (episodes && episodes.length > 0) {
            episodes.forEach(epNum => {
                selectedEpisodes.push({ season: seasonKey, episode: epNum });
            });
        }
    });

    if (selectedEpisodes.length === 0) {
        // No episodes selected - show all available languages from the series
        updateLanguageSelector(cachedSeriesData.languages || []);
        return;
    }

    // Collect languages from all selected episodes
    const languageCounts = new Map(); // key -> { lang: {...}, count: number }
    let totalSelected = 0;

    selectedEpisodes.forEach(({ season, episode }) => {
        // Determine if this is a regular season or extra tab
        let episodeDetails = null;

        if (cachedSeriesData.seasons_data && cachedSeriesData.seasons_data[season]) {
            episodeDetails = cachedSeriesData.seasons_data[season].episode_details;
        } else if (cachedSeriesData.extras_data && cachedSeriesData.extras_data[season]) {
            episodeDetails = cachedSeriesData.extras_data[season].episode_details;
        }

        if (episodeDetails && episodeDetails[episode] && episodeDetails[episode].languages) {
            const epLangs = episodeDetails[episode].languages;
            epLangs.forEach(lang => {
                const key = lang.key || lang.srcFile;
                if (key) {
                    if (!languageCounts.has(key)) {
                        languageCounts.set(key, { lang: lang, count: 0 });
                    }
                    languageCounts.get(key).count++;
                }
            });
            totalSelected++;
        }
    });

    // Only show languages that are available for ALL selected episodes
    const commonLanguages = [];
    languageCounts.forEach((data, key) => {
        if (data.count === totalSelected) {
            commonLanguages.push(data.lang);
        }
    });

    // If we couldn't find per-episode languages, fall back to series-level languages
    if (commonLanguages.length === 0 && cachedSeriesData.languages) {
        updateLanguageSelector(cachedSeriesData.languages);
    } else {
        updateLanguageSelector(commonLanguages);
    }
}

// Reset UI to initial state
function resetUI() {
    const startBtn = document.getElementById('startBtn');
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');
    const currentEpisode = document.getElementById('currentEpisode');
    const successCount = document.getElementById('successCount');
    const failedCount = document.getElementById('failedCount');

    if (startBtn) {
        startBtn.disabled = false;
        startBtn.innerHTML = '<span class="btn-icon-text">🚀</span><span>Start Download</span>';
        startBtn.style.display = 'inline-flex';
    }
    currentSessionId = null;

    // Clear all episode selections
    selectedEpisodesPerSeason = {};

    // Deselect all episode buttons
    document.querySelectorAll('.episode-btn.selected').forEach(btn => {
        btn.classList.remove('selected');
    });

    // Update download summary
    updateDownloadSummary();

    // Reset progress counters (with null checks)
    if (progressFill) progressFill.style.width = '0%';
    if (progressText) progressText.textContent = '0 / 0';
    if (currentEpisode) currentEpisode.textContent = '-';
    if (successCount) successCount.textContent = '0';
    if (failedCount) failedCount.textContent = '0';
}

// Save preferences to localStorage
function savePreferences(options) {
    const prefs = {
        wait: options.wait,
        quality: options.quality,
        format: options.format,
        codec: options.codec,
        english_title: options.english_title,
        force: options.force,
        audio_only: options.audio_only,
        audio_format: options.audio_format,
        audio_bitrate: options.audio_bitrate
    };
    localStorage.setItem('hlsDownloaderPrefs', JSON.stringify(prefs));
}

// Load preferences from localStorage
function loadPreferences() {
    const saved = localStorage.getItem('hlsDownloaderPrefs');
    if (!saved) return;

    try {
        const prefs = JSON.parse(saved);

        if (prefs.wait) document.getElementById('wait').value = prefs.wait;
        if (prefs.parallel) document.getElementById('max_concurrent').value = prefs.parallel;
        if (prefs.quality) document.getElementById('quality').value = prefs.quality;
        if (prefs.format) document.getElementById('format').value = prefs.format;
        if (prefs.codec && document.getElementById('codec')) document.getElementById('codec').value = prefs.codec;
        if (prefs.english_title !== undefined) document.getElementById('english_title').checked = prefs.english_title;
        if (prefs.force !== undefined) document.getElementById('force').checked = prefs.force;
        // Load audio settings
        if (prefs.audio_format && document.getElementById('audioFormat')) {
            document.getElementById('audioFormat').value = prefs.audio_format;
        }
        if (prefs.audio_bitrate && document.getElementById('audioBitrate')) {
            document.getElementById('audioBitrate').value = prefs.audio_bitrate;
        }
        if (prefs.audio_only !== undefined && document.getElementById('audio_only')) {
            document.getElementById('audio_only').checked = prefs.audio_only;
            // Apply the audio-only mode state to UI
            toggleAudioOnlyMode();
        }

    } catch (error) {
        console.error('Error loading preferences:', error);
    }
}

// Initialize max concurrent downloads control
function initializeMaxConcurrentControl() {
    const maxConcurrentInput = document.getElementById('max_concurrent');
    if (!maxConcurrentInput) return;

    // Load current value from backend
    fetch('/api/queue/max-concurrent')
        .then(res => res.json())
        .then(data => {
            maxConcurrentInput.value = data.max_concurrent;
            console.log(`⚙️ Max concurrent downloads: ${data.max_concurrent}`);
        })
        .catch(err => console.error('Error loading max concurrent:', err));

    // Update backend when user changes value
    maxConcurrentInput.addEventListener('change', async function() {
        const newValue = parseInt(this.value);
        const maxLimit = parseInt(this.max) || 20;  // Read max from HTML attribute

        if (newValue < 1 || newValue > maxLimit) {
            showToast(`Wert muss zwischen 1 und ${maxLimit} liegen`, 'warning');
            return;
        }

        try {
            const response = await fetch('/api/queue/max-concurrent', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({max_concurrent: newValue})
            });

            const data = await response.json();

            if (response.ok) {
                showToast(`Max parallel downloads: ${newValue}`, 'success', 3000);
                console.log(`✅ Max concurrent updated: ${data.old_value} → ${data.new_value}`);
            } else {
                showToast(data.error || 'Error updating', 'error');
            }
        } catch (error) {
            showToast('Error updating: ' + error.message, 'error');
        }
    });
}

// Parse URL manually (called by button click)
async function parseURLManually() {
    const url = document.getElementById('url').value.trim();

    if (!url) {
        showToast('Please enter a valid URL!', 'warning');
        return;
    }

    // Show loading indicator
    showParsingIndicator();

    try {
        const response = await fetch('/api/parse-url', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                url: url,
                force_refresh: false
            })
        });

        const data = await response.json();

        if (response.ok) {
            parsedUrlData = data;

            // Show notification based on cache status
            if (data.from_cache) {
                console.log('✅ Data loaded from cache');
            } else {
                console.log('🔍 New data scraped and cached');
            }

            // Convert seasons_data dictionary to array format
            const seasonsArray = [];
            if (data.seasons_data) {
                for (const seasonNum in data.seasons_data) {
                    seasonsArray.push({
                        season: parseInt(seasonNum),
                        episodes: data.seasons_data[seasonNum].episodes || [],
                        episode_details: data.seasons_data[seasonNum].episode_details || {}
                    });
                }
            }

            // Convert extras_data (Filme, Specials, etc.) to array format
            const extrasArray = [];
            if (data.extras_data) {
                for (const extraName in data.extras_data) {
                    extrasArray.push({
                        name: extraName,
                        label: extraName.charAt(0).toUpperCase() + extraName.slice(1), // Capitalize
                        episodes: data.extras_data[extraName].episodes || [],
                        episode_details: data.extras_data[extraName].episode_details || {}
                    });
                }
            }

            // Cache the data
            // Use real series name from page if available, fallback to URL slug
            const seriesName = data.series_name || data.series_slug?.replace(/-/g, ' ');
            cachedSeriesData = {
                series_name: seriesName,
                seasons: seasonsArray,
                extras: extrasArray,
                extra_tabs: data.extra_tabs || [],
                languages: data.languages || []
            };

            // Also update the series_display input field with the real name
            const seriesDisplayInput = document.getElementById('series_display');
            if (seriesDisplayInput && seriesName) {
                seriesDisplayInput.value = seriesName;
                seriesDisplayInput.placeholder = seriesName;
            }
            seriesCoverUrl = data.series_cover_url; // Cache cover image
            seriesDescription = data.series_description; // Cache description

            console.log('Parsed data:', cachedSeriesData);

            // Update language selector if languages available
            updateLanguageSelector(data.languages || []);

            // Show enhanced selector with all seasons
            showEnhancedSelector();

            hideParsingIndicator();
        } else {
            console.error('Error parsing URL:', data.error);
            showToast(`Error analyzing URL: ${data.error}`, 'error', 6000);
            hideParsingIndicator();
            hideEpisodeSelector();
        }
    } catch (error) {
        console.error('Error parsing URL:', error);
        showToast(`Error analyzing URL: ${error.message}`, 'error', 6000);
        hideParsingIndicator();
        hideEpisodeSelector();
    }
}

// Show parsing indicator
function showParsingIndicator() {
    const indicator = document.getElementById('parsingIndicator');
    if (indicator) {
        indicator.style.display = 'block';
    }
}

// Hide parsing indicator
function hideParsingIndicator() {
    const indicator = document.getElementById('parsingIndicator');
    if (indicator) {
        indicator.style.display = 'none';
    }
}

// Show episode selector with checkboxes (new layout like screenshot)
function showEpisodeSelector(episodes, season, episodeDetails = {}) {
    // Find or create episodes container
    let episodesContainer = document.getElementById('episodesContainer');

    if (!episodesContainer) {
        // If called directly (not from season selector), use main container
        episodesContainer = document.getElementById('episodeSelector');
        if (!episodesContainer) return;
        episodesContainer.innerHTML = '';
    } else {
        // Clear only episodes container
        episodesContainer.innerHTML = '';
    }

    if (!episodes || episodes.length === 0) {
        episodesContainer.style.display = 'none';
        return;
    }

    currentSelectedSeason = season;

    // Episodes label and buttons (like "Episoden: 1 2 3 4 5...")
    const episodesTabsContainer = document.createElement('div');
    episodesTabsContainer.className = 'episodes-tabs-container';

    const episodesLabel = document.createElement('span');
    episodesLabel.className = 'tabs-label';
    episodesLabel.textContent = 'Episodes:';
    episodesTabsContainer.appendChild(episodesLabel);

    const episodesTabsDiv = document.createElement('div');
    episodesTabsDiv.className = 'episode-tabs';

    episodes.forEach(epNum => {
        const epButton = document.createElement('button');
        epButton.className = 'episode-tab';
        epButton.textContent = epNum;
        epButton.dataset.episode = epNum;

        // Add title as tooltip if available
        if (episodeDetails[epNum]) {
            const title = episodeDetails[epNum].title_de || episodeDetails[epNum].title_en;
            if (title) {
                epButton.title = title;
            }
        }

        // Toggle selection on click
        epButton.onclick = function() {
            epButton.classList.toggle('selected');
            updateEpisodeRangeFromButtons();
        };

        episodesTabsDiv.appendChild(epButton);
    });

    episodesTabsContainer.appendChild(episodesTabsDiv);
    episodesContainer.appendChild(episodesTabsContainer);

    // Select all button
    const selectAllContainer = document.createElement('div');
    selectAllContainer.className = 'select-all-container';

    const selectAllBtn = document.createElement('button');
    selectAllBtn.textContent = 'Select all';
    selectAllBtn.className = 'select-all-btn';
    selectAllBtn.onclick = function() {
        const buttons = episodesTabsDiv.querySelectorAll('.episode-tab');
        const allSelected = Array.from(buttons).every(b => b.classList.contains('selected'));

        buttons.forEach(b => {
            if (allSelected) {
                b.classList.remove('selected');
            } else {
                b.classList.add('selected');
            }
        });

        selectAllBtn.textContent = allSelected ? 'Select all' : 'Deselect all';
        updateEpisodeRangeFromButtons();
    };

    selectAllContainer.appendChild(selectAllBtn);
    episodesContainer.appendChild(selectAllContainer);

    episodesContainer.style.display = 'block';

    // Hide the old episodes input
    const episodesInput = document.getElementById('episodes');
    if (episodesInput) {
        episodesInput.parentElement.style.display = 'none';
    }
}

// Update episode range from button selection
function updateEpisodeRangeFromButtons() {
    const buttons = document.querySelectorAll('.episode-tab.selected');
    const selectedEpisodes = Array.from(buttons)
        .map(b => parseInt(b.dataset.episode))
        .sort((a, b) => a - b);

    let rangeString = '';
    if (selectedEpisodes.length > 0) {
        rangeString = episodesToRangeString(selectedEpisodes);
    }

    document.getElementById('episodes').value = rangeString;
}

// Update episode range input from checkboxes
function updateEpisodeRangeFromCheckboxes() {
    const container = document.getElementById('episodeSelector');
    const checkboxes = container.querySelectorAll('input[type="checkbox"]:checked');

    const selectedEpisodes = Array.from(checkboxes)
        .map(cb => parseInt(cb.value))
        .sort((a, b) => a - b);

    // Convert to range string
    let rangeString = '';
    if (selectedEpisodes.length > 0) {
        rangeString = episodesToRangeString(selectedEpisodes);
    }

    document.getElementById('episodes').value = rangeString;
}

// Convert episode array to range string (e.g., [1,2,3,5,6,7] -> "1-3,5-7")
function episodesToRangeString(episodes) {
    if (episodes.length === 0) return '';

    const ranges = [];
    let start = episodes[0];
    let end = episodes[0];

    for (let i = 1; i <= episodes.length; i++) {
        if (i < episodes.length && episodes[i] === end + 1) {
            end = episodes[i];
        } else {
            if (start === end) {
                ranges.push(start.toString());
            } else {
                ranges.push(`${start}-${end}`);
            }
            if (i < episodes.length) {
                start = episodes[i];
                end = episodes[i];
            }
        }
    }

    return ranges.join(',');
}

// Hide episode selector
function hideEpisodeSelector() {
    const container = document.getElementById('episodeSelector');
    if (container) {
        container.style.display = 'none';
    }

    const episodesInput = document.getElementById('episodes');
    if (episodesInput) {
        episodesInput.parentElement.style.display = 'block';
    }

    // Also hide language selector when episode selector is hidden
    hideLanguageSelector();
}

// Show season selector
function showSeasonSelector(totalSeasons, currentSeason = null) {
    const container = document.getElementById('episodeSelector');
    if (!container || !totalSeasons) return;

    // Clear previous
    container.innerHTML = '';

    // Create series info header layout (similar to screenshot)
    const seriesInfoContainer = document.createElement('div');
    seriesInfoContainer.className = 'series-info-layout';

    // Left side: Cover image
    if (seriesCoverUrl) {
        const coverDiv = document.createElement('div');
        coverDiv.className = 'series-cover-box';

        const coverImg = document.createElement('img');
        coverImg.src = seriesCoverUrl;
        coverImg.alt = 'Series Cover';
        coverImg.className = 'series-cover-image';
        coverImg.onerror = function() {
            coverDiv.style.display = 'none';
        };

        coverDiv.appendChild(coverImg);
        seriesInfoContainer.appendChild(coverDiv);
    }

    // Right side: Series info
    const infoDiv = document.createElement('div');
    infoDiv.className = 'series-info-box';

    const title = document.createElement('h2');
    title.className = 'series-title';
    // Use real series name from cached data if available, fallback to URL slug
    const displayName = cachedSeriesData?.series_name || parsedUrlData?.series_name || parsedUrlData?.series_slug?.replace(/-/g, ' ') || 'Series';
    title.textContent = displayName;
    infoDiv.appendChild(title);

    const seasonInfo = document.createElement('p');
    seasonInfo.className = 'series-meta';
    seasonInfo.textContent = `${totalSeasons} Season${totalSeasons > 1 ? 's' : ''}`;
    infoDiv.appendChild(seasonInfo);

    // Add description if available
    if (seriesDescription) {
        const descDiv = document.createElement('p');
        descDiv.className = 'series-description';
        descDiv.textContent = seriesDescription;
        infoDiv.appendChild(descDiv);
    }

    seriesInfoContainer.appendChild(infoDiv);
    container.appendChild(seriesInfoContainer);

    // Season tabs (like "Staffeln: Filme 1 2 3")
    const seasonTabsContainer = document.createElement('div');
    seasonTabsContainer.className = 'season-tabs-container';

    const tabsLabel = document.createElement('span');
    tabsLabel.className = 'tabs-label';
    tabsLabel.textContent = 'Seasons:';
    seasonTabsContainer.appendChild(tabsLabel);

    const tabsDiv = document.createElement('div');
    tabsDiv.className = 'season-tabs';

    for (let seasonNum = 1; seasonNum <= totalSeasons; seasonNum++) {
        const seasonTab = document.createElement('button');
        seasonTab.className = 'season-tab';
        seasonTab.textContent = seasonNum;

        // Highlight current/first season
        if (seasonNum === (currentSeason || 1)) {
            seasonTab.classList.add('active');
        }

        seasonTab.onclick = function() {
            // Update active tab
            tabsDiv.querySelectorAll('.season-tab').forEach(t => t.classList.remove('active'));
            seasonTab.classList.add('active');

            // Load episodes for this season
            selectSeasonFromCache(seasonNum);
        };

        tabsDiv.appendChild(seasonTab);
    }

    seasonTabsContainer.appendChild(tabsDiv);
    container.appendChild(seasonTabsContainer);

    // Auto-select first season if no current season
    if (!currentSeason && cachedSeriesData && cachedSeriesData[1]) {
        // Show episodes for first season immediately
        const episodesDiv = document.createElement('div');
        episodesDiv.id = 'episodesContainer';
        container.appendChild(episodesDiv);

        // Populate with first season's episodes
        setTimeout(() => selectSeasonFromCache(1), 100);
    }

    container.style.display = 'block';

    // Hide episodes input
    const episodesInput = document.getElementById('episodes');
    if (episodesInput) {
        episodesInput.parentElement.style.display = 'none';
    }
}

// Select a season from cache (instant, no API call)
function selectSeasonFromCache(seasonNum) {
    if (!cachedSeriesData || !cachedSeriesData[seasonNum]) {
        showToast(`Season ${seasonNum} data not available. Please wait until scraping is complete.`, 'warning', 5000);
        return;
    }

    const seasonData = cachedSeriesData[seasonNum];

    // Update URL to reflect selected season
    if (parsedUrlData && parsedUrlData.series_slug) {
        const baseUrl = document.getElementById('url').value.trim().match(/https?:\/\/[^\/]+\/serie\/stream\/[^\/]+/)[0];
        const seasonUrl = `${baseUrl}/staffel-${seasonNum}`;
        document.getElementById('url').value = seasonUrl;
    }

    // Show episodes immediately from cache
    showEpisodeSelector(seasonData.episodes, seasonNum, seasonData.episode_details);
}

// Example URL helper
function fillExampleURL() {
    document.getElementById('url').value = 'http://186.2.175.5/serie/stream/unbesiegbar/staffel-3/episode-1';
    // User must click "URL Analysieren" button manually
}

// Set "all episodes" based on cached data
function setAllEpisodes() {
    if (!cachedSeriesData || !currentSelectedSeason) {
        // Try to parse from URL
        const url = document.getElementById('url').value.trim();
        if (!url) {
            alert('Please enter and analyze a URL first!');
            return;
        }

        // Trigger parse and then set all episodes
        parseURLManually().then(() => {
            if (cachedSeriesData && currentSelectedSeason) {
                const seasonData = cachedSeriesData.seasons.find(s => s.season === currentSelectedSeason);
                if (seasonData && seasonData.episodes.length > 0) {
                    const maxEpisode = Math.max(...seasonData.episodes);
                    document.getElementById('episodes').value = `1-${maxEpisode}`;
                } else {
                    showToast('Could not determine episode count. Please enter manually.', 'warning', 5000);
                }
            }
        });
        return;
    }

    // Find current season data
    const seasonData = cachedSeriesData.seasons.find(s => s.season === currentSelectedSeason);
    if (seasonData && seasonData.episodes.length > 0) {
        const maxEpisode = Math.max(...seasonData.episodes);
        document.getElementById('episodes').value = `1-${maxEpisode}`;
        console.log(`Set episodes to 1-${maxEpisode} for season ${currentSelectedSeason}`);
    } else {
        showToast('Could not determine episode count. Please enter manually.', 'warning', 5000);
    }
}

// Set "all seasons" based on cached data
function setAllSeasons() {
    if (!cachedSeriesData) {
        // Try to parse from URL
        const url = document.getElementById('url').value.trim();
        if (!url) {
            showToast('Please enter and analyze a URL first!', 'warning');
            return;
        }

        // Trigger parse and then set all seasons
        parseURLManually().then(() => {
            if (cachedSeriesData && cachedSeriesData.seasons.length > 0) {
                const maxSeason = Math.max(...cachedSeriesData.seasons.map(s => s.season));
                document.getElementById('seasons').value = `1-${maxSeason}`;
                document.getElementById('episodes').value = 'all';
            }
        });
        return;
    }

    if (cachedSeriesData.seasons.length > 0) {
        const maxSeason = Math.max(...cachedSeriesData.seasons.map(s => s.season));
        document.getElementById('seasons').value = `1-${maxSeason}`;
        // Set episodes to "all" to download all episodes of all seasons
        document.getElementById('episodes').value = 'all';
        console.log(`Set seasons to 1-${maxSeason}`);
    } else {
        showToast('Could not determine season count. Please enter manually.', 'warning', 5000);
    }
}

// ========================================
// MULTI-SEASON EPISODE SELECTOR
// ========================================

// Show enhanced season/episode selector after URL parse
function showEnhancedSelector() {
    console.log('showEnhancedSelector called', cachedSeriesData);

    if (!cachedSeriesData || !cachedSeriesData.seasons || cachedSeriesData.seasons.length === 0) {
        console.error('No seasons data available');
        return;
    }

    const container = document.getElementById('episodeSelector');
    container.innerHTML = '';
    container.style.display = 'block';

    // Store available seasons
    availableSeasonsData = cachedSeriesData.seasons;

    // Add series cover and info if available
    if (seriesCoverUrl || cachedSeriesData.series_name) {
        const seriesInfoLayout = document.createElement('div');
        seriesInfoLayout.className = 'series-info-layout';

        // Cover image
        if (seriesCoverUrl) {
            const coverBox = document.createElement('div');
            coverBox.className = 'series-cover-box';
            const coverImg = document.createElement('img');
            coverImg.src = seriesCoverUrl;
            coverImg.alt = cachedSeriesData.series_name || 'Series Cover';
            coverBox.appendChild(coverImg);
            seriesInfoLayout.appendChild(coverBox);
        }

        // Series info
        const infoBox = document.createElement('div');
        infoBox.className = 'series-info-box';

        if (cachedSeriesData.series_name) {
            const seriesTitle = document.createElement('div');
            seriesTitle.className = 'series-title';
            seriesTitle.textContent = cachedSeriesData.series_name;
            infoBox.appendChild(seriesTitle);
        }

        const seriesMeta = document.createElement('div');
        seriesMeta.className = 'series-meta';
        let metaText = `${cachedSeriesData.seasons.length} Season${cachedSeriesData.seasons.length > 1 ? 's' : ''}`;
        // Add extra tabs info if present
        if (cachedSeriesData.extras && cachedSeriesData.extras.length > 0) {
            const extraLabels = cachedSeriesData.extras.map(e => e.label).join(', ');
            metaText += ` + ${extraLabels}`;
        }
        seriesMeta.textContent = metaText;
        infoBox.appendChild(seriesMeta);

        if (seriesDescription) {
            const seriesDesc = document.createElement('div');
            seriesDesc.className = 'series-description';
            seriesDesc.textContent = seriesDescription;
            infoBox.appendChild(seriesDesc);
        }

        seriesInfoLayout.appendChild(infoBox);
        container.appendChild(seriesInfoLayout);
    }

    // Create season tabs
    const seasonTabsContainer = document.createElement('div');
    seasonTabsContainer.className = 'season-tabs-container';

    const tabsLabel = document.createElement('div');
    tabsLabel.className = 'tabs-label';
    tabsLabel.textContent = 'Seasons:';
    seasonTabsContainer.appendChild(tabsLabel);

    const seasonTabs = document.createElement('div');
    seasonTabs.className = 'season-tabs';

    cachedSeriesData.seasons.forEach(seasonData => {
        const seasonTab = document.createElement('button');
        seasonTab.className = 'season-tab';
        seasonTab.textContent = seasonData.season;
        seasonTab.dataset.season = seasonData.season;
        seasonTab.dataset.type = 'season';

        if (seasonData.season === currentSelectedSeason || (!currentSelectedSeason && seasonData.season === cachedSeriesData.seasons[0].season)) {
            seasonTab.classList.add('active');
            currentSelectedSeason = seasonData.season;
            currentSelectedType = 'season';
        }

        seasonTab.onclick = function() {
            switchToSeason(seasonData.season, 'season');
        };

        seasonTabs.appendChild(seasonTab);
    });

    // Add extra tabs (Filme, Specials, etc.) if present
    if (cachedSeriesData.extras && cachedSeriesData.extras.length > 0) {
        cachedSeriesData.extras.forEach(extraData => {
            const extraTab = document.createElement('button');
            extraTab.className = 'season-tab extra-tab';
            extraTab.textContent = extraData.label;
            extraTab.dataset.extra = extraData.name;
            extraTab.dataset.type = 'extra';
            extraTab.title = `${extraData.episodes.length} Episodes`;

            extraTab.onclick = function() {
                switchToSeason(extraData.name, 'extra');
            };

            seasonTabs.appendChild(extraTab);
        });
    }

    seasonTabsContainer.appendChild(seasonTabs);
    container.appendChild(seasonTabsContainer);

    // Create episode selector container
    const episodesContainer = document.createElement('div');
    episodesContainer.className = 'episodes-selector-container';
    episodesContainer.id = 'episodesSelectorContainer';
    container.appendChild(episodesContainer);

    // Show episodes for current season
    showEpisodesForSeason(currentSelectedSeason);

    // Add download summary
    updateDownloadSummary();
}

// Switch to a different season or extra tab
function switchToSeason(identifier, type = 'season') {
    // Save current season's selection before switching
    saveCurrentSeasonSelection();

    // Update current selection
    currentSelectedSeason = identifier;
    currentSelectedType = type;

    // Update active tab
    document.querySelectorAll('.season-tab').forEach(tab => {
        const isMatch = (type === 'season' && parseInt(tab.dataset.season) === identifier) ||
                       (type === 'extra' && tab.dataset.extra === identifier);
        if (isMatch) {
            tab.classList.add('active');
        } else {
            tab.classList.remove('active');
        }
    });

    // Show episodes for new season/extra
    showEpisodesForSeason(identifier, type);
}

// Show episodes for a specific season or extra tab
function showEpisodesForSeason(identifier, type = 'season') {
    let itemData;
    let labelText;

    if (type === 'extra') {
        // Find extra tab data
        itemData = cachedSeriesData.extras?.find(e => e.name === identifier);
        labelText = itemData?.label || identifier;
    } else {
        // Find season data
        itemData = availableSeasonsData.find(s => s.season === identifier);
        labelText = `Season ${identifier}`;
    }

    if (!itemData) return;

    const container = document.getElementById('episodesSelectorContainer');
    container.innerHTML = '';

    // Episode label with count badge
    const episodes = itemData.episodes || [];
    const selectionKey = type === 'extra' ? `extra_${identifier}` : identifier;
    const selectedEpisodes = selectedEpisodesPerSeason[selectionKey] || [];

    const episodeLabel = document.createElement('div');
    episodeLabel.className = 'selector-label';
    episodeLabel.innerHTML = `
        Episodes (${labelText}):
        <span class="episode-count-badge">${selectedEpisodes.length}/${episodes.length}</span>
    `;
    container.appendChild(episodeLabel);

    // Episode buttons
    const episodesGrid = document.createElement('div');
    episodesGrid.className = 'episodes-grid';

    episodes.forEach(epNum => {
        const epButton = document.createElement('button');
        epButton.className = 'episode-btn';
        epButton.textContent = epNum;
        epButton.dataset.episode = epNum;

        // Check if already selected
        if (selectedEpisodes.includes(epNum)) {
            epButton.classList.add('selected');
        }

        // Add title tooltip if available
        if (itemData.episode_details && itemData.episode_details[epNum]) {
            const title = itemData.episode_details[epNum].title_de || itemData.episode_details[epNum].title_en;
            if (title) {
                epButton.title = title;
            }
        }

        epButton.onclick = function() {
            toggleEpisodeSelection(selectionKey, epNum, type, identifier);
        };

        episodesGrid.appendChild(epButton);
    });

    container.appendChild(episodesGrid);

    // Control buttons
    const controlsContainer = document.createElement('div');
    controlsContainer.className = 'selector-controls';

    const selectAllBtn = document.createElement('button');
    selectAllBtn.className = 'btn-small';
    selectAllBtn.textContent = '✅ Select all';
    selectAllBtn.onclick = function() {
        selectAllEpisodes(selectionKey, episodes, type, identifier);
    };

    const deselectAllBtn = document.createElement('button');
    deselectAllBtn.className = 'btn-small';
    deselectAllBtn.textContent = '❌ Deselect all';
    deselectAllBtn.onclick = function() {
        deselectAllEpisodes(selectionKey);
    };

    controlsContainer.appendChild(selectAllBtn);
    controlsContainer.appendChild(deselectAllBtn);

    // Smart selection buttons
    const firstHalfBtn = document.createElement('button');
    firstHalfBtn.className = 'btn-small';
    firstHalfBtn.textContent = '◀️ First Half';
    firstHalfBtn.title = 'Select first 50% of episodes';
    firstHalfBtn.onclick = function() {
        selectFirstHalf(selectionKey, episodes);
    };

    const patternBtn = document.createElement('button');
    patternBtn.className = 'btn-small btn-primary';
    patternBtn.textContent = '🔀 Custom Pattern';
    patternBtn.title = 'E.g. "1,3,5-7,10-15"';
    patternBtn.onclick = function() {
        showPatternModal(selectionKey, episodes);
    };

    controlsContainer.appendChild(firstHalfBtn);
    controlsContainer.appendChild(patternBtn);

    container.appendChild(controlsContainer);

    // Update language selector for this season's episodes
    updateLanguageSelectorForSelectedEpisodes();
}

// Toggle episode selection
function toggleEpisodeSelection(seasonNum, epNum) {
    if (!selectedEpisodesPerSeason[seasonNum]) {
        selectedEpisodesPerSeason[seasonNum] = [];
    }

    const index = selectedEpisodesPerSeason[seasonNum].indexOf(epNum);
    const button = document.querySelector(`.episode-btn[data-episode="${epNum}"]`);

    if (index > -1) {
        // Deselect
        selectedEpisodesPerSeason[seasonNum].splice(index, 1);
        button.classList.remove('selected');
    } else {
        // Select
        selectedEpisodesPerSeason[seasonNum].push(epNum);
        selectedEpisodesPerSeason[seasonNum].sort((a, b) => a - b);
        button.classList.add('selected');
    }

    updateDownloadSummary();
    updateLanguageSelectorForSelectedEpisodes();
}

// Select all episodes in a season
function selectAllEpisodes(seasonNum, episodes) {
    selectedEpisodesPerSeason[seasonNum] = [...episodes];
    document.querySelectorAll('.episode-btn').forEach(btn => {
        btn.classList.add('selected');
    });
    updateDownloadSummary();
    updateLanguageSelectorForSelectedEpisodes();
}

// Deselect all episodes in a season
function deselectAllEpisodes(seasonNum) {
    selectedEpisodesPerSeason[seasonNum] = [];
    document.querySelectorAll('.episode-btn').forEach(btn => {
        btn.classList.remove('selected');
    });
    updateDownloadSummary();
    updateLanguageSelectorForSelectedEpisodes();
}

// Save current season's selection
function saveCurrentSeasonSelection() {
    if (!currentSelectedSeason) return;

    // Determine the correct selection key based on type
    const selectionKey = currentSelectedType === 'extra'
        ? `extra_${currentSelectedSeason}`
        : currentSelectedSeason;

    const selectedButtons = document.querySelectorAll('.episode-btn.selected');
    const selected = Array.from(selectedButtons).map(btn => parseInt(btn.dataset.episode));

    console.log(`💾 saveCurrentSeasonSelection: type=${currentSelectedType}, key=${selectionKey}, selected=${selected.length} episodes`);

    if (selected.length > 0) {
        selectedEpisodesPerSeason[selectionKey] = selected.sort((a, b) => a - b);
    } else {
        delete selectedEpisodesPerSeason[selectionKey];
    }

    console.log('📦 selectedEpisodesPerSeason after save:', JSON.stringify(selectedEpisodesPerSeason));
}

// Update download summary
function updateDownloadSummary() {
    let container = document.getElementById('downloadSummary');
    if (!container) {
        container = document.createElement('div');
        container.id = 'downloadSummary';
        container.className = 'download-summary';
        document.getElementById('episodeSelector').appendChild(container);
    }

    // Calculate total
    let totalEpisodes = 0;
    let seasonsCount = 0;
    const summaryParts = [];

    Object.keys(selectedEpisodesPerSeason).forEach(seasonNum => {
        const episodes = selectedEpisodesPerSeason[seasonNum];
        if (episodes && episodes.length > 0) {
            totalEpisodes += episodes.length;
            seasonsCount++;
            summaryParts.push(`S${seasonNum}: ${episodes.length} Ep.`);
        }
    });

    if (totalEpisodes > 0) {
        container.innerHTML = `
            <div class="summary-header">📊 Selection</div>
            <div class="summary-content">
                <div class="summary-total">${totalEpisodes} Episode(s) from ${seasonsCount} Season(s)</div>
                <div class="summary-details">${summaryParts.join(' • ')}</div>
            </div>
        `;
        container.style.display = 'block';
    } else {
        container.style.display = 'none';
    }
}

// Convert selection to download format
function getSelectionForDownload() {
    // Build seasons string
    const selectedSeasons = Object.keys(selectedEpisodesPerSeason)
        .filter(s => selectedEpisodesPerSeason[s].length > 0)
        .map(s => parseInt(s))
        .sort((a, b) => a - b);

    let seasonsString = '';
    if (selectedSeasons.length > 0) {
        seasonsString = episodesToRangeString(selectedSeasons);
    }

    // For episodes, we need to handle per-season
    // Store in a format the backend can understand
    return {
        seasonsString: seasonsString,
        episodesPerSeason: selectedEpisodesPerSeason
    };
}

// ========================================
// QUEUE MANAGEMENT FUNCTIONS
// ========================================

let queuePollInterval = null;

// Start queue polling after download start
function startQueuePolling() {
    if (queuePollInterval) {
        clearInterval(queuePollInterval);
    }
    queuePollInterval = setInterval(updateQueueStatus, 2000);
    showDownloadsSection();
    updateQueueStatus(); // Immediate first call
}

// Update queue status from API
async function updateQueueStatus() {
    try {
        console.log('🔍 Fetching queue status from /api/queue...');
        const response = await fetch('/api/queue');
        const data = await response.json();
        console.log('📥 Queue status received:', data);
        console.log(`   Total: ${data.total}, Queued: ${data.queued}, Processing: ${data.processing}, Completed: ${data.completed}`);
        renderQueue(data);

        // Check for duplicate series entries
        checkForDuplicates();
    } catch (error) {
        console.error('❌ Error fetching queue:', error);
    }
}

// Render unified downloads section (Status + Queue merged)
// Now shows individual episode cards instead of grouped series
// Track current queue state for differential updates
let currentQueueState = new Map(); // sessionId -> item data hash

function renderQueue(queueData) {
    // Calculate overall statistics
    const totalEpisodes = queueData.items.reduce((sum, item) => sum + (item.total_episodes || 0), 0);
    const completedEpisodes = queueData.items.reduce((sum, item) => sum + (item.completed_episodes || 0), 0);
    const totalProgress = totalEpisodes > 0 ? Math.round((completedEpisodes / totalEpisodes) * 100) : 0;
    const failedEpisodes = queueData.items.reduce((sum, item) => sum + (item.failed_episodes_count || 0), 0);

    // Update overall statistics
    document.getElementById('totalProgressText').textContent = `${completedEpisodes} / ${totalEpisodes}`;
    document.getElementById('totalProgressFill').style.width = `${totalProgress}%`;
    document.getElementById('totalProgressPercent').textContent = `${totalProgress}%`;
    document.getElementById('totalSuccessCount').textContent = completedEpisodes;
    document.getElementById('totalFailedCount').textContent = failedEpisodes;

    const container = document.getElementById('downloadsContainer');

    if (!queueData.items || queueData.items.length === 0) {
        container.innerHTML = '<p class="downloads-empty">No active downloads</p>';
        currentQueueState.clear();
        return;
    }

    // Check if we need a full re-render or can do incremental updates
    const existingSessionIds = new Set(
        Array.from(container.querySelectorAll('.download-series-container'))
            .map(el => el.dataset.session)
    );
    const newSessionIds = new Set(queueData.items.map(item => item.session_id.substring(0, 8)));

    // Check if structure changed (items added/removed)
    const structureChanged = existingSessionIds.size !== newSessionIds.size ||
        [...existingSessionIds].some(id => !newSessionIds.has(id)) ||
        [...newSessionIds].some(id => !existingSessionIds.has(id));

    if (structureChanged || existingSessionIds.size === 0) {
        // Full re-render needed
        renderQueueFull(queueData, container);
    } else {
        // Incremental update - only update changed elements
        updateQueueIncremental(queueData);
    }
}

// Full re-render (only when structure changes)
function renderQueueFull(queueData, container) {
    let html = '';

    queueData.items.forEach((item) => {
        const statusIcon = getStatusIcon(item.status);
        const shortSessionId = item.session_id.substring(0, 8);

        const failedButton = item.has_failed_episodes
            ? `<button class="btn-small btn-retry" onclick="retryFailed('${item.session_id}')">Wiederholen (${item.failed_episodes_count})</button>`
            : '';

        // Queue action buttons (Cancel for active, Remove for finished)
        let queueActionBtn = '';
        if (item.status === 'queued' || item.status === 'processing') {
            queueActionBtn = `<button class="btn-small btn-queue-cancel" onclick="cancelQueueItem('${item.session_id}', event)" title="Download abbrechen">✕</button>`;
        } else if (item.status === 'completed' || item.status === 'failed' || item.status === 'cancelled') {
            queueActionBtn = `<button class="btn-small btn-queue-remove" onclick="removeQueueItem('${item.session_id}', event)" title="Aus Liste entfernen">🗑</button>`;
        }

        const episodeStatus = item.episode_status || {};
        const hasEpisodes = Object.keys(episodeStatus).length > 0;

        html += `
            <div class="download-series-container status-${item.status}"
                 data-session="${shortSessionId}"
                 data-session-id="${item.session_id}">
                <div class="download-series-header" onclick="toggleSeriesCollapse('${shortSessionId}', event)">
                    <button class="series-toggle-btn" title="Ein-/Ausklappen">▼</button>
                    <div class="download-card-info">
                        <div class="download-series-name">${escapeHtml(item.series_name || 'Unknown Series')}</div>
                        <span class="download-episode-count">${item.completed_episodes} / ${item.total_episodes} Episodes</span>
                    </div>
                    <div class="download-card-status">
                        <span class="download-status-badge status-${item.status}">${statusIcon} ${item.status}</span>
                        ${failedButton}
                    </div>
                    <div class="download-card-actions">
                        ${queueActionBtn}
                    </div>
                </div>
                <div class="episode-list-container" id="episodes-${shortSessionId}">
                    ${hasEpisodes ? renderEpisodeCards(episodeStatus, shortSessionId, item.session_id) : '<div class="episodes-loading">Loading episodes...</div>'}
                </div>
            </div>
        `;

        // Store state for future comparisons
        currentQueueState.set(shortSessionId, JSON.stringify(item.episode_status || {}));
    });

    container.innerHTML = html;

    // Restore states after full re-render
    restoreCollapsedStates();
    initScrollListeners();
    clearDownloadCardCache();
    initEpisodeDragDrop();
}

// Incremental update - only update what changed
function updateQueueIncremental(queueData) {
    queueData.items.forEach((item) => {
        const shortSessionId = item.session_id.substring(0, 8);
        const container = document.querySelector(`.download-series-container[data-session="${shortSessionId}"]`);

        if (!container) return;

        // Update series status class
        container.className = container.className.replace(/status-\w+/g, '');
        container.classList.add(`status-${item.status}`);

        // Update episode count
        const countEl = container.querySelector('.download-episode-count');
        if (countEl) {
            countEl.textContent = `${item.completed_episodes} / ${item.total_episodes} Episodes`;
        }

        // Update status badge
        const badgeEl = container.querySelector('.download-status-badge');
        if (badgeEl) {
            badgeEl.className = `download-status-badge status-${item.status}`;
            badgeEl.innerHTML = `${getStatusIcon(item.status)} ${item.status}`;
        }

        // Update retry button
        const statusDiv = container.querySelector('.download-card-status');
        const existingRetryBtn = statusDiv?.querySelector('.btn-retry');
        if (item.has_failed_episodes) {
            if (existingRetryBtn) {
                existingRetryBtn.textContent = `Wiederholen (${item.failed_episodes_count})`;
            } else if (statusDiv) {
                const btn = document.createElement('button');
                btn.className = 'btn-small btn-retry';
                btn.onclick = () => retryFailed(item.session_id);
                btn.textContent = `Wiederholen (${item.failed_episodes_count})`;
                statusDiv.appendChild(btn);
            }
        } else if (existingRetryBtn) {
            existingRetryBtn.remove();
        }

        // Update queue action buttons (Cancel/Remove)
        const actionsDiv = container.querySelector('.download-card-actions');
        if (actionsDiv) {
            let newBtnHtml = '';
            if (item.status === 'queued' || item.status === 'processing') {
                newBtnHtml = `<button class="btn-small btn-queue-cancel" onclick="cancelQueueItem('${item.session_id}', event)" title="Download abbrechen">✕</button>`;
            } else if (item.status === 'completed' || item.status === 'failed' || item.status === 'cancelled') {
                newBtnHtml = `<button class="btn-small btn-queue-remove" onclick="removeQueueItem('${item.session_id}', event)" title="Aus Liste entfernen">🗑</button>`;
            }
            actionsDiv.innerHTML = newBtnHtml;
        }

        // Update individual episodes (only if changed)
        const episodeStatus = item.episode_status || {};
        const stateKey = JSON.stringify(episodeStatus);
        const previousState = currentQueueState.get(shortSessionId);

        if (stateKey !== previousState) {
            // Episodes changed - update them
            updateEpisodesIncremental(shortSessionId, item.session_id, episodeStatus);
            currentQueueState.set(shortSessionId, stateKey);
        }
    });
}

// Update individual episode rows without re-rendering
function updateEpisodesIncremental(shortSessionId, fullSessionId, episodeStatus) {
    const episodeContainer = document.getElementById(`episodes-${shortSessionId}`);
    if (!episodeContainer) return;

    const episodes = Object.entries(episodeStatus);

    // Check if we need to add new episode rows
    const existingRows = episodeContainer.querySelectorAll('.episode-row');
    const existingKeys = new Set(Array.from(existingRows).map(r => r.dataset.episodeKey));
    const newKeys = new Set(episodes.map(([key]) => key));

    // If structure changed, do full episode re-render
    if (existingKeys.size !== newKeys.size ||
        [...existingKeys].some(k => !newKeys.has(k))) {
        episodeContainer.innerHTML = renderEpisodeCards(episodeStatus, shortSessionId, fullSessionId);
        return;
    }

    // Update each episode row
    episodes.forEach(([episodeKey, status]) => {
        const row = episodeContainer.querySelector(`.episode-row[data-episode-key="${episodeKey}"]`);
        if (!row) return;

        const epStatus = status.status || 'queued';
        const progress = status.progress || 0;

        // Update status class
        row.className = row.className.replace(/status-\w+/g, '');
        row.classList.add(`status-${epStatus}`);

        // Update status icon
        const iconEl = row.querySelector('.episode-status-icon');
        if (iconEl) {
            iconEl.textContent = getEpisodeStatusIcon(epStatus);
        }

        // Update progress bar
        const progressFill = row.querySelector('.episode-progress-fill');
        if (progressFill) {
            progressFill.style.width = `${progress}%`;
        }

        // Update progress text
        const progressText = row.querySelector('.episode-progress-text');
        if (progressText) {
            progressText.textContent = `${Math.round(progress)}%`;
        }

        // Update action button
        updateEpisodeActionButton(row, epStatus, fullSessionId, episodeKey);
    });
}

// Update action button based on episode status
function updateEpisodeActionButton(row, epStatus, fullSessionId, episodeKey) {
    const existingBtn = row.querySelector('.episode-action-btn');
    const canStop = epStatus === 'downloading';
    const canCancel = epStatus === 'queued';
    const canRestart = ['stopped', 'failed', 'cancelled'].includes(epStatus);

    let newBtnHtml = '';
    if (canStop) {
        newBtnHtml = `<button class="episode-action-btn btn-stop" onclick="stopEpisode('${fullSessionId}', '${episodeKey}', event)" title="Stop episode">⏹</button>`;
    } else if (canCancel) {
        newBtnHtml = `<button class="episode-action-btn btn-cancel" onclick="cancelEpisode('${fullSessionId}', '${episodeKey}', event)" title="Cancel episode">✕</button>`;
    } else if (canRestart) {
        newBtnHtml = `<button class="episode-action-btn btn-restart" onclick="restartEpisode('${fullSessionId}', '${episodeKey}', event)" title="Restart episode">🔄</button>`;
    }

    // Only update if button changed
    if (existingBtn) {
        const needsUpdate =
            (canStop && !existingBtn.classList.contains('btn-stop')) ||
            (canCancel && !existingBtn.classList.contains('btn-cancel')) ||
            (canRestart && !existingBtn.classList.contains('btn-restart')) ||
            (!canStop && !canCancel && !canRestart);

        if (needsUpdate) {
            if (newBtnHtml) {
                existingBtn.outerHTML = newBtnHtml;
            } else {
                existingBtn.remove();
            }
        }
    } else if (newBtnHtml) {
        // Add new button
        const actionsDiv = row.querySelector('.episode-actions') || row;
        actionsDiv.insertAdjacentHTML('beforeend', newBtnHtml);
    }
}

// Render individual episode cards within a series (full horizontal layout)
function renderEpisodeCards(episodeStatus, shortSessionId, fullSessionId) {
    const episodes = Object.entries(episodeStatus);
    if (episodes.length === 0) return '';

    // Sort episodes by key (S01E01, S01E02, etc.)
    episodes.sort((a, b) => a[0].localeCompare(b[0]));

    return episodes.map(([episodeKey, status]) => {
        const epStatus = status.status || 'queued';
        const progress = status.progress || 0;
        const statusIcon = getEpisodeStatusIcon(epStatus);

        // Determine if episode can be stopped/cancelled/restarted
        const canStop = epStatus === 'downloading';
        const canCancel = epStatus === 'queued';
        const canRestart = ['stopped', 'failed', 'cancelled'].includes(epStatus);
        const isDraggable = epStatus === 'queued';

        // Action button based on status
        let actionButton = '';
        if (canStop) {
            actionButton = `<button class="episode-action-btn btn-stop" onclick="stopEpisode('${fullSessionId}', '${episodeKey}', event)" title="Stop episode">⏹</button>`;
        } else if (canCancel) {
            actionButton = `<button class="episode-action-btn btn-cancel" onclick="cancelEpisode('${fullSessionId}', '${episodeKey}', event)" title="Cancel episode">✕</button>`;
        } else if (canRestart) {
            actionButton = `<button class="episode-action-btn btn-restart" onclick="restartEpisode('${fullSessionId}', '${episodeKey}', event)" title="Restart episode">🔄</button>`;
        }

        // Drag handle for queued episodes
        const dragHandle = isDraggable ? '<span class="episode-drag-handle" title="Ziehen zum Neuordnen">⋮⋮</span>' : '';

        return `
            <div class="episode-row episode-status-${epStatus}"
                 data-episode="${episodeKey}"
                 data-session="${shortSessionId}"
                 ${isDraggable ? 'draggable="true"' : ''}>
                ${dragHandle}
                <div class="episode-info">
                    <span class="episode-key">${episodeKey}</span>
                    <span class="episode-status-icon">${statusIcon}</span>
                </div>
                <div class="episode-progress">
                    <div class="episode-progress-bar">
                        <div class="episode-progress-fill" style="width: ${progress}%"></div>
                    </div>
                    <span class="episode-progress-text">${progress.toFixed(0)}%</span>
                </div>
                <div class="episode-actions">
                    ${actionButton}
                </div>
            </div>
        `;
    }).join('');
}

// Get icon for episode status
function getEpisodeStatusIcon(status) {
    switch (status) {
        case 'queued': return '⏳';
        case 'downloading': return '⬇️';
        case 'completed': return '✅';
        case 'failed': return '❌';
        case 'stopped': return '⏹️';
        case 'cancelled': return '🚫';
        default: return '⏳';
    }
}

// Stop a specific episode download
async function stopEpisode(sessionId, episodeKey, event) {
    if (event) event.stopPropagation();

    try {
        const response = await fetch(`/api/queue/${sessionId}/episode/${episodeKey}/stop`, {
            method: 'POST'
        });

        if (response.ok) {
            showToast(`Episode ${episodeKey} stopping...`, 'info');
        } else {
            showToast(`Error stopping ${episodeKey}`, 'error');
        }
    } catch (error) {
        console.error('Error stopping episode:', error);
        showToast('Error stopping episode', 'error');
    }
}

// Cancel a specific queued episode
async function cancelEpisode(sessionId, episodeKey, event) {
    if (event) event.stopPropagation();

    try {
        const response = await fetch(`/api/queue/${sessionId}/episode/${episodeKey}/cancel`, {
            method: 'POST'
        });

        if (response.ok) {
            showToast(`Episode ${episodeKey} cancelled`, 'success');
            updateQueueStatus();
        } else {
            showToast(`Error cancelling ${episodeKey}`, 'error');
        }
    } catch (error) {
        console.error('Error cancelling episode:', error);
        showToast('Error cancelling episode', 'error');
    }
}

// Restart a stopped/failed/cancelled episode
async function restartEpisode(sessionId, episodeKey, event) {
    if (event) event.stopPropagation();

    try {
        const response = await fetch(`/api/queue/${sessionId}/episode/${episodeKey}/restart`, {
            method: 'POST'
        });

        if (response.ok) {
            showToast(`Episode ${episodeKey} restarting...`, 'success');
            updateQueueStatus();
        } else {
            const data = await response.json();
            showToast(data.error || `Error restarting ${episodeKey}`, 'error');
        }
    } catch (error) {
        console.error('Error restarting episode:', error);
        showToast('Error restarting episode', 'error');
    }
}

// Update episode count in series header (replaces progress bar)
function updateQueueItemProgress(sessionId, current, total) {
    const card = findDownloadCard(sessionId);
    if (!card) return;

    // Update episode count text
    const countText = card.querySelector('.download-episode-count');
    if (countText) {
        countText.textContent = `${current} / ${total} Episodes`;
    }

    // Schedule debounced statistics update
    scheduleStatisticsUpdate();
}

// Update download card with download percentage (FFmpeg progress)
function updateQueueItemDownloadProgress(sessionId, percent) {
    // Use cached card lookup for better performance
    const card = findDownloadCard(sessionId);
    if (!card) return;

    const progressText = card.querySelector('.download-progress-text');
    if (!progressText) return;

    const currentText = progressText.textContent;
    const baseText = currentText.split(' - ')[0]; // Keep episode count

    // Check if multiple episodes are downloading (total > 1)
    const match = baseText.match(/(\d+)\s*\/\s*(\d+)/);
    if (match) {
        const total = parseInt(match[2]);
        if (total > 1) {
            // Multiple episodes - DON'T update text here!
            // Let updateQueueItemAggregatedProgress handle parallel downloads
            // to avoid flickering from competing updates
            return;
        }
    }

    // Single episode - show exact percentage in text
    progressText.textContent = `${baseText} - Downloading ${percent.toFixed(1)}%`;

    // IMPORTANT: Also update the progress bar fill for single episodes!
    const progressFill = card.querySelector('.download-series-progress .download-progress-fill') ||
                         card.querySelector('.download-progress-fill');
    if (progressFill) {
        progressFill.style.width = `${Math.min(100, percent)}%`;
    }
}

// Update queue item with aggregated progress (averaged across parallel downloads)
function updateQueueItemAggregatedProgress(data) {
    const { session_id, total_percent, completed_episodes, total_episodes, active_downloads } = data;

    // Use cached card lookup for better performance
    const card = findDownloadCard(session_id);
    if (!card) return;

    // Update series-level progress bar with smooth averaged progress
    const progressFill = card.querySelector('.download-series-progress .download-progress-fill') ||
                         card.querySelector('.download-progress-fill');
    if (progressFill) {
        progressFill.style.width = `${Math.min(100, total_percent)}%`;
    }

    // Update series-level progress text
    const progressText = card.querySelector('.download-series-progress .download-progress-text') ||
                         card.querySelector('.download-progress-text');
    if (progressText) {
        if (active_downloads > 0) {
            // Show completed/total with averaged percentage and active count
            progressText.textContent = `${completed_episodes} / ${total_episodes} Episodes - ${total_percent.toFixed(1)}% (${active_downloads} active)`;
        } else {
            // No active downloads - show just the count
            progressText.textContent = `${completed_episodes} / ${total_episodes} Episodes`;
        }
    }
}

// Update overall statistics with aggregated progress data
function updateOverallAggregatedProgress() {
    // Get all processing downloads to calculate overall progress (support both old and new structure)
    const containers = document.querySelectorAll('.download-series-container.status-processing, .download-card.status-processing');
    if (containers.length === 0) return;

    let overallCompleted = 0;
    let overallTotal = 0;
    let overallProgressSum = 0;

    containers.forEach(container => {
        const progressText = container.querySelector('.download-series-progress .download-progress-text') ||
                            container.querySelector('.download-progress-text');

        if (progressText) {
            const match = progressText.textContent.match(/(\d+)\s*\/\s*(\d+)/);
            if (match) {
                const cardCompleted = parseInt(match[1]);
                const cardTotal = parseInt(match[2]);
                overallCompleted += cardCompleted;
                overallTotal += cardTotal;

                // Extract percentage if available
                const percentMatch = progressText.textContent.match(/(\d+\.?\d*)%/);
                if (percentMatch) {
                    overallProgressSum += parseFloat(percentMatch[1]) * cardTotal / 100;
                } else {
                    overallProgressSum += cardCompleted;
                }
            }
        }
    });

    if (overallTotal > 0) {
        // Calculate smooth overall progress
        const overallProgress = Math.round((overallProgressSum / overallTotal) * 100);

        document.getElementById('totalProgressText').textContent = `${overallCompleted} / ${overallTotal}`;
        document.getElementById('totalProgressFill').style.width = `${overallProgress}%`;
        document.getElementById('totalProgressPercent').textContent = `${overallProgress}%`;
        document.getElementById('totalSuccessCount').textContent = overallCompleted;
    }
}

// Update overall statistics (called when individual cards update)
function updateOverallStatistics() {
    // Support both old and new structure
    const containers = document.querySelectorAll('.download-series-container, .download-card');
    let totalEpisodes = 0;
    let completedEpisodes = 0;

    containers.forEach(container => {
        const progressText = container.querySelector('.download-series-progress .download-progress-text') ||
                            container.querySelector('.download-progress-text');
        if (progressText) {
            const match = progressText.textContent.match(/(\d+)\s*\/\s*(\d+)/);
            if (match) {
                completedEpisodes += parseInt(match[1]);
                totalEpisodes += parseInt(match[2]);
            }
        }
    });

    if (totalEpisodes > 0) {
        const totalProgress = Math.round((completedEpisodes / totalEpisodes) * 100);
        document.getElementById('totalProgressText').textContent = `${completedEpisodes} / ${totalEpisodes}`;
        document.getElementById('totalProgressFill').style.width = `${totalProgress}%`;
        document.getElementById('totalProgressPercent').textContent = `${totalProgress}%`;
    }
}

// Retry failed episodes
async function retryFailed(sessionId) {
    const confirmed = await showConfirm(
        'Do you want to retry the failed episodes?',
        'Retry Failed Episodes',
        'Yes, retry',
        'Cancel'
    );
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/retry/${sessionId}`, { method: 'POST' });
        const data = await response.json();

        if (response.ok) {
            showToast(`${data.retrying_episodes} episode(s) added to queue`, 'success', 5000);
            updateQueueStatus();
        } else {
            showToast(data.error, 'error', 5000);
        }
    } catch (error) {
        console.error('Error retrying failed episodes:', error);
        showToast('Error retrying failed episodes', 'error', 5000);
    }
}

// Cancel a queue item (for queued/processing items)
async function cancelQueueItem(sessionId, event) {
    event.stopPropagation(); // Prevent collapse toggle

    const confirmed = await showConfirm(
        'Möchtest du diesen Download wirklich abbrechen?',
        'Download abbrechen',
        'Ja, abbrechen',
        'Nein'
    );
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/queue/${sessionId}`, { method: 'DELETE' });
        const data = await response.json();

        if (response.ok) {
            showToast('Download wurde abgebrochen', 'success', 3000);
            updateQueueStatus();
        } else {
            showToast(data.error || 'Fehler beim Abbrechen', 'error', 5000);
        }
    } catch (error) {
        console.error('Error cancelling queue item:', error);
        showToast('Fehler beim Abbrechen des Downloads', 'error', 5000);
    }
}

// Remove a queue item completely (for completed/failed/cancelled items)
async function removeQueueItem(sessionId, event) {
    event.stopPropagation(); // Prevent collapse toggle

    const confirmed = await showConfirm(
        'Möchtest du diesen Eintrag aus der Liste entfernen?',
        'Eintrag entfernen',
        'Ja, entfernen',
        'Nein'
    );
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/queue/${sessionId}/remove`, { method: 'DELETE' });
        const data = await response.json();

        if (response.ok) {
            showToast('Eintrag wurde entfernt', 'success', 3000);
            updateQueueStatus();
        } else {
            showToast(data.error || 'Fehler beim Entfernen', 'error', 5000);
        }
    } catch (error) {
        console.error('Error removing queue item:', error);
        showToast('Fehler beim Entfernen des Eintrags', 'error', 5000);
    }
}

// ==========================================
// Drag & Drop Reordering
// ==========================================

let draggedCard = null;
let draggedSessionId = null;

// Initialize drag and drop handlers
function initDragAndDrop() {
    const container = document.getElementById('downloadsContainer');
    if (!container) return;

    // Use event delegation for better performance
    container.addEventListener('dragstart', handleDragStart);
    container.addEventListener('dragend', handleDragEnd);
    container.addEventListener('dragover', handleDragOver);
    container.addEventListener('dragleave', handleDragLeave);
    container.addEventListener('drop', handleDrop);
}

function handleDragStart(e) {
    const card = e.target.closest('.download-card');
    if (!card) return;

    // Only allow dragging queued items
    if (!card.classList.contains('status-queued')) {
        e.preventDefault();
        return;
    }

    draggedCard = card;
    const sessionSpan = card.querySelector('.download-session-id');
    if (sessionSpan) {
        const match = sessionSpan.textContent.match(/Session: ([a-f0-9]+)\.\.\./);
        if (match) {
            draggedSessionId = match[1];
        }
    }

    card.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', draggedSessionId || '');

    // Add visual feedback after a short delay
    setTimeout(() => {
        card.style.opacity = '0.5';
    }, 0);
}

function handleDragEnd(e) {
    if (draggedCard) {
        draggedCard.classList.remove('dragging');
        draggedCard.style.opacity = '';
        draggedCard = null;
        draggedSessionId = null;
    }

    // Remove all drag-over indicators
    document.querySelectorAll('.download-card.drag-over').forEach(c => {
        c.classList.remove('drag-over');
    });
}

function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';

    const card = e.target.closest('.download-card');
    if (card && card !== draggedCard && card.classList.contains('status-queued')) {
        // Remove drag-over from other cards
        document.querySelectorAll('.download-card.drag-over').forEach(c => {
            if (c !== card) c.classList.remove('drag-over');
        });
        card.classList.add('drag-over');
    }
}

function handleDragLeave(e) {
    const card = e.target.closest('.download-card');
    if (card && !card.contains(e.relatedTarget)) {
        card.classList.remove('drag-over');
    }
}

async function handleDrop(e) {
    e.preventDefault();

    const targetCard = e.target.closest('.download-card');
    if (!targetCard || targetCard === draggedCard || !targetCard.classList.contains('status-queued')) {
        return;
    }

    targetCard.classList.remove('drag-over');

    // Get all queued cards in current order
    const container = document.getElementById('downloadsContainer');
    const cards = Array.from(container.querySelectorAll('.download-card.status-queued'));

    // Build new order by moving dragged card to target position
    const newOrder = [];
    const draggedIndex = cards.indexOf(draggedCard);
    const targetIndex = cards.indexOf(targetCard);

    cards.forEach((card, index) => {
        if (card === draggedCard) return; // Skip dragged card

        const sessionSpan = card.querySelector('.download-session-id');
        if (sessionSpan) {
            const match = sessionSpan.textContent.match(/Session: ([a-f0-9]+)\.\.\./);
            if (match) {
                // Insert dragged card before or after target based on drag direction
                if (index === targetIndex) {
                    if (draggedIndex < targetIndex) {
                        // Dragging down: insert after target
                        newOrder.push(match[1]);
                        newOrder.push(draggedSessionId);
                    } else {
                        // Dragging up: insert before target
                        newOrder.push(draggedSessionId);
                        newOrder.push(match[1]);
                    }
                } else {
                    newOrder.push(match[1]);
                }
            }
        }
    });

    // If dragged card wasn't inserted yet (edge case), add it at the end
    if (!newOrder.includes(draggedSessionId)) {
        newOrder.push(draggedSessionId);
    }

    // Send reorder request to server
    try {
        const response = await fetch('/api/queue/reorder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order: newOrder })
        });

        if (response.ok) {
            showToast('Order updated', 'success', 2000);
            updateQueueStatus(); // Refresh the queue display
        } else {
            showToast('Error reordering', 'error', 3000);
        }
    } catch (error) {
        console.error('Error reordering queue:', error);
        showToast('Error reordering', 'error', 3000);
    }
}

// ==========================================
// Episode Drag & Drop
// ==========================================

let draggedEpisode = null;
let draggedEpisodeSession = null;

// Initialize episode drag and drop handlers (called after rendering episodes)
function initEpisodeDragDrop() {
    const containers = document.querySelectorAll('.episode-list-container');
    containers.forEach(container => {
        // Remove old listeners to avoid duplicates
        container.removeEventListener('dragstart', handleEpisodeDragStart);
        container.removeEventListener('dragend', handleEpisodeDragEnd);
        container.removeEventListener('dragover', handleEpisodeDragOver);
        container.removeEventListener('dragleave', handleEpisodeDragLeave);
        container.removeEventListener('drop', handleEpisodeDrop);

        // Add new listeners
        container.addEventListener('dragstart', handleEpisodeDragStart);
        container.addEventListener('dragend', handleEpisodeDragEnd);
        container.addEventListener('dragover', handleEpisodeDragOver);
        container.addEventListener('dragleave', handleEpisodeDragLeave);
        container.addEventListener('drop', handleEpisodeDrop);
    });
}

function handleEpisodeDragStart(e) {
    const row = e.target.closest('.episode-row');
    if (!row) return;

    // Only allow dragging queued episodes
    if (!row.classList.contains('episode-status-queued')) {
        e.preventDefault();
        return;
    }

    draggedEpisode = row;
    draggedEpisodeSession = row.dataset.session;

    row.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', row.dataset.episode || '');

    setTimeout(() => {
        row.style.opacity = '0.5';
    }, 0);
}

function handleEpisodeDragEnd(e) {
    if (draggedEpisode) {
        draggedEpisode.classList.remove('dragging');
        draggedEpisode.style.opacity = '';
        draggedEpisode = null;
        draggedEpisodeSession = null;
    }

    // Remove all drag-over indicators
    document.querySelectorAll('.episode-row.drag-over').forEach(r => {
        r.classList.remove('drag-over');
    });
}

function handleEpisodeDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';

    const row = e.target.closest('.episode-row');
    if (row && row !== draggedEpisode && row.classList.contains('episode-status-queued')) {
        // Only allow reordering within the same session
        if (row.dataset.session === draggedEpisodeSession) {
            document.querySelectorAll('.episode-row.drag-over').forEach(r => {
                if (r !== row) r.classList.remove('drag-over');
            });
            row.classList.add('drag-over');
        }
    }
}

function handleEpisodeDragLeave(e) {
    const row = e.target.closest('.episode-row');
    if (row && !row.contains(e.relatedTarget)) {
        row.classList.remove('drag-over');
    }
}

async function handleEpisodeDrop(e) {
    e.preventDefault();

    const targetRow = e.target.closest('.episode-row');
    if (!targetRow || targetRow === draggedEpisode || !targetRow.classList.contains('episode-status-queued')) {
        return;
    }

    // Only allow reordering within the same session
    if (targetRow.dataset.session !== draggedEpisodeSession) {
        return;
    }

    targetRow.classList.remove('drag-over');

    // Get the episode list container
    const container = targetRow.closest('.episode-list-container');
    if (!container) return;

    // Get all episode rows in current order
    const rows = Array.from(container.querySelectorAll('.episode-row'));

    // Build new order by extracting episode keys
    const draggedIndex = rows.indexOf(draggedEpisode);
    const targetIndex = rows.indexOf(targetRow);

    // Reorder: move dragged to target position
    const newOrder = [];
    rows.forEach((row, index) => {
        if (index === draggedIndex) return; // Skip dragged
        if (index === targetIndex) {
            // Insert dragged before or after target depending on direction
            if (draggedIndex < targetIndex) {
                newOrder.push(row.dataset.episode);
                newOrder.push(draggedEpisode.dataset.episode);
            } else {
                newOrder.push(draggedEpisode.dataset.episode);
                newOrder.push(row.dataset.episode);
            }
        } else {
            newOrder.push(row.dataset.episode);
        }
    });

    // Get full session ID from the series container
    const seriesContainer = container.closest('.download-series-container');
    const fullSessionId = seriesContainer?.dataset.sessionId;

    if (!fullSessionId) {
        console.error('Could not find session ID for episode reorder');
        return;
    }

    // Send reorder request to backend
    try {
        const response = await fetch(`/api/queue/${fullSessionId}/episodes/reorder`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order: newOrder })
        });

        if (response.ok) {
            showToast('Episode order updated', 'success', 2000);
            updateQueueStatus(); // Refresh to show new order
        } else {
            showToast('Error reordering episodes', 'error', 3000);
        }
    } catch (error) {
        console.error('Error reordering episodes:', error);
        showToast('Error reordering episodes', 'error', 3000);
    }
}

// Initialize drag and drop when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    initDragAndDrop();
});

// Cancel queued download
async function cancelQueued(sessionId) {
    const confirmed = await showConfirm(
        'Remove download from queue?',
        'Remove Download',
        'Yes, remove',
        'Cancel'
    );
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/queue/${sessionId}`, { method: 'DELETE' });
        if (response.ok) {
            updateQueueStatus();
        }
    } catch (error) {
        console.error('Error cancelling download:', error);
    }
}

// Cancel processing download (stop running download)
async function cancelProcessing(sessionId) {
    const confirmed = await showConfirm(
        'Really cancel running download?\n\nAlready downloaded episodes will be kept.',
        'Stop Download',
        'Yes, stop',
        'Cancel'
    );
    if (!confirmed) return;

    try {
        const response = await fetch(`/api/cancel/${sessionId}`, { method: 'POST' });
        if (response.ok) {
            showToast('Download stopping...', 'warning', 3000);
            updateQueueStatus();
        } else {
            showToast('Error stopping download', 'error', 5000);
        }
    } catch (error) {
        console.error('Error cancelling processing download:', error);
        showToast('Error stopping download', 'error', 5000);
    }
}

// Refresh queue manually
function refreshQueue() {
    updateQueueStatus();
}

// Clear old queue items
async function clearOldQueue() {
    const confirmed = await showConfirm(
        'Remove all completed downloads from the queue?\n\nOnly active downloads (queued or processing) will be kept.',
        'Clear Queue',
        'Yes, clear',
        'Cancel'
    );
    if (!confirmed) return;

    try {
        const response = await fetch('/api/queue/clear', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keep_recent: 0 })
        });
        const data = await response.json();

        if (response.ok) {
            showToast(data.message || 'Queue cleared', 'success', 4000);
            updateQueueStatus();
        }
    } catch (error) {
        console.error('Error clearing queue:', error);
        showToast('Error clearing queue', 'error', 4000);
    }
}

// Store collapsed state and scroll positions for series containers
const collapsedSeries = new Set();
const scrollPositions = new Map(); // sessionId -> scrollTop

// Initialize scroll event listeners (called after render)
function initScrollListeners() {
    document.querySelectorAll('.episode-list-container').forEach(container => {
        // Remove old listener to avoid duplicates
        container.removeEventListener('scroll', handleEpisodeScroll);
        // Add new listener
        container.addEventListener('scroll', handleEpisodeScroll);
    });
}

// Handle scroll event - save position on scroll
function handleEpisodeScroll(event) {
    const container = event.target;
    const parent = container.closest('.download-series-container');
    if (parent && parent.dataset.session) {
        scrollPositions.set(parent.dataset.session, container.scrollTop);
    }
}

// Toggle series container collapse state
function toggleSeriesCollapse(sessionId, event) {
    // Don't toggle if clicking on buttons or checkboxes
    if (event.target.closest('button:not(.series-toggle-btn)') ||
        event.target.closest('input[type="checkbox"]') ||
        event.target.closest('.btn-retry')) {
        return;
    }

    const container = document.querySelector(`.download-series-container[data-session="${sessionId}"]`);
    if (container) {
        container.classList.toggle('collapsed');

        // Save state
        if (container.classList.contains('collapsed')) {
            collapsedSeries.add(sessionId);
        } else {
            collapsedSeries.delete(sessionId);
        }
    }
}

// Restore collapsed states and scroll positions after re-render
function restoreCollapsedStates() {
    // Restore collapsed states
    collapsedSeries.forEach(sessionId => {
        const container = document.querySelector(`.download-series-container[data-session="${sessionId}"]`);
        if (container) {
            container.classList.add('collapsed');
        }
    });

    // Restore scroll positions
    scrollPositions.forEach((scrollTop, sessionId) => {
        const container = document.querySelector(`.download-series-container[data-session="${sessionId}"] .episode-list-container`);
        if (container) {
            container.scrollTop = scrollTop;
        }
    });
}

// Collapse all series containers
function collapseAllSeries() {
    document.querySelectorAll('.download-series-container').forEach(c => {
        c.classList.add('collapsed');
        const sessionId = c.dataset.session;
        if (sessionId) collapsedSeries.add(sessionId);
    });
}

// Expand all series containers
function expandAllSeries() {
    document.querySelectorAll('.download-series-container').forEach(c => {
        c.classList.remove('collapsed');
    });
    collapsedSeries.clear();
}

// Check for duplicate series and show/hide consolidate button
async function checkForDuplicates() {
    try {
        const response = await fetch('/api/queue/duplicates');
        const data = await response.json();

        const btn = document.getElementById('consolidateDuplicatesBtn');
        if (btn) {
            if (data.count > 0) {
                btn.style.display = '';
                btn.textContent = `🔗 Merge (${data.count})`;
                btn.title = `${data.count} series with multiple entries found`;
            } else {
                btn.style.display = 'none';
            }
        }
    } catch (error) {
        console.error('Error checking for duplicates:', error);
    }
}

// Consolidate all duplicate series entries
async function consolidateAllDuplicates() {
    const confirmed = await showConfirm(
        'Merge all duplicate series entries?\n\nEpisodes from the same series will be consolidated into one entry.',
        'Merge Series',
        'Yes, merge',
        'Cancel'
    );
    if (!confirmed) return;

    try {
        const response = await fetch('/api/queue/consolidate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ consolidate_all: true })
        });
        const data = await response.json();

        if (response.ok) {
            if (data.series_consolidated > 0) {
                showToast(`${data.series_consolidated} series merged, ${data.total_entries_merged} entries removed`, 'success', 5000);
                addLog(`🔗 ${data.series_consolidated} series merged`, 'success');
            } else {
                showToast('No duplicates found', 'info', 3000);
            }
            updateQueueStatus();
        } else {
            showToast(data.error || 'Error merging', 'error', 4000);
        }
    } catch (error) {
        console.error('Error consolidating duplicates:', error);
        showToast('Error merging series', 'error', 4000);
    }
}

// Get status icon for queue item
function getStatusIcon(status) {
    const icons = {
        'queued': '⏳',
        'processing': '▶️',
        'completed': '✅',
        'failed': '❌',
        'cancelled': '🚫'
    };
    return icons[status] || '❓';
}

// Format time for display
function formatTime(isoString) {
    if (!isoString) return '-';
    const date = new Date(isoString);
    return date.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
}

// Show downloads section
function showDownloadsSection() {
    const downloadsSection = document.getElementById('downloadsSection');
    if (downloadsSection) downloadsSection.style.display = 'block';
}

// ==========================================
// Smart Selection Functions
// ==========================================

function selectEveryNthEpisode(seasonNum, episodes, n, offset = 0) {
    const selected = [];
    for (let i = offset; i < episodes.length; i += n) {
        selected.push(episodes[i]);
    }

    selectedEpisodesPerSeason[seasonNum] = selected;
    showEpisodesForSeason(seasonNum); // Refresh display
    updateDownloadSummary();
}

function selectFirstHalf(seasonNum, episodes) {
    const midpoint = Math.ceil(episodes.length / 2);
    selectedEpisodesPerSeason[seasonNum] = episodes.slice(0, midpoint);
    showEpisodesForSeason(seasonNum);
    updateDownloadSummary();
}

function selectSecondHalf(seasonNum, episodes) {
    const midpoint = Math.ceil(episodes.length / 2);
    selectedEpisodesPerSeason[seasonNum] = episodes.slice(midpoint);
    showEpisodesForSeason(seasonNum);
    updateDownloadSummary();
}

// ==========================================
// Pattern Modal Functions
// ==========================================

function showPatternModal(seasonNum, availableEpisodes) {
    // Create modal overlay
    const overlay = document.createElement('div');
    overlay.className = 'pattern-modal-overlay';
    overlay.onclick = () => closePatternModal();

    // Create modal
    const modal = document.createElement('div');
    modal.className = 'pattern-modal';
    modal.id = 'patternModal';

    modal.innerHTML = `
        <div class="pattern-modal-header">
            <h3>📋 Custom Episode Selection</h3>
            <button class="btn-icon modal-close" onclick="closePatternModal()">✕</button>
        </div>

        <div class="pattern-modal-body">
            <label for="patternInput">Enter episode pattern:</label>
            <input
                type="text"
                id="patternInput"
                class="form-control"
                placeholder="z.B. 1,3,5-7,10-15,20"
                autocomplete="off"
            />

            <div class="pattern-input-help">
                <strong>Syntax:</strong>
                <ul>
                    <li><code>1,2,3</code> - Individual episodes</li>
                    <li><code>5-10</code> - Range (episode 5 to 10)</li>
                    <li><code>1,3,5-7,10-15</code> - Combined</li>
                </ul>
            </div>

            <div class="pattern-preview" id="patternPreview"></div>
        </div>

        <div class="pattern-modal-footer">
            <button class="btn btn-secondary" onclick="closePatternModal()">Cancel</button>
            <button class="btn btn-primary" id="applyPatternBtn">Apply</button>
        </div>
    `;

    document.body.appendChild(overlay);
    document.body.appendChild(modal);

    // Focus input
    setTimeout(() => document.getElementById('patternInput').focus(), 100);

    // Live preview
    document.getElementById('patternInput').addEventListener('input', (e) => {
        previewPattern(e.target.value, availableEpisodes);
    });

    // Apply button handler
    document.getElementById('applyPatternBtn').addEventListener('click', () => {
        applyPattern(seasonNum, availableEpisodes);
    });

    // Allow Enter key to apply
    document.getElementById('patternInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            applyPattern(seasonNum, availableEpisodes);
        }
    });
}

function parseEpisodePattern(patternString, availableEpisodes) {
    const episodes = new Set();
    const parts = patternString.split(',').map(p => p.trim());

    parts.forEach(part => {
        if (!part) return;

        if (part.includes('-')) {
            // Range: "5-10"
            const [start, end] = part.split('-').map(s => parseInt(s.trim()));
            if (!isNaN(start) && !isNaN(end)) {
                for (let i = start; i <= end; i++) {
                    if (availableEpisodes.includes(i)) {
                        episodes.add(i);
                    }
                }
            }
        } else {
            // Single episode: "3"
            const ep = parseInt(part);
            if (!isNaN(ep) && availableEpisodes.includes(ep)) {
                episodes.add(ep);
            }
        }
    });

    return Array.from(episodes).sort((a, b) => a - b);
}

function previewPattern(patternString, availableEpisodes) {
    const preview = document.getElementById('patternPreview');
    if (!patternString.trim()) {
        preview.innerHTML = '';
        return;
    }

    try {
        const selected = parseEpisodePattern(patternString, availableEpisodes);
        if (selected.length > 0) {
            preview.innerHTML = `
                <div class="preview-success">
                    ✅ ${selected.length} episode(s) will be selected:<br>
                    <strong>${selected.join(', ')}</strong>
                </div>
            `;
        } else {
            preview.innerHTML = '<div class="preview-warning">⚠️ No valid episodes found</div>';
        }
    } catch (error) {
        preview.innerHTML = '<div class="preview-error">❌ Invalid pattern</div>';
    }
}

function applyPattern(seasonNum, availableEpisodes) {
    const patternInput = document.getElementById('patternInput').value;
    const selected = parseEpisodePattern(patternInput, availableEpisodes);

    if (selected.length > 0) {
        selectedEpisodesPerSeason[seasonNum] = selected;
        showEpisodesForSeason(seasonNum);
        updateDownloadSummary();
        closePatternModal();
    } else {
        showToast('No valid episodes found in pattern!', 'warning', 5000);
    }
}

function closePatternModal() {
    document.querySelector('.pattern-modal-overlay')?.remove();
    document.getElementById('patternModal')?.remove();
}

// ==========================================
// Keyboard Shortcuts
// ==========================================

let keyboardHandlerAttached = false;

function handleEpisodeKeyboard(e) {
    // Ctrl+A: Select all in current season
    if (e.ctrlKey && e.key === 'a' && currentSelectedSeason) {
        const activeElement = document.activeElement;
        // Don't intercept if user is typing in an input field
        if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA')) {
            return;
        }

        e.preventDefault();
        const seasonData = availableSeasonsData.find(s => s.season === currentSelectedSeason);
        if (seasonData) {
            selectAllEpisodes(currentSelectedSeason, seasonData.episodes);
        }
    }

    // Escape: Close modal
    if (e.key === 'Escape') {
        closePatternModal();
    }
}

// Attach keyboard handler once
if (!keyboardHandlerAttached) {
    document.addEventListener('keydown', handleEpisodeKeyboard);
    keyboardHandlerAttached = true;
}

// ==========================================
// Series Catalog Functions
// ==========================================

let catalogData = null;
let filteredSeries = [];
let currentGenreFilter = 'all';
let currentSource = 'series'; // Track current source
let displayedCount = 0;
const SERIES_PER_PAGE = Infinity; // Load all series for horizontal slider

// Batch mode state
let batchModeActive = false;
let selectedSeries = new Set(); // Set of slugs

// Change source (when dropdown changes)
async function changeSource() {
    const sourceSelector = document.getElementById('sourceSelector');
    currentSource = sourceSelector.value;

    // Load catalog for new source (from cache if available)
    await loadCatalogOnStartup();
}

async function updateCatalog(forceRefresh = false) {
    const btn = document.getElementById('updateCatalogBtn');
    btn.disabled = true;
    btn.textContent = '⏳ Loading...';

    try {
        const response = await fetch(`/api/catalog?source=${currentSource}&force_refresh=${forceRefresh}`);
        const data = await response.json();

        if (data.error) {
            throw new Error(data.error);
        }

        catalogData = data;
        updateCatalogStats(data);
        renderGenreFilters(data.genres);
        applyFilters(); // Render all series

        btn.textContent = forceRefresh ? '✅ Updated' : '✅ Loaded';
        setTimeout(() => {
            btn.textContent = '🔄 Check for Updates';
            btn.disabled = false;
        }, 2000);

    } catch (error) {
        showToast('Error loading catalog: ' + error.message, 'error', 6000);
        btn.textContent = '❌ Error';
        btn.disabled = false;
    }
}

function updateCatalogStats(data) {
    document.getElementById('catalogTotalSeries').textContent =
        `${data.total_series} Series`;

    const lastUpdate = new Date(data.last_updated);
    document.getElementById('catalogLastUpdate').textContent =
        `Last updated: ${lastUpdate.toLocaleString()}`;
}

function renderGenreFilters(genres) {
    const container = document.getElementById('genreFilterTabs');
    container.innerHTML = '<button class="genre-tab active" data-genre="all" onclick="filterByGenre(\'all\')">All</button>';

    Object.keys(genres).sort().forEach(genre => {
        const count = genres[genre].length;
        const btn = document.createElement('button');
        btn.className = 'genre-tab';
        btn.dataset.genre = genre;
        btn.textContent = `${genre} (${count})`;
        btn.onclick = () => filterByGenre(genre);
        container.appendChild(btn);
    });
}

function toggleCatalogSection() {
    const content = document.getElementById('catalogContent');
    const icon = document.getElementById('catalogToggleIcon');

    if (content.style.display === 'none') {
        content.style.display = 'block';
        icon.textContent = '▼';
    } else {
        content.style.display = 'none';
        icon.textContent = '▶';
    }
}

// ==========================================
// Batch Mode Functions
// ==========================================

function toggleBatchMode() {
    batchModeActive = !batchModeActive;
    const btn = document.getElementById('batchModeBtn');
    const selectionBar = document.getElementById('batchSelectionBar');
    const grid = document.getElementById('seriesGrid');

    if (batchModeActive) {
        btn.classList.add('active');
        btn.textContent = '✅ Batch-Modus';
        selectionBar.style.display = 'flex';
        grid.classList.add('batch-mode');
    } else {
        btn.classList.remove('active');
        btn.textContent = '📦 Batch-Modus';
        selectionBar.style.display = 'none';
        grid.classList.remove('batch-mode');
        clearBatchSelection();
    }

    // Re-render cards with/without checkboxes
    renderSeriesGrid();
}

function toggleSeriesSelection(slug, checkbox) {
    if (checkbox.checked) {
        selectedSeries.add(slug);
    } else {
        selectedSeries.delete(slug);
    }
    updateBatchSelectionCount();
}

function updateBatchSelectionCount() {
    document.getElementById('batchSelectedCount').textContent =
        `${selectedSeries.size} selected`;
}

function clearBatchSelection() {
    selectedSeries.clear();
    updateBatchSelectionCount();

    // Uncheck all checkboxes
    document.querySelectorAll('.series-checkbox').forEach(cb => {
        cb.checked = false;
    });
}

async function addSelectedToQueue(event) {
    console.log('🔧 addSelectedToQueue() called');
    console.log(`   Selected series count: ${selectedSeries.size}`);

    if (selectedSeries.size === 0) {
        showToast('No series selected', 'warning');
        return;
    }

    const count = selectedSeries.size;
    console.log(`   Showing confirmation dialog for ${count} series...`);

    const confirmed = await showConfirm(
        `Add ${count} series to queue?`,
        'Add Series to Queue',
        'Yes, add',
        'Cancel'
    );

    if (!confirmed) {
        console.log('   User cancelled confirmation dialog');
        return;
    }

    console.log('✅ User confirmed, starting batch add...');

    const btn = event.target;
    btn.disabled = true;
    btn.textContent = '⏳ Adding...';

    try {
        // Add each selected series to queue
        let added = 0;
        let failed = 0;

        console.log(`📦 Processing ${selectedSeries.size} series...`);

        for (const slug of selectedSeries) {
            console.log(`\n🔄 Processing series: ${slug}`);

            try {
                // Get series details from catalog
                const seriesData = findSeriesBySlug(slug);
                if (!seriesData) {
                    console.error(`   ❌ Series data not found for slug: ${slug}`);
                    failed++;
                    continue;
                }

                console.log(`   ✓ Series data found: ${seriesData.name}`);

                // Construct URL
                const baseUrls = {
                    'series': 'http://186.2.175.5',
                    'anime': 'https://aniworld.to'
                };
                const contentPaths = {
                    'series': '/serie/stream/',
                    'anime': '/anime/stream/'
                };
                const source = seriesData.source || currentSource;
                const url = `${baseUrls[source]}${contentPaths[source]}${slug}`;

                console.log(`   📍 Constructed URL: ${url}`);
                console.log(`   📤 Sending POST request to /api/queue/add-series...`);

                // Add to queue via backend
                const requestBody = {
                    url: url,
                    series_name: seriesData.name,
                    slug: slug
                };

                console.log(`   Request body:`, requestBody);

                const response = await fetch('/api/queue/add-series', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(requestBody)
                });

                console.log(`   📥 Response status: ${response.status} ${response.statusText}`);

                if (response.ok) {
                    const responseData = await response.json();
                    console.log(`   ✅ Success! Session ID: ${responseData.session_id}`);
                    added++;
                } else {
                    const errorData = await response.json();
                    console.error(`   ❌ Failed! Error:`, errorData);
                    failed++;
                }
            } catch (error) {
                console.error(`   ❌ Exception while adding ${slug}:`, error);
                console.error(`   Stack trace:`, error.stack);
                failed++;
            }
        }

        console.log(`\n📊 Batch add complete:`);
        console.log(`   ✅ Added: ${added}`);
        console.log(`   ❌ Failed: ${failed}`);
        console.log(`   📍 Total: ${count}`);

        if (failed > 0) {
            showToast(`${added} of ${count} series added, ${failed} failed (see console)`, 'warning', 6000);
        } else {
            showToast(`${added} of ${count} series added to queue`, 'success', 5000);
        }
        clearBatchSelection();

        // Show downloads section
        console.log('🔍 Showing downloads section and updating status...');
        document.getElementById('downloadsSection').style.display = 'block';
        updateQueueStatus();

    } catch (error) {
        console.error('❌ CRITICAL ERROR in addSelectedToQueue():', error);
        console.error('Stack trace:', error.stack);
        showToast('Error adding to queue: ' + error.message, 'error', 6000);
    } finally {
        btn.disabled = false;
        btn.textContent = '➕ Add to Queue';
        console.log('🏁 addSelectedToQueue() finished\n');
    }
}

function findSeriesBySlug(slug) {
    if (!catalogData || !catalogData.genres) return null;

    for (const genreName in catalogData.genres) {
        const series = catalogData.genres[genreName].find(s => s.slug === slug);
        if (series) {
            return {...series, genre: genreName};
        }
    }
    return null;
}

function toggleGenreFilter() {
    const tabs = document.getElementById('genreFilterTabs');
    tabs.style.display = tabs.style.display === 'none' ? 'flex' : 'none';
}

function filterByGenre(genre) {
    currentGenreFilter = genre;

    // Update active tab
    document.querySelectorAll('.genre-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.genre === genre);
    });

    applyFilters();
}

function applyFilters() {
    if (!catalogData) return;

    const searchTerm = document.getElementById('catalogSearch').value.toLowerCase().trim();
    filteredSeries = [];

    // Flatten series from all genres
    Object.entries(catalogData.genres).forEach(([genre, seriesList]) => {
        // Filter by genre
        if (currentGenreFilter !== 'all' && genre !== currentGenreFilter) {
            return;
        }

        // Filter by search term
        seriesList.forEach(series => {
            const matchesSearch = !searchTerm ||
                series.name.toLowerCase().includes(searchTerm) ||
                (series.alternative_titles || '').toLowerCase().includes(searchTerm) ||
                genre.toLowerCase().includes(searchTerm);

            if (matchesSearch) {
                filteredSeries.push({...series, genre});
            }
        });
    });

    // Sort alphabetically
    filteredSeries.sort((a, b) => a.name.localeCompare(b.name));

    // Render with pagination
    displayedCount = 0;
    renderSeriesGrid();
}

function clearCatalogSearch() {
    document.getElementById('catalogSearch').value = '';
    applyFilters();
}

function debounce(func, wait) {
    let timeout;
    return function(...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
}

function renderSeriesGrid() {
    const grid = document.getElementById('seriesGrid');

    if (filteredSeries.length === 0) {
        grid.innerHTML = '<p class="catalog-empty">No series found</p>';
        return;
    }

    // Render all series (horizontal slider loads all at once)
    grid.innerHTML = '';
    for (let i = 0; i < filteredSeries.length; i++) {
        grid.appendChild(createSeriesCard(filteredSeries[i]));
    }

    displayedCount = filteredSeries.length;
}

// Scroll catalog horizontally
function scrollCatalog(direction) {
    const grid = document.getElementById('seriesGrid');
    if (!grid) return;

    // Calculate scroll amount (roughly 5 cards width)
    const cardWidth = 160 + 16; // card width + gap
    const scrollAmount = cardWidth * 5 * direction;

    grid.scrollBy({
        left: scrollAmount,
        behavior: 'smooth'
    });
}

function createSeriesCard(series) {
    const card = document.createElement('div');
    card.className = 'series-card';
    card.dataset.slug = series.slug;
    card.dataset.genre = series.genre;
    card.dataset.source = series.source || currentSource; // Store source

    // Get first letter for placeholder
    const initial = series.name.charAt(0).toUpperCase();

    // Checkbox for batch mode
    const checkboxHtml = batchModeActive ? `
        <div class="series-card-checkbox">
            <input type="checkbox"
                   class="series-checkbox"
                   id="cb-${series.slug}"
                   onchange="toggleSeriesSelection('${series.slug}', this)"
                   ${selectedSeries.has(series.slug) ? 'checked' : ''}>
            <label for="cb-${series.slug}"></label>
        </div>
    ` : '';

    card.innerHTML = `
        ${checkboxHtml}
        <div class="series-card-cover" data-lazy-cover="${series.slug}" data-source="${series.source || currentSource}">
            <div class="series-card-placeholder">
                <span class="series-initial">${initial}</span>
            </div>
        </div>

        <div class="series-card-info">
            <h3 class="series-card-title">${escapeHtml(series.name)}</h3>
            <p class="series-card-meta">
                <span class="genre-tag">${escapeHtml(series.genre)}</span>
            </p>
            <button class="btn-small btn-primary" onclick="selectSeries('${series.slug}', '${series.source || currentSource}')">
                Select
            </button>
        </div>
    `;

    // Setup lazy loading for cover image
    setupLazyCoverLoading(card);

    return card;
}

// Lazy load cover images using IntersectionObserver
const coverCache = {}; // Cache loaded covers
let coverObserver = null;

function setupLazyCoverLoading(card) {
    if (!coverObserver) {
        coverObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const coverDiv = entry.target;
                    const slug = coverDiv.dataset.lazyCover;

                    if (slug && !coverDiv.dataset.loaded) {
                        loadSeriesCover(slug, coverDiv);
                        coverObserver.unobserve(coverDiv);
                    }
                }
            });
        }, {
            rootMargin: '50px' // Start loading 50px before visible
        });
    }

    const coverDiv = card.querySelector('[data-lazy-cover]');
    if (coverDiv) {
        coverObserver.observe(coverDiv);
    }
}

async function loadSeriesCover(slug, coverDiv) {
    // Check cache first
    if (coverCache[slug]) {
        applyCoverImage(coverDiv, coverCache[slug]);
        return;
    }

    try {
        // Get source from data attribute
        const source = coverDiv.dataset.source || currentSource;

        // Construct URL based on source
        const baseUrls = {
            'series': 'http://186.2.175.5',
            'anime': 'https://aniworld.to'
        };
        const contentPaths = {
            'series': '/serie/stream/',
            'anime': '/anime/stream/'
        };

        const baseUrl = baseUrls[source];
        const contentPath = contentPaths[source];
        const url = `${baseUrl}${contentPath}${slug}`;

        // Try to get cover from series cache
        const cacheResponse = await fetch(`/api/parse-url`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                url: url,
                force_refresh: false
            })
        });

        if (cacheResponse.ok) {
            const data = await cacheResponse.json();
            const coverUrl = data.series_cover_url;

            if (coverUrl) {
                coverCache[slug] = coverUrl;
                applyCoverImage(coverDiv, coverUrl);
            } else {
                coverDiv.dataset.loaded = 'no-cover';
            }
        }
    } catch (error) {
        console.log(`Could not load cover for ${slug}:`, error);
        coverDiv.dataset.loaded = 'error';
    }
}

function applyCoverImage(coverDiv, coverUrl) {
    const img = document.createElement('img');
    img.src = coverUrl;
    img.alt = 'Series Cover';
    img.onload = () => {
        coverDiv.appendChild(img);
        coverDiv.dataset.loaded = 'true';
    };
    img.onerror = () => {
        coverDiv.dataset.loaded = 'error';
    };
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function selectSeries(slug, source) {
    // Construct URL based on source
    const baseUrls = {
        'series': 'http://186.2.175.5',
        'anime': 'https://aniworld.to'
    };
    const contentPaths = {
        'series': '/serie/stream/',
        'anime': '/anime/stream/'
    };

    // Use currentSource if source not provided
    const selectedSource = source || currentSource;
    const baseUrl = baseUrls[selectedSource];
    const contentPath = contentPaths[selectedSource];
    const url = `${baseUrl}${contentPath}${slug}`;

    document.getElementById('url').value = url;

    // Scroll to URL section
    document.querySelector('#quickstart').scrollIntoView({ behavior: 'smooth' });

    // Auto-parse URL
    await parseURLManually();
}

// Initialize catalog on page load
document.addEventListener('DOMContentLoaded', () => {
    const searchInput = document.getElementById('catalogSearch');
    if (searchInput) {
        searchInput.addEventListener('input', debounce(applyFilters, 300));
    }

    // Auto-load catalog from cache if available
    loadCatalogOnStartup();
});

async function loadCatalogOnStartup() {
    try {
        // Check if cache exists via stats endpoint for current source
        const statsResponse = await fetch(`/api/catalog/stats?source=${currentSource}`);
        const stats = await statsResponse.json();

        if (stats.cached && !stats.is_stale) {
            // Load from cache automatically
            console.log(`📦 Loading ${stats.source_name} catalog from cache...`);
            await updateCatalog(false);
        } else if (stats.cached && stats.is_stale) {
            // Show cache is stale
            console.log(`⚠️ ${stats.source_name} catalog cache is stale. Click "Nach Updates suchen" to refresh.`);
            document.getElementById('catalogLastUpdate').textContent =
                'Cache veraltet - Aktualisierung empfohlen';
        } else {
            console.log(`ℹ️ No ${stats.source_name} catalog cache found. Click "Nach Updates suchen" to load.`);
        }
    } catch (error) {
        console.log('Could not check catalog status:', error);
    }
}
