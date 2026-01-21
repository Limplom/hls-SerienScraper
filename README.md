# HLS Video Downloader

Automated HLS video stream downloader with modern web interface.

## Features

- **Series & Episode Management**: Browse, search, and download entire series or specific episodes
- **Parallel Downloads**: Download multiple episodes simultaneously (configurable 1-10 concurrent)
- **Smart Caching**: Series data is cached for faster navigation
- **Language Selection**: Choose audio language/subtitles before download
- **Multiple Formats**: MKV, MP4, AVI video formats
- **Audio Extraction**: Extract audio only (MP3, FLAC, AAC, OGG, WAV, Opus)
- **Quality Selection**: Best, 1080p, 720p, 480p
- **Video Codec Options**: Auto, H.264, H.265 (HEVC), AV1
- **Ad Blocking**: Built-in ad and overlay blocking
- **Real-time Progress**: Live progress tracking with WebSocket updates
- **Download Queue**: Queue multiple series/episodes for batch downloading
- **Plex-compatible Naming**: Output files named for media server compatibility

## Screenshots

The web interface provides:
- Series catalog browser with search and genre filtering
- Episode selector with batch selection tools
- Download queue with real-time progress
- Detailed logs for troubleshooting

## Installation

### Prerequisites

- Python 3.9+
- FFmpeg (auto-downloaded on first run)
- Chromium browser (installed via Playwright)

### Quick Start

```bash
# 1. Clone or extract the repository
cd hls-Downloader

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

### FFmpeg

FFmpeg is required for video processing and conversion. The tool will **automatically download** FFmpeg on first run if it's not found on your system.

- FFmpeg is downloaded to the `bin/` folder
- No manual installation required
- Supports Windows, Linux, and macOS

If you prefer to use your system's FFmpeg installation, simply ensure it's in your PATH.

## Project Structure

```
hls-Downloader/
├── app/                      # Main application
│   ├── __init__.py
│   ├── browser_pool.py       # Browser instance management
│   ├── config.py             # Configuration handling
│   ├── download_queue.py     # Download queue management
│   ├── ffmpeg_setup.py       # Auto FFmpeg download/setup
│   ├── hls_downloader_final.py # Core download logic
│   ├── models.py             # Data models
│   ├── series_cache.py       # Series data caching
│   ├── series_catalog.py     # Catalog management
│   ├── tasks.py              # Background tasks
│   └── web_gui.py            # Flask web application
│
├── bin/                      # FFmpeg binaries (auto-downloaded)
│
├── config/                   # Configuration files
│   ├── requirements.txt      # Python dependencies
│   └── settings.json         # User settings
│
├── static/                   # Frontend assets
│   ├── css/
│   │   └── style.css         # Application styles
│   └── js/
│       └── app.js            # Frontend JavaScript
│
├── templates/                # HTML templates
│   └── index.html            # Main page template
│
├── Downloads/                # Default download directory
├── series_cache/             # Cached series data
├── filter_cache/             # Ad-blocking filter cache
└── run.py                    # Application entry point
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
- **Max Parallel Downloads**: Number of concurrent downloads (1-10)
- **Override Series Name**: Custom name for downloaded files
- **English Titles**: Use English episode titles if available
- **Overwrite**: Replace existing files
- **Auto-Retry**: Automatically retry failed downloads

## Configuration

Settings are stored in `config/settings.json`:

```json
{
  "download_path": "Downloads",
  "default_quality": "best",
  "default_format": "mkv",
  "max_concurrent": 3,
  "auto_retry": true
}
```

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
# Change port in run.py or use environment variable
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
