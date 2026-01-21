@echo off
REM HLS Video Downloader - Web-GUI Starter
REM Überprüft Dependencies und startet die Web-GUI

echo ========================================================================
echo HLS Video Downloader - Web-GUI Starter
echo ========================================================================
echo.

REM Change to project root directory
cd /d "%~dp0.."

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python ist nicht installiert!
    echo Bitte installiere Python 3.8 oder hoeher von https://www.python.org/
    pause
    exit /b 1
)

echo [OK] Python gefunden
echo.

REM Check if venv exists, create if not
if not exist "venv\" (
    echo [INFO] Virtual Environment nicht gefunden, erstelle neue venv...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Konnte venv nicht erstellen!
        pause
        exit /b 1
    )
    echo [OK] venv erstellt
    echo.
)

REM Activate venv
echo [INFO] Aktiviere Virtual Environment...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Konnte venv nicht aktivieren!
    pause
    exit /b 1
)

echo [OK] venv aktiviert
echo.

REM Check and install dependencies
echo [INFO] Ueberpruefe Dependencies...
echo.

REM Check Flask
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo [INSTALL] Flask wird installiert...
    pip install flask flask-socketio python-socketio eventlet
)

REM Check Playwright
python -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo [INSTALL] Playwright wird installiert...
    pip install playwright
    echo [INFO] Installiere Chromium Browser...
    playwright install chromium
)

REM Check yt-dlp
python -c "import yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo [INSTALL] yt-dlp wird installiert...
    pip install yt-dlp
)

echo.
echo [OK] Alle Dependencies sind installiert!
echo.

REM Check if Chromium is installed
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.launch(); p.stop()" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Chromium Browser nicht gefunden, installiere...
    playwright install chromium
)

echo ========================================================================
echo Starte Web-GUI...
echo ========================================================================
echo.
echo [INFO] Oeffne deinen Browser und gehe zu: http://localhost:5000
echo [INFO] Druecke Ctrl+C um den Server zu stoppen
echo.
echo ========================================================================
echo.

REM Start the web GUI
python run.py

REM If we get here, the server stopped
echo.
echo ========================================================================
echo Web-GUI wurde beendet
echo ========================================================================
pause
