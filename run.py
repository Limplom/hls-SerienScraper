#!/usr/bin/env python3
"""
HLS Downloader - Main Entry Point
Run this file to start the web GUI in development mode.
"""
import sys
import os
import logging

# IMPORTANT: Disable output buffering FIRST (before any other imports)
# This ensures print() output from threads is displayed immediately
os.environ['PYTHONUNBUFFERED'] = '1'

# Configure logging for all modules
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)

# Fix Windows console encoding for emoji support
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass  # Fallback if reconfigure fails

# Add app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

def check_dependencies():
    """Check and setup required dependencies."""
    print("=" * 60)
    print("🚀 HLS Downloader - Startup Check")
    print("=" * 60)
    print()

    # Check FFmpeg
    from app.ffmpeg_setup import ensure_ffmpeg
    if not ensure_ffmpeg():
        print("\n⚠ Warning: FFmpeg not available. Some features may not work.")
        print("  The tool will attempt to use yt-dlp's built-in downloading.")
        print()

    # Check Playwright (optional, just inform user)
    try:
        from playwright.sync_api import sync_playwright
        print("✓ Playwright found")
    except ImportError:
        print("⚠ Playwright not installed. Run: pip install playwright")
        print("  Then run: playwright install chromium")

    print()

def main():
    """Main entry point."""
    check_dependencies()

    # Import and run the web GUI
    from app.web_gui import app, socketio

    # Configurable server settings via environment variables
    DEBUG = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    HOST = os.getenv('FLASK_HOST', '127.0.0.1')
    PORT = int(os.getenv('FLASK_PORT', 5000))

    print("=" * 60)
    print("🌐 Starting Web Server")
    print("=" * 60)
    print()
    print(f"📍 Server URL: http://{HOST}:{PORT}")
    print("📁 Downloads folder: ./Downloads/")
    print(f"🔧 Debug mode: {DEBUG}")
    print()
    print("Press Ctrl+C to stop the server")
    print("=" * 60)
    print()

    socketio.run(app, debug=DEBUG, host=HOST, port=PORT)

if __name__ == '__main__':
    main()
