"""
Settings Routes
Handles settings page and settings API endpoints
"""
from flask import Blueprint, render_template, request, jsonify
import json
import os
from pathlib import Path
import traceback


settings_bp = Blueprint('settings', __name__)


@settings_bp.route('/settings')
def settings_page():
    """Settings configuration page"""
    from app.config import _json_settings, PROJECT_ROOT

    # Load current settings from JSON file
    settings_file = PROJECT_ROOT / "config" / "settings.json"
    if settings_file.exists():
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                current_settings = json.load(f)
        except Exception as e:
            print(f"⚠️ Error loading settings: {e}")
            current_settings = {}
    else:
        current_settings = {}

    # Provide defaults for any missing values
    settings_data = {
        'download_path': current_settings.get('download_path', './Downloads'),
        'max_parallel_limit': current_settings.get('max_parallel_limit', 25),
        'max_parallel_downloads': current_settings.get('max_parallel_downloads', 10),
        'default_format': current_settings.get('default_format', 'mkv'),
        'default_quality': current_settings.get('default_quality', '1080p'),
        'default_wait_time': current_settings.get('default_wait_time', 45),
        'audio_only': current_settings.get('audio_only', False),
        'verify_downloads': current_settings.get('verify_downloads', True),
        'browser_max_context_uses': current_settings.get('browser_max_context_uses', 75),
        'browser_headless': current_settings.get('browser_headless', True),
        'auto_scraper': {
            'enabled': current_settings.get('auto_scraper', {}).get('enabled', True),
            'idle_threshold_seconds': current_settings.get('auto_scraper', {}).get('idle_threshold_seconds', 30),
            'scrape_interval_seconds': current_settings.get('auto_scraper', {}).get('scrape_interval_seconds', 25),
            'batch_size': current_settings.get('auto_scraper', {}).get('batch_size', 10),
            'min_idle_between_scrapes': current_settings.get('auto_scraper', {}).get('min_idle_between_scrapes', 5)
        },
        'cache': {
            'enabled': current_settings.get('cache', {}).get('enabled', True),
            'cache_dir': current_settings.get('cache', {}).get('cache_dir', './cache'),
            'cache_cover_images': current_settings.get('cache', {}).get('cache_cover_images', True),
            'cache_episodes': current_settings.get('cache', {}).get('cache_episodes', True),
            'hot_cache_size': current_settings.get('cache', {}).get('hot_cache_size', 100),
            'ttl_metadata_days': current_settings.get('cache', {}).get('ttl_metadata_days', 7),
            'ttl_episodes_days': current_settings.get('cache', {}).get('ttl_episodes_days', 30),
            'ttl_cover_images_days': current_settings.get('cache', {}).get('ttl_cover_images_days', 90)
        }
    }

    return render_template('settings.html', settings=settings_data)


@settings_bp.route('/api/settings/save', methods=['POST'])
def save_settings():
    """Save settings to config/settings.json"""
    try:
        from app.config import PROJECT_ROOT

        new_settings = request.json

        # Validate settings
        if 'max_parallel_downloads' in new_settings:
            max_val = new_settings.get('max_parallel_limit', 25)
            if not (1 <= new_settings['max_parallel_downloads'] <= max_val):
                return jsonify({
                    'success': False,
                    'error': f'max_parallel_downloads must be between 1 and {max_val}'
                }), 400

        # Save to file
        settings_file = PROJECT_ROOT / "config" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)

        with open(settings_file, 'w', encoding='utf-8') as f:
            json.dump(new_settings, f, indent=4, ensure_ascii=False)

        print(f"✅ Settings saved to {settings_file}")

        return jsonify({
            'success': True,
            'message': 'Settings saved successfully'
        })

    except Exception as e:
        print(f"❌ Error saving settings: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@settings_bp.route('/api/settings/reset', methods=['POST'])
def reset_settings():
    """Reset settings to defaults"""
    try:
        from app.config import PROJECT_ROOT

        default_settings = {
            "download_path": "./Downloads",
            "max_parallel_limit": 25,
            "max_parallel_downloads": 10,
            "default_format": "mkv",
            "default_quality": "1080p",
            "default_wait_time": 45,
            "audio_only": False,
            "verify_downloads": True,
            "browser_max_context_uses": 75,
            "browser_headless": True,
            "auto_scraper": {
                "enabled": True,
                "idle_threshold_seconds": 30,
                "scrape_interval_seconds": 25,
                "batch_size": 10,
                "min_idle_between_scrapes": 5
            },
            "cache": {
                "enabled": True,
                "cache_dir": "./cache",
                "cache_cover_images": True,
                "cache_episodes": True,
                "hot_cache_size": 100,
                "ttl_metadata_days": 7,
                "ttl_episodes_days": 30,
                "ttl_cover_images_days": 90
            }
        }

        settings_file = PROJECT_ROOT / "config" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)

        with open(settings_file, 'w', encoding='utf-8') as f:
            json.dump(default_settings, f, indent=4, ensure_ascii=False)

        print(f"✅ Settings reset to defaults")

        return jsonify({
            'success': True,
            'message': 'Settings reset to defaults'
        })

    except Exception as e:
        print(f"❌ Error resetting settings: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@settings_bp.route('/api/settings/test-path', methods=['POST'])
def test_download_path():
    """Test if download path is accessible"""
    try:
        from app.config import PROJECT_ROOT

        data = request.json
        test_path = data.get('path', '')

        if not test_path:
            return jsonify({
                'success': False,
                'error': 'No path provided'
            }), 400

        # Convert to absolute path if relative
        if not os.path.isabs(test_path):
            test_path = str(PROJECT_ROOT / test_path)

        # Try to create directory
        Path(test_path).mkdir(parents=True, exist_ok=True)

        # Test write access
        test_file = Path(test_path) / '.test_write'
        try:
            test_file.write_text('test')
            test_file.unlink()
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'Path exists but is not writable: {e}'
            }), 400

        return jsonify({
            'success': True,
            'message': f'Path is valid and writable: {test_path}'
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
