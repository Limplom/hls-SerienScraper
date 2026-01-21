#!/usr/bin/env python3
"""
HLS Downloader - Main Entry Point
Run this file to start the web GUI in development mode.
"""
import sys
import os

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

    print("=" * 60)
    print("🌐 Starting Web Server")
    print("=" * 60)
    print()
    print("📍 Server URL: http://localhost:5000")
    print("📁 Downloads folder: ./Downloads/")
    print()
    print("Press Ctrl+C to stop the server")
    print("=" * 60)
    print()

    socketio.run(app, debug=True, host='0.0.0.0', port=5000)

if __name__ == '__main__':
    main()
