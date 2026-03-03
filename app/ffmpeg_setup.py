#!/usr/bin/env python3
"""
FFmpeg Auto-Setup Module
Automatically downloads and extracts FFmpeg if not found.
"""

import os
import sys
import platform
import zipfile
import tarfile
import shutil
import urllib.request
import tempfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# FFmpeg download URLs for different platforms
FFMPEG_URLS = {
    'Windows': {
        'url': 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip',
        'archive_type': 'zip',
        'binary_name': 'ffmpeg.exe',
        'probe_name': 'ffprobe.exe'
    },
    'Linux': {
        'url': 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz',
        'archive_type': 'tar.xz',
        'binary_name': 'ffmpeg',
        'probe_name': 'ffprobe'
    },
    'Darwin': {  # macOS
        'url': 'https://evermeet.cx/ffmpeg/getrelease/zip',
        'archive_type': 'zip',
        'binary_name': 'ffmpeg',
        'probe_name': 'ffprobe'
    }
}

def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent

def get_bin_dir():
    """Get the bin directory path."""
    bin_dir = get_project_root() / 'bin'
    bin_dir.mkdir(exist_ok=True)
    return bin_dir

def get_ffmpeg_path():
    """Get the path to ffmpeg binary in bin folder."""
    system = platform.system()
    config = FFMPEG_URLS.get(system, FFMPEG_URLS['Linux'])
    return get_bin_dir() / config['binary_name']

def get_ffprobe_path():
    """Get the path to ffprobe binary in bin folder."""
    system = platform.system()
    config = FFMPEG_URLS.get(system, FFMPEG_URLS['Linux'])
    return get_bin_dir() / config['probe_name']

def is_ffmpeg_installed():
    """Check if FFmpeg is available (either in PATH or in bin folder)."""
    # First check our bin folder
    ffmpeg_path = get_ffmpeg_path()
    if ffmpeg_path.exists():
        return True

    # Then check system PATH
    return shutil.which('ffmpeg') is not None

def is_ffprobe_installed():
    """Check if FFprobe is available."""
    ffprobe_path = get_ffprobe_path()
    if ffprobe_path.exists():
        return True
    return shutil.which('ffprobe') is not None

def download_with_progress(url, dest_path):
    """Download a file with progress indication."""
    logger.info(f"Downloading from: {url[:60]}...")

    def report_progress(block_num, block_size, total_size):
        if total_size > 0:
            percent = min(100, block_num * block_size * 100 / total_size)
            downloaded = min(total_size, block_num * block_size)
            mb_downloaded = downloaded / (1024 * 1024)
            mb_total = total_size / (1024 * 1024)
            logger.info(f"Progress: {percent:.1f}% ({mb_downloaded:.1f}/{mb_total:.1f} MB)")

    urllib.request.urlretrieve(url, dest_path, reporthook=report_progress)

def extract_ffmpeg_from_archive(archive_path, dest_dir, archive_type):
    """Extract FFmpeg binaries from archive."""
    system = platform.system()
    config = FFMPEG_URLS.get(system, FFMPEG_URLS['Linux'])

    logger.info(f"Extracting archive...")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        if archive_type == 'zip':
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(temp_path)
        elif archive_type in ['tar.xz', 'tar.gz']:
            with tarfile.open(archive_path, 'r:*') as tf:
                tf.extractall(temp_path)

        # Find the ffmpeg binary in the extracted files
        ffmpeg_binary = None
        ffprobe_binary = None

        for root, dirs, files in os.walk(temp_path):
            for file in files:
                if file == config['binary_name']:
                    ffmpeg_binary = Path(root) / file
                elif file == config['probe_name']:
                    ffprobe_binary = Path(root) / file

        if ffmpeg_binary:
            dest_ffmpeg = dest_dir / config['binary_name']
            shutil.copy2(ffmpeg_binary, dest_ffmpeg)
            if system != 'Windows':
                os.chmod(dest_ffmpeg, 0o755)
            logger.info(f"Extracted {config['binary_name']}")

        if ffprobe_binary:
            dest_ffprobe = dest_dir / config['probe_name']
            shutil.copy2(ffprobe_binary, dest_ffprobe)
            if system != 'Windows':
                os.chmod(dest_ffprobe, 0o755)
            logger.info(f"Extracted {config['probe_name']}")

        return ffmpeg_binary is not None

