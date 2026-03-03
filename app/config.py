"""
Configuration for HLS Downloader - Production Ready
Supports environment variables for Docker deployment.
"""
import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Optional: load .env file if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, use environment variables directly

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent


def _load_settings_json():
    """Load settings from config/settings.json if exists."""
    settings_file = PROJECT_ROOT / "config" / "settings.json"
    if settings_file.exists():
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error loading settings.json: {e}")
    return {}

# Load JSON settings
_json_settings = _load_settings_json()


class Config:
    """Base configuration"""
    SECRET_KEY = os.getenv('SECRET_KEY', os.urandom(24).hex())

    # ===========================================
    # DOWNLOAD SETTINGS (from settings.json or env)
    # ===========================================

    # Download path - can be set via:
    # 1. Environment variable HLS_DOWNLOAD_PATH
    # 2. config/settings.json "download_path"
    # 3. Default: ./Downloads
    @staticmethod
    def get_download_path():
        path = os.getenv('HLS_DOWNLOAD_PATH', _json_settings.get('download_path', './Downloads'))
        if not os.path.isabs(path):
            path = str(PROJECT_ROOT / path)
        # Ensure directory exists
        Path(path).mkdir(parents=True, exist_ok=True)
        return path

    # Max parallel downloads - absolute maximum allowed (hard limit)
    MAX_PARALLEL_LIMIT = int(os.getenv(
        'HLS_MAX_PARALLEL_LIMIT',
        _json_settings.get('max_parallel_limit', 10)
    ))

    # Default parallel downloads at startup (can be changed at runtime)
    MAX_PARALLEL_DOWNLOADS = int(os.getenv(
        'HLS_MAX_PARALLEL',
        _json_settings.get('max_parallel_downloads', 3)
    ))

    # Default format (mkv, mp4, avi)
    DEFAULT_FORMAT = os.getenv(
        'HLS_DEFAULT_FORMAT',
        _json_settings.get('default_format', 'mkv')
    )

    # Default quality
    DEFAULT_QUALITY = os.getenv(
        'HLS_DEFAULT_QUALITY',
        _json_settings.get('default_quality', '1080p')
    )

    # Default wait time for browser
    DEFAULT_WAIT_TIME = int(os.getenv(
        'HLS_WAIT_TIME',
        _json_settings.get('default_wait_time', 60)
    ))

    # Note: Auto-retry has been removed. Retries are now manual only via the UI.

    # Audio only mode (extract only audio track)
    AUDIO_ONLY = os.getenv(
        'HLS_AUDIO_ONLY',
        str(_json_settings.get('audio_only', False))
    ).lower() in ('true', '1', 'yes')

    # ===========================================
    # AUTO-SCRAPER SETTINGS
    # ===========================================
    _auto_scraper_settings = _json_settings.get('auto_scraper', {})

    AUTO_SCRAPER_ENABLED = os.getenv(
        'HLS_AUTO_SCRAPER_ENABLED',
        str(_auto_scraper_settings.get('enabled', True))
    ).lower() in ('true', '1', 'yes')

    AUTO_SCRAPER_IDLE_THRESHOLD = int(os.getenv(
        'HLS_AUTO_SCRAPER_IDLE_THRESHOLD',
        _auto_scraper_settings.get('idle_threshold_seconds', 30)
    ))

    AUTO_SCRAPER_INTERVAL = int(os.getenv(
        'HLS_AUTO_SCRAPER_INTERVAL',
        _auto_scraper_settings.get('scrape_interval_seconds', 60)
    ))

    AUTO_SCRAPER_BATCH_SIZE = int(os.getenv(
        'HLS_AUTO_SCRAPER_BATCH_SIZE',
        _auto_scraper_settings.get('batch_size', 3)
    ))

    AUTO_SCRAPER_MIN_IDLE = int(os.getenv(
        'HLS_AUTO_SCRAPER_MIN_IDLE',
        _auto_scraper_settings.get('min_idle_between_scrapes', 10)
    ))

    # ===========================================
    # BROWSER SETTINGS
    # ===========================================

    # Browser Pool
    MAX_CONCURRENT_BROWSERS = int(os.getenv('MAX_CONCURRENT_BROWSERS', 10))
    BROWSER_TIMEOUT = int(os.getenv('BROWSER_TIMEOUT', 300))
    BROWSER_VIEWPORT_WIDTH = int(os.getenv('BROWSER_VIEWPORT_WIDTH', 1920))
    BROWSER_VIEWPORT_HEIGHT = int(os.getenv('BROWSER_VIEWPORT_HEIGHT', 1080))
    BROWSER_MAX_CONTEXT_USES = int(os.getenv(
        'BROWSER_MAX_CONTEXT_USES',
        _json_settings.get('browser_max_context_uses', 75)
    ))

    # Browser visibility mode
    # - headless: false = starts off-screen/hidden (default, best compatibility)
    # - headless: true = completely invisible (faster, but may be detected by some sites)
    BROWSER_HEADLESS = os.getenv(
        'BROWSER_HEADLESS',
        str(_json_settings.get('browser_headless', False))
    ).lower() in ('true', '1', 'yes')

    # Timeouts (in milliseconds)
    PAGE_LOAD_TIMEOUT = int(os.getenv('PAGE_LOAD_TIMEOUT', 60000))
    ELEMENT_WAIT_TIMEOUT = int(os.getenv('ELEMENT_WAIT_TIMEOUT', 5000))

    # ===========================================
    # SOURCE URLs (configurable for flexibility)
    # ===========================================
    SERIEN_BASE_URL = os.getenv('SERIEN_BASE_URL', 'http://186.2.175.5')
    ANIME_BASE_URL = os.getenv('ANIME_BASE_URL', 'https://aniworld.to')

    # ===========================================
    # FLASK SETTINGS
    # ===========================================

    # Session
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'False').lower() == 'true'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = 86400  # 24 hours

    # File Upload
    MAX_CONTENT_LENGTH = int(os.getenv('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))  # 16MB

    # CORS
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', 'http://localhost:5000')


class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    TESTING = False
    SESSION_COOKIE_SECURE = False


class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = True


class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
