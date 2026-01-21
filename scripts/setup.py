#!/usr/bin/env python3
"""
Setup Script für HLS Video Downloader
Überprüft und installiert alle Dependencies automatisch
"""

import subprocess
import sys
import os
from pathlib import Path

def print_header(text):
    """Print formatted header"""
    print("\n" + "="*70)
    print(text)
    print("="*70 + "\n")

def check_python_version():
    """Check if Python version is 3.8+"""
    print("🔍 Überprüfe Python Version...")
    version = sys.version_info
    if version.major < 3 or (version.major == 3 and version.minor < 8):
        print(f"❌ Python 3.8+ benötigt, aber {version.major}.{version.minor} gefunden")
        return False
    print(f"✅ Python {version.major}.{version.minor}.{version.micro} gefunden")
    return True

def check_module(module_name, import_name=None):
    """Check if a Python module is installed"""
    if import_name is None:
        import_name = module_name

    try:
        __import__(import_name)
        return True
    except ImportError:
        return False

def install_package(package_name, display_name=None):
    """Install a Python package via pip"""
    if display_name is None:
        display_name = package_name

    print(f"📦 Installiere {display_name}...")
    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", package_name,
            "--quiet", "--disable-pip-version-check"
        ])
        print(f"✅ {display_name} installiert")
        return True
    except subprocess.CalledProcessError:
        print(f"❌ Fehler beim Installieren von {display_name}")
        return False

def install_chromium():
    """Install Chromium browser for Playwright"""
    print("🌐 Installiere Chromium Browser...")
    try:
        subprocess.check_call([
            sys.executable, "-m", "playwright", "install", "chromium",
            "--quiet"
        ])
        print("✅ Chromium Browser installiert")
        return True
    except subprocess.CalledProcessError:
        print("❌ Fehler beim Installieren von Chromium")
        return False

def check_chromium():
    """Check if Chromium is installed"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except:
        return False

def main():
    print_header("HLS Video Downloader - Setup")

    # Check Python version
    if not check_python_version():
        print("\n❌ Setup fehlgeschlagen: Python Version zu alt")
        input("\nDrücke Enter zum Beenden...")
        return 1

    print("\n📋 Überprüfe Dependencies...\n")

    # List of required packages
    packages = [
        ("flask", "Flask"),
        ("flask-socketio", "Flask-SocketIO"),
        ("python-socketio", "Python-SocketIO"),
        ("eventlet", "Eventlet"),
        ("playwright", "Playwright"),
        ("yt-dlp", "yt-dlp", "yt_dlp"),
    ]

    all_installed = True
    needs_install = []

    # Check each package
    for package_info in packages:
        package_name = package_info[0]
        display_name = package_info[1]
        import_name = package_info[2] if len(package_info) > 2 else package_name.replace("-", "_")

        if check_module(package_name, import_name):
            print(f"✅ {display_name} bereits installiert")
        else:
            print(f"⚠️  {display_name} nicht gefunden")
            needs_install.append((package_name, display_name))
            all_installed = False

    # Install missing packages
    if needs_install:
        print(f"\n📦 Installiere {len(needs_install)} fehlende Pakete...\n")

        for package_name, display_name in needs_install:
            if not install_package(package_name, display_name):
                print(f"\n❌ Setup fehlgeschlagen beim Installieren von {display_name}")
                input("\nDrücke Enter zum Beenden...")
                return 1

    # Check Chromium
    print("\n🌐 Überprüfe Chromium Browser...")
    if not check_chromium():
        print("⚠️  Chromium Browser nicht gefunden")
        if not install_chromium():
            print("\n❌ Setup fehlgeschlagen beim Installieren von Chromium")
            input("\nDrücke Enter zum Beenden...")
            return 1
    else:
        print("✅ Chromium Browser bereits installiert")

    # Success
    print_header("✅ Setup erfolgreich abgeschlossen!")

    print("Du kannst jetzt den HLS Downloader verwenden:\n")
    print("  📁 Web-GUI starten:")
    print("     Windows: start_webgui.bat")
    print("     oder:    python web_gui.py\n")
    print("  📁 CLI verwenden:")
    print("     Windows: start_cli.bat \"URL\" --episodes 1-8 --parallel 4")
    print("     oder:    python hls_downloader_final.py \"URL\"\n")

    input("Drücke Enter zum Beenden...")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n❌ Setup abgebrochen")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unerwarteter Fehler: {e}")
        import traceback
        traceback.print_exc()
        input("\nDrücke Enter zum Beenden...")
        sys.exit(1)
