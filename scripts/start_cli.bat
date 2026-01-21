@echo off
REM HLS Video Downloader - CLI Starter
REM Startet die Command-Line Version

echo ========================================================================
echo HLS Video Downloader - CLI Version
echo ========================================================================
echo.

REM Change to script directory
cd /d "%~dp0"

REM Activate venv if exists
if exist "venv\" (
    echo [INFO] Aktiviere Virtual Environment...
    call venv\Scripts\activate.bat
)

REM Check if dependencies are installed
python -c "import playwright; import yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Dependencies nicht installiert!
    echo Bitte fuehre zuerst 'start_webgui.bat' aus um Dependencies zu installieren
    echo Oder installiere manuell: pip install -r requirements.txt
    pause
    exit /b 1
)

echo [OK] Dependencies gefunden
echo.
echo ========================================================================
echo.

REM Show usage
if "%~1"=="" (
    echo Verwendung:
    echo   start_cli.bat "URL" [--episodes RANGE] [--parallel N]
    echo.
    echo Beispiele:
    echo   start_cli.bat "http://186.2.175.5/serie/stream/NAME/staffel-1/episode-1"
    echo   start_cli.bat "URL" --episodes 1-8 --parallel 4
    echo.
    echo Fuer weitere Optionen:
    echo   python hls_downloader_final.py --help
    echo.
    pause
    exit /b 0
)

REM Run with all arguments
python hls_downloader_final.py %*

echo.
echo ========================================================================
pause
