"""
Library Routes
Scans download directory and tracks which series are already downloaded.
"""
from flask import Blueprint, jsonify
import json
import os
import re
import logging
from pathlib import Path

from app.config import Config, PROJECT_ROOT

logger = logging.getLogger(__name__)

library_bp = Blueprint('library', __name__)

LIBRARY_FILE = PROJECT_ROOT / "config" / "library.json"


def scan_download_directory():
    """
    Scan the download directory and build a library index.

    Returns dict mapping normalized series names to their info:
    {
        "inside job": {
            "name": "Inside Job",
            "seasons": {
                "1": {"episode_count": 10, "files": ["file1.mkv", ...]},
                "2": {"episode_count": 5, "files": [...]}
            },
            "total_episodes": 15
        }
    }
    """
    download_path = Path(Config.get_download_path())
    library = {}

    if not download_path.exists():
        return library

    video_extensions = {'.mkv', '.mp4', '.avi', '.ts', '.mp3', '.flac', '.aac', '.ogg', '.wav', '.opus'}

    for series_dir in sorted(download_path.iterdir()):
        if not series_dir.is_dir():
            continue

        series_name = series_dir.name
        seasons = {}
        total_episodes = 0

        for item in sorted(series_dir.iterdir()):
            if item.is_dir() and item.name.lower().startswith("season"):
                # Extract season number
                season_num = item.name.split()[-1] if len(item.name.split()) > 1 else "0"
                try:
                    season_num = str(int(season_num))
                except ValueError:
                    season_num = "0"

                files = [
                    f.name for f in sorted(item.iterdir())
                    if f.is_file() and f.suffix.lower() in video_extensions
                ]

                # Extract episode numbers from filenames (e.g. "S01E05" -> 5)
                episode_numbers = []
                for fname in files:
                    m = re.search(r'S\d+E(\d+)', fname)
                    if m:
                        episode_numbers.append(int(m.group(1)))
                episode_numbers.sort()

                seasons[season_num] = {
                    "episode_count": len(files),
                    "episodes": episode_numbers,
                    "files": files
                }
                total_episodes += len(files)

        if seasons:
            key = series_name.lower().strip()
            library[key] = {
                "name": series_name,
                "seasons": seasons,
                "total_episodes": total_episodes
            }

    return library


def save_library(library):
    """Save library index to config/library.json"""
    LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = LIBRARY_FILE.with_suffix('.json.tmp')
    tmp_path.write_text(json.dumps(library, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp_path.replace(LIBRARY_FILE)


def load_library():
    """Load library index from config/library.json"""
    if LIBRARY_FILE.exists():
        try:
            return json.loads(LIBRARY_FILE.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warning(f"Error loading library: {e}")
    return {}


def normalize_name(name):
    """Normalize a series name for matching (lowercase, strip, collapse whitespace)."""
    import re
    return re.sub(r'\s+', ' ', name.strip().lower())


@library_bp.route('/api/library/scan', methods=['POST'])
def scan_library():
    """Scan download directory and update library index"""
    try:
        library = scan_download_directory()
        save_library(library)

        total_series = len(library)
        total_episodes = sum(s['total_episodes'] for s in library.values())

        logger.info(f"Library scan complete: {total_series} series, {total_episodes} episodes")

        return jsonify({
            'success': True,
            'total_series': total_series,
            'total_episodes': total_episodes,
            'library': library
        })
    except Exception as e:
        logger.error(f"Library scan error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@library_bp.route('/api/library', methods=['GET'])
def get_library():
    """Get current library index (from cache file, no rescan)"""
    library = load_library()
    return jsonify({
        'total_series': len(library),
        'total_episodes': sum(s['total_episodes'] for s in library.values()),
        'library': library
    })


@library_bp.route('/api/library/check/<path:series_name>', methods=['GET'])
def check_series(series_name):
    """Check if a specific series exists in the library"""
    library = load_library()
    key = normalize_name(series_name)

    if key in library:
        return jsonify({'downloaded': True, **library[key]})

    # Fuzzy: check if key is contained in any library entry or vice versa
    for lib_key, lib_data in library.items():
        if key in lib_key or lib_key in key:
            return jsonify({'downloaded': True, **lib_data})

    return jsonify({'downloaded': False})
