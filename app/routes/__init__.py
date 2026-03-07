"""Routes module"""
from .settings_routes import settings_bp
from .catalog_routes import catalog_bp
from .library_routes import library_bp

__all__ = ['settings_bp', 'catalog_bp', 'library_bp']
