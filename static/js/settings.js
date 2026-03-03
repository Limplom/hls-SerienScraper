/**
 * Settings Page JavaScript
 * Handles settings form interactions and API calls
 */

// Show notification message
function showNotification(message, type = 'success') {
    const notification = document.getElementById('saveNotification');
    notification.textContent = message;
    notification.className = `notification ${type}`;
    notification.style.display = 'block';

    // Auto-hide after 5 seconds
    setTimeout(() => {
        notification.style.display = 'none';
    }, 5000);
}

// Collect form data into JSON object
function collectFormData() {
    const form = document.getElementById('settingsForm');
    const formData = new FormData(form);
    const settings = {
        auto_scraper: {},
        cache: {}
    };

    for (const [key, value] of formData.entries()) {
        if (key.startsWith('auto_scraper.')) {
            const subKey = key.replace('auto_scraper.', '');
            // Handle boolean checkboxes
            if (subKey === 'enabled') {
                settings.auto_scraper[subKey] = true;
            } else {
                settings.auto_scraper[subKey] = isNaN(value) ? value : Number(value);
            }
        } else if (key.startsWith('cache.')) {
            const subKey = key.replace('cache.', '');
            // Handle boolean checkboxes
            if (subKey === 'enabled' || subKey === 'cache_cover_images' || subKey === 'cache_episodes') {
                settings.cache[subKey] = true;
            } else {
                settings.cache[subKey] = isNaN(value) ? value : Number(value);
            }
        } else {
            // Handle top-level settings
            if (key === 'audio_only' || key === 'browser_headless' || key === 'verify_downloads') {
                settings[key] = true;
            } else {
                settings[key] = isNaN(value) ? value : Number(value);
            }
        }
    }

    // Handle unchecked checkboxes (they don't appear in FormData)
    const checkboxes = [
        'audio_only',
        'browser_headless',
        'verify_downloads',
        'auto_scraper.enabled',
        'cache.enabled',
        'cache.cache_cover_images',
        'cache.cache_episodes'
    ];
    checkboxes.forEach(name => {
        const checkbox = form.querySelector(`[name="${name}"]`);
        if (checkbox && checkbox.type === 'checkbox' && !checkbox.checked) {
            if (name.startsWith('auto_scraper.')) {
                const subKey = name.replace('auto_scraper.', '');
                settings.auto_scraper[subKey] = false;
            } else if (name.startsWith('cache.')) {
                const subKey = name.replace('cache.', '');
                settings.cache[subKey] = false;
            } else {
                settings[name] = false;
            }
        }
    });

    return settings;
}

// Save settings to server
async function saveSettings() {
    try {
        const settings = collectFormData();

        const response = await fetch('/api/settings/save', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(settings)
        });

        const result = await response.json();

        if (result.success) {
            showNotification('✅ Settings saved successfully!', 'success');

            // Show restart notice for certain settings
            if (needsRestart(settings)) {
                document.getElementById('restartNotice').style.display = 'block';
            }
        } else {
            showNotification('❌ Error saving settings: ' + (result.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error saving settings:', error);
        showNotification('❌ Failed to save settings: ' + error.message, 'error');
    }
}

// Check if settings require restart
function needsRestart(settings) {
    const restartRequired = [
        'browser_headless',
        'browser_max_context_uses'
    ];

    // Simple check - in reality you'd compare with original values
    return restartRequired.some(key => key in settings);
}

// Reset to default settings
async function resetToDefaults() {
    if (!confirm('Are you sure you want to reset all settings to defaults? This cannot be undone.')) {
        return;
    }

    try {
        const response = await fetch('/api/settings/reset', {
            method: 'POST'
        });

        const result = await response.json();

        if (result.success) {
            showNotification('✅ Settings reset to defaults!', 'success');
            // Reload page to show default values
            setTimeout(() => {
                window.location.reload();
            }, 1500);
        } else {
            showNotification('❌ Error resetting settings: ' + (result.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error resetting settings:', error);
        showNotification('❌ Failed to reset settings: ' + error.message, 'error');
    }
}

// Export settings as JSON file
function exportSettings() {
    const settings = collectFormData();
    const blob = new Blob([JSON.stringify(settings, null, 4)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `hls-settings-${new Date().toISOString().split('T')[0]}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showNotification('📤 Settings exported successfully!', 'success');
}

// Test download path accessibility
async function testDownloadPath() {
    const downloadPath = document.getElementById('download_path').value;

    if (!downloadPath) {
        showNotification('❌ Please enter a download path first', 'error');
        return;
    }

    try {
        const response = await fetch('/api/settings/test-path', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ path: downloadPath })
        });

        const result = await response.json();

        if (result.success) {
            showNotification(`✅ Path is valid and accessible!\n${result.message}`, 'success');
        } else {
            showNotification(`❌ Path test failed: ${result.error}`, 'error');
        }
    } catch (error) {
        console.error('Error testing path:', error);
        showNotification('❌ Failed to test path: ' + error.message, 'error');
    }
}

// Restart server
async function restartServer() {
    if (!confirm('Are you sure you want to restart the server? All active downloads will be interrupted.')) {
        return;
    }

    try {
        showNotification('🔄 Restarting server...', 'info');

        const response = await fetch('/api/settings/restart', {
            method: 'POST'
        });

        const result = await response.json();

        if (result.success) {
            showNotification('🔄 Server is restarting... Page will reload automatically.', 'success');

            // Poll until server is back up, then reload
            const checkServer = setInterval(async () => {
                try {
                    const res = await fetch('/api/settings/save', {
                        method: 'HEAD',
                        signal: AbortSignal.timeout(2000)
                    });
                    // Server is back
                    clearInterval(checkServer);
                    window.location.reload();
                } catch (e) {
                    // Server still restarting
                }
            }, 2000);

            // Stop trying after 30 seconds
            setTimeout(() => {
                clearInterval(checkServer);
                showNotification('⚠️ Server may still be restarting. Please reload manually.', 'error');
            }, 30000);
        } else {
            showNotification('❌ Error restarting server: ' + (result.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error restarting server:', error);
        showNotification('❌ Failed to restart server: ' + error.message, 'error');
    }
}

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
    // Ctrl+S or Cmd+S to save
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        saveSettings();
    }
});

// Initialize tooltips or other UI enhancements on page load
document.addEventListener('DOMContentLoaded', () => {
    console.log('Settings page loaded');
});
