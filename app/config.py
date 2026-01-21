"""
Configuration for HLS Downloader - Production Ready
Supports environment variables for Docker deployment.
"""
import os
import json
from pathlib import Path

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
            print(f"⚠️ Error loading settings.json: {e}")
    return {}

# Load JSON settings
_json_settings = _load_settings_json()


class Config:
    """Base configuration"""
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

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

    DOWNLOAD_PATH = property(lambda self: Config.get_download_path())

    # Max parallel downloads
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

    # Auto retry on failure
    AUTO_RETRY = os.getenv(
        'HLS_AUTO_RETRY',
        str(_json_settings.get('auto_retry', True))
    ).lower() in ('true', '1', 'yes')

    # Max retry attempts
    MAX_RETRY_ATTEMPTS = int(os.getenv(
        'HLS_MAX_RETRIES',
        _json_settings.get('max_retry_attempts', 3)
    ))

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
    # FLASK / DATABASE SETTINGS
    # ===========================================

    # Database
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///hls_downloader.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Redis
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

    # Celery
    CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/1')
    CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/2')
    CELERY_TASK_SERIALIZER = 'json'
    CELERY_RESULT_SERIALIZER = 'json'
    CELERY_ACCEPT_CONTENT = ['json']
    CELERY_TIMEZONE = 'Europe/Berlin'
    CELERY_TASK_TRACK_STARTED = True
    CELERY_TASK_TIME_LIMIT = 3600  # 1 hour max per task

    # Rate Limiting
    RATELIMIT_STORAGE_URL = os.getenv('RATELIMIT_STORAGE_URL', 'redis://localhost:6379/3')
    RATELIMIT_STRATEGY = 'fixed-window'
    MAX_DOWNLOADS_PER_USER = int(os.getenv('MAX_DOWNLOADS_PER_USER', 3))
    MAX_DOWNLOADS_PER_HOUR = int(os.getenv('MAX_DOWNLOADS_PER_HOUR', 20))

    # Browser Pool
    MAX_CONCURRENT_BROWSERS = int(os.getenv('MAX_CONCURRENT_BROWSERS', 10))
    BROWSER_TIMEOUT = int(os.getenv('BROWSER_TIMEOUT', 300))

    # Session
    SESSION_TYPE = 'redis'
    SESSION_REDIS = None  # Will be set at runtime
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'False').lower() == 'true'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = 86400  # 24 hours

    # File Upload
    MAX_CONTENT_LENGTH = int(os.getenv('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))  # 16MB

    # SocketIO
    SOCKETIO_MESSAGE_QUEUE = None  # Will be set at runtime
    SOCKETIO_CHANNEL = 'hls-downloader'


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
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
