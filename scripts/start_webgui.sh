#!/usr/bin/env bash
# HLS Video Downloader - Web-GUI Starter
# Überprüft Dependencies und startet die Web-GUI

echo "========================================================================"
echo "HLS Video Downloader - Web-GUI Starter"
echo "========================================================================"
echo

# Change to project root directory
cd "$(dirname "$0")/.." || exit 1

# Check if Python is installed
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python ist nicht installiert!"
    echo "Bitte installiere Python 3.8 oder höher:"
    echo "  Linux (Debian/Ubuntu): sudo apt install python3 python3-venv"
    echo "  macOS: brew install python3"
    exit 1
fi

echo "[OK] Python gefunden ($(python3 --version))"
echo

# Check if venv exists, create if not
if [ ! -d "venv" ]; then
    echo "[INFO] Virtual Environment nicht gefunden, erstelle neue venv..."
    python3 -m venv venv
    if [ $? -ne 0 ]; then
        echo "[ERROR] Konnte venv nicht erstellen!"
        exit 1
    fi
    echo "[OK] venv erstellt"
    echo
fi

# Activate venv
echo "[INFO] Aktiviere Virtual Environment..."
source venv/bin/activate
if [ $? -ne 0 ]; then
    echo "[ERROR] Konnte venv nicht aktivieren!"
    exit 1
fi

echo "[OK] venv aktiviert"
echo

# Check and install dependencies
echo "[INFO] Überprüfe Dependencies..."
echo

# Check Flask
if ! python3 -c "import flask" &>/dev/null; then
    echo "[INSTALL] Flask wird installiert..."
    pip install flask flask-socketio python-socketio eventlet
fi

# Check Playwright
if ! python3 -c "import playwright" &>/dev/null; then
    echo "[INSTALL] Playwright wird installiert..."
    pip install playwright
    echo "[INFO] Installiere Chromium Browser..."
    playwright install chromium
fi

# Check yt-dlp
if ! python3 -c "import yt_dlp" &>/dev/null; then
    echo "[INSTALL] yt-dlp wird installiert..."
    pip install yt-dlp
fi

echo
echo "[OK] Alle Dependencies sind installiert!"
echo

# Check if Chromium is installed
if ! python3 -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.launch(); p.stop()" &>/dev/null; then
    echo "[WARN] Chromium Browser nicht gefunden, installiere..."
    playwright install chromium
fi

echo "========================================================================"
echo "Starte Web-GUI..."
echo "========================================================================"
echo
echo "[INFO] Öffne deinen Browser und gehe zu: http://localhost:5000"
echo "[INFO] Drücke Ctrl+C um den Server zu stoppen"
echo
echo "========================================================================"
echo

# Start the web GUI
python3 run.py

# If we get here, the server stopped
echo
echo "========================================================================"
echo "Web-GUI wurde beendet"
echo "========================================================================"
