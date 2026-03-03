#!/usr/bin/env bash
# HLS Video Downloader - CLI Starter
# Startet die Command-Line Version

echo "========================================================================"
echo "HLS Video Downloader - CLI Version"
echo "========================================================================"
echo

# Change to project root directory
cd "$(dirname "$0")/.." || exit 1

# Activate venv if exists
if [ -d "venv" ]; then
    echo "[INFO] Aktiviere Virtual Environment..."
    source venv/bin/activate
fi

# Check if dependencies are installed
if ! python3 -c "import playwright; import yt_dlp" &>/dev/null; then
    echo "[ERROR] Dependencies nicht installiert!"
    echo "Bitte führe zuerst './scripts/start_webgui.sh' aus um Dependencies zu installieren"
    echo "Oder installiere manuell: pip install -r requirements.txt"
    exit 1
fi

echo "[OK] Dependencies gefunden"
echo
echo "========================================================================"
echo

# Show usage if no arguments
if [ $# -eq 0 ]; then
    echo "Verwendung:"
    echo "  ./scripts/start_cli.sh \"URL\" [--episodes RANGE] [--parallel N]"
    echo
    echo "Beispiele:"
    echo "  ./scripts/start_cli.sh \"http://186.2.175.5/serie/stream/NAME/staffel-1/episode-1\""
    echo "  ./scripts/start_cli.sh \"URL\" --episodes 1-8 --parallel 4"
    echo
    echo "Für weitere Optionen:"
    echo "  python3 hls_downloader_final.py --help"
    echo
    exit 0
fi

# Run with all arguments
python3 hls_downloader_final.py "$@"

echo
echo "========================================================================"