def download_ffmpeg():
    """Download and install FFmpeg to bin folder."""
    system = platform.system()

    if system not in FFMPEG_URLS:
        logger.warning(f"Unsupported platform: {system}")
        logger.warning("Please install FFmpeg manually: https://ffmpeg.org/download.html")
        return False

    config = FFMPEG_URLS[system]
    bin_dir = get_bin_dir()

    logger.info(f"Downloading FFmpeg for {system}...")

    # Download to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{config["archive_type"]}') as tmp:
        temp_archive = tmp.name

    try:
        download_with_progress(config['url'], temp_archive)

        # Extract
        success = extract_ffmpeg_from_archive(temp_archive, bin_dir, config['archive_type'])

        if success:
            logger.info(f"FFmpeg installed to: {bin_dir}")
            return True
        else:
            logger.error("Failed to extract FFmpeg binary")
            return False

    except Exception as e:
        logger.error(f"Download failed: {e}")
        return False
    finally:
        # Cleanup temp file
        if os.path.exists(temp_archive):
            os.remove(temp_archive)

def setup_ffmpeg_path():
    """Add bin directory to PATH if FFmpeg is there."""
    bin_dir = get_bin_dir()
    ffmpeg_path = get_ffmpeg_path()

    if ffmpeg_path.exists():
        # Add bin dir to PATH for this process
        bin_str = str(bin_dir)
        if bin_str not in os.environ.get('PATH', ''):
            os.environ['PATH'] = bin_str + os.pathsep + os.environ.get('PATH', '')
        return True
    return False

def ensure_ffmpeg():
    """
    Ensure FFmpeg is available. Downloads if not found.
    Returns True if FFmpeg is available, False otherwise.
    """
    logger.info("Checking FFmpeg installation...")

    # First, check if it's in our bin folder
    if setup_ffmpeg_path():
        logger.info("FFmpeg found in bin folder")
        return True

    # Check system PATH
    if is_ffmpeg_installed():
        logger.info("FFmpeg found in system PATH")
        return True

    # Not found - try to download
    logger.warning("FFmpeg not found")

    if download_ffmpeg():
        setup_ffmpeg_path()
        return True

    logger.warning("FFmpeg is required for video processing.")
    logger.warning("Please install it manually: https://ffmpeg.org/download.html")
    return False

def get_ffmpeg_executable():
    """Get the path to the FFmpeg executable to use."""
    # Check bin folder first
    bin_ffmpeg = get_ffmpeg_path()
    if bin_ffmpeg.exists():
        return str(bin_ffmpeg)

    # Fall back to system PATH
    system_ffmpeg = shutil.which('ffmpeg')
    if system_ffmpeg:
        return system_ffmpeg

    # Default fallback
    return 'ffmpeg'

def get_ffprobe_executable():
    """Get the path to the FFprobe executable to use."""
    # Check bin folder first
    bin_ffprobe = get_ffprobe_path()
    if bin_ffprobe.exists():
        return str(bin_ffprobe)

    # Fall back to system PATH
    system_ffprobe = shutil.which('ffprobe')
    if system_ffprobe:
        return system_ffprobe

    # Default fallback
    return 'ffprobe'


if __name__ == '__main__':
    # Test the module
    logger.info("FFmpeg Setup Test")
    logger.info("=" * 40)

    if ensure_ffmpeg():
        logger.info(f"FFmpeg executable: {get_ffmpeg_executable()}")
        logger.info(f"FFprobe executable: {get_ffprobe_executable()}")
    else:
        logger.error("FFmpeg setup failed!")
        sys.exit(1)
