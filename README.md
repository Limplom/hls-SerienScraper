# HLS Video Downloader

Automated HLS video stream downloader with modern web interface.

## Features

- **Series & Episode Management**: Browse, search, and download entire series or specific episodes
- **Parallel Downloads**: Download multiple episodes simultaneously (configurable)
- **Download Queue**: Queue multiple series/episodes for batch downloading with priority ordering
- **Real-time Progress**: Live progress tracking with WebSocket updates and download speed display
- **Episode Progress Tracking**: Individual progress bars per episode with stop/cancel/restart controls
- **Smart Caching**: Multi-layer caching (disk + hot in-memory cache) for series metadata, cover images, and episodes
- **Auto-Scraper**: Automatically updates series metadata in the background when idle
- **Series Catalog**: Browse the full series and anime catalog with search and genre filtering
- **Language Selection**: Choose audio language/subtitles before download
- **Multiple Formats**: MKV, MP4, AVI video formats
- **Audio Extraction**: Extract audio only (MP3, FLAC, AAC, OGG, WAV, Opus)
- **Quality Selection**: Best, 1080p, 720p, 480p
- **Video Codec Options**: Auto, H.264, H.265 (HEVC), AV1
- **Ad Blocking**: Built-in ad and overlay blocking with auto-updating filter lists
- **File Verification**: Verify downloaded files for integrity (duration, codecs, resolution)
- **Plex-compatible Naming**: Output files named for media server compatibility
- **Settings UI**: Web-based settings page with server restart capability
- **Drag & Drop Reordering**: Reorder queued episodes via drag and drop

## Screenshots

The web interface provides:
- Series catalog browser with search and genre filtering
- Episode selector with batch selection tools
- Download queue with real-time progress per episode
- Settings page for all configuration options
- Detailed logs for troubleshooting

## Installation

### Prerequisites

- Python 3.9+
- FFmpeg (auto-downloaded on first run)
- Chromium browser (installed via Playwright)

### Quick Start

```bash
# 1. Clone or extract the repository
cd hls-SerienScraper

# 2. Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# 3. Install dependencies
pip install -r config/requirements.txt

# 4. Install Playwright browser
playwright install chromium

# 5. Start the web GUI
python run.py

# 6. Open browser
# Navigate to http://localhost:5000
```

Alternatively, use the provided startup scripts:
```bash
# Linux/macOS
./scripts/start_webgui.sh

# Windows
scripts\start_webgui.bat
```

### FFmpeg

FFmpeg is required for video processing and conversion. The tool will **automatically download** FFmpeg on first run if it's not found on your system.

- FFmpeg is downloaded to the `bin/` folder
- No manual installation required
- Supports Windows, Linux, and macOS

If you prefer to use your system's FFmpeg installation, simply ensure it's in your PATH.

## Project Structure

```
hls-SerienScraper/
├── app/                          # Main application
│   ├── __init__.py
│   ├── browser_pool.py           # Browser instance management
│   ├── config.py                 # Configuration handling
│   ├── download_queue.py         # Download queue management
│   ├── ffmpeg_setup.py           # Auto FFmpeg download/setup
│   ├── file_verification.py      # Downloaded file integrity checks
│   ├── hls_downloader_final.py   # Core download logic
│   ├── series_cache.py           # Series data caching
│   ├── series_catalog.py         # Catalog management
│   ├── web_gui.py                # Flask web application & WebSocket
│   ├── routes/
│   │   ├── catalog_routes.py     # Series catalog API endpoints
│   │   └── settings_routes.py    # Settings API endpoints
│   └── services/
│       └── cache_manager.py      # Multi-layer cache manager
│
├── bin/                          # FFmpeg binaries (auto-downloaded)
│
├── config/                       # Configuration files
│   ├── requirements.txt          # Python dependencies
│   └── settings.json             # User settings
│
├── scripts/                      # Startup scripts
│   ├── setup.py                  # Initial setup helper
│   ├── start_webgui.bat          # Windows launcher
│   ├── start_webgui.sh           # Linux/macOS launcher
│   ├── start_cli.bat             # Windows CLI launcher
│   └── start_cli.sh              # Linux/macOS CLI launcher
│
├── static/                       # Frontend assets
│   ├── css/
│   │   ├── style.css             # Main application styles
│   │   └── settings.css          # Settings page styles
│   └── js/
│       ├── app.js                # Main frontend JavaScript
│       └── settings.js           # Settings page JavaScript
│
├── templates/                    # HTML templates
│   ├── index.html                # Main page template
│   └── settings.html             # Settings page template
│
├── Downloads/                    # Default download directory
├── series_cache/                 # Cached series data
├── cache/                        # Disk cache (metadata, covers)
├── filter_cache/                 # Ad-blocking filter cache
└── run.py                        # Application entry point
```

## Usage

### Basic Usage

1. **Enter URL**: Paste a series or episode URL in the input field
2. **Analyze**: Click the search icon to parse the URL
3. **Select Episodes**: Choose which episodes to download
4. **Configure Options**: Set quality, format, language preferences
5. **Start Download**: Click "Start Download" to begin

### Batch Mode

1. Open the Series Catalog
2. Enable "Batch Mode"
3. Select multiple series
4. Add all to queue at once

### Advanced Options

- **Wait Time**: Seconds to wait for page load (30-120)
- **Max Parallel Downloads**: Number of concurrent downloads
- **Override Series Name**: Custom name for downloaded files
- **English Titles**: Use English episode titles if available
- **Overwrite**: Replace existing files

## Configuration

Settings can be configured via the **Settings UI** at `/settings` or by editing `config/settings.json` directly:

```json
{
    "download_path": "./Downloads",
    "max_parallel_downloads": 3,
    "max_parallel_limit": 10,
    "default_format": "mkv",
    "default_quality": "1080p",
    "default_wait_time": 60,
    "audio_only": false,
    "verify_downloads": true,
    "browser_max_context_uses": 75,
    "browser_headless": true,
    "auto_scraper": {
        "enabled": true,
        "idle_threshold_seconds": 30,
        "scrape_interval_seconds": 25,
        "batch_size": 10,
        "min_idle_between_scrapes": 5
    },
    "cache": {
        "enabled": true,
        "cache_cover_images": true,
        "cache_episodes": true,
        "hot_cache_size": 100,
        "ttl_metadata_days": 7,
        "ttl_cover_images_days": 90
    }
}
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FLASK_HOST` | `127.0.0.1` | Server bind address |
| `FLASK_PORT` | `5000` | Server port |
| `FLASK_DEBUG` | `false` | Enable debug mode |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Dependencies

Core dependencies (see `config/requirements.txt`):
- `playwright` - Browser automation
- `yt-dlp` - Video downloading
- `flask` - Web framework
- `flask-socketio` - WebSocket support
- `eventlet` - Async networking

## Troubleshooting

### Browser Issues
```bash
# Reinstall Playwright browsers
playwright install chromium --force
```

### Port Already in Use
```bash
# Change port via environment variable
export FLASK_PORT=5001
python run.py
```

### Download Stuck
- Check the logs section for error messages
- Try increasing the wait time in Advanced Options
- Ensure stable internet connection

## License

This tool is for private, non-commercial use only.

## Disclaimer

This tool is provided as-is for educational purposes. Users are responsible for ensuring their use complies with applicable laws and terms of service.
