"""
Cache Manager
Advanced caching system for series metadata, cover images, and HTTP responses
"""
import os
import json
import hashlib
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import requests
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class CacheManager:
    """Enhanced caching system with episode-level caching and cover image storage"""

    # Shared requests session for connection pooling
    _session = None

    @classmethod
    def _get_session(cls) -> requests.Session:
        if cls._session is None:
            cls._session = requests.Session()
            cls._session.headers.update({'User-Agent': 'Mozilla/5.0'})
        return cls._session

    def __init__(self, cache_dir: str = './cache'):
        """
        Initialize cache manager

        Args:
            cache_dir: Base directory for all cache storage
        """
        self.cache_dir = Path(cache_dir)
        self.images_dir = self.cache_dir / 'images'
        self.metadata_dir = self.cache_dir / 'metadata'
        self.http_cache_dir = self.cache_dir / 'http_responses'

        # Create cache directories
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.http_cache_dir.mkdir(parents=True, exist_ok=True)

        # In-memory cache for hot data (frequently accessed)
        self._hot_cache: Dict[str, Dict[str, Any]] = {}
        self._hot_cache_max_size = 100  # Max items in hot cache

        # Default TTLs
        self.default_ttl = {
            'metadata': 7 * 24 * 3600,      # 7 days for series metadata
            'episode': 30 * 24 * 3600,       # 30 days for episode data
            'cover_image': 90 * 24 * 3600,   # 90 days for cover images
            'http_response': 24 * 3600       # 24 hours for HTTP responses
        }

    def _get_cache_key(self, identifier: str) -> str:
        """Generate cache key from identifier"""
        return hashlib.md5(identifier.encode('utf-8')).hexdigest()

    def _is_expired(self, cache_file: Path, ttl: int) -> bool:
        """Check if cache file is expired"""
        if not cache_file.exists():
            return True

        file_age = time.time() - cache_file.stat().st_mtime
        return file_age > ttl

    # ==========================================
    # Episode-level caching
    # ==========================================

    def cache_episode(self, series_slug: str, season: int, episode: int, data: Dict[str, Any]):
        """
        Cache episode metadata

        Args:
            series_slug: Series identifier
            season: Season number
            episode: Episode number
            data: Episode metadata to cache
        """
        episode_key = f"{series_slug}_S{season:02d}E{episode:02d}"
        cache_file = self.metadata_dir / f"{episode_key}.json"

        cache_data = {
            'cached_at': datetime.now().isoformat(),
            'series_slug': series_slug,
            'season': season,
            'episode': episode,
            'data': data
        }

        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False)

    def get_cached_episode(self, series_slug: str, season: int, episode: int) -> Optional[Dict[str, Any]]:
        """
        Get cached episode metadata

        Args:
            series_slug: Series identifier
            season: Season number
            episode: Episode number

        Returns:
            Cached episode data or None if not cached/expired
        """
        episode_key = f"{series_slug}_S{season:02d}E{episode:02d}"
        cache_file = self.metadata_dir / f"{episode_key}.json"

        # Check hot cache first
        if episode_key in self._hot_cache:
            hot_data = self._hot_cache[episode_key]
            if not self._is_expired(cache_file, self.default_ttl['episode']):
                return hot_data.get('data')

        # Check disk cache
        if self._is_expired(cache_file, self.default_ttl['episode']):
            return None

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            # Add to hot cache
            self._add_to_hot_cache(episode_key, cache_data)

            return cache_data.get('data')
        except Exception:
            return None

    # ==========================================
    # Cover image caching
    # ==========================================

    def cache_cover_image(self, image_url: str, image_data: bytes) -> str:
        """
        Cache cover image locally

        Args:
            image_url: Original image URL
            image_data: Image bytes

        Returns:
            Local file path to cached image
        """
        # Generate filename from URL
        url_hash = self._get_cache_key(image_url)
        extension = self._get_image_extension(image_url)
        image_file = self.images_dir / f"{url_hash}{extension}"

        # Save image
        with open(image_file, 'wb') as f:
            f.write(image_data)

        return str(image_file)

    def get_cached_cover_image(self, image_url: str) -> Optional[str]:
        """
        Get cached cover image path

        Args:
            image_url: Original image URL

        Returns:
            Local file path or None if not cached/expired
        """
        url_hash = self._get_cache_key(image_url)

        # Try different extensions
        for ext in ['.jpg', '.jpeg', '.png', '.webp']:
            image_file = self.images_dir / f"{url_hash}{ext}"
            if not self._is_expired(image_file, self.default_ttl['cover_image']):
                return str(image_file)

        return None

    def download_and_cache_image(self, image_url: str) -> Optional[str]:
        """
        Download and cache image from URL

        Args:
            image_url: Image URL to download

        Returns:
            Local file path or None on error
        """
        # Check cache first
        cached_path = self.get_cached_cover_image(image_url)
        if cached_path:
            return cached_path

        # Download image
        try:
            response = self._get_session().get(image_url, timeout=10)
            if response.status_code == 200:
                return self.cache_cover_image(image_url, response.content)
        except Exception:
            pass

        return None

    def _get_image_extension(self, url: str) -> str:
        """Extract image extension from URL"""
        parsed = urlparse(url)
        path = parsed.path.lower()

        if path.endswith('.jpg') or path.endswith('.jpeg'):
            return '.jpg'
        elif path.endswith('.png'):
            return '.png'
        elif path.endswith('.webp'):
            return '.webp'
        else:
            return '.jpg'  # Default

    # ==========================================
    # HTTP response caching
    # ==========================================

    def cache_http_response(self, url: str, response_data: Any, ttl: Optional[int] = None):
        """
        Cache HTTP response

        Args:
            url: Request URL
            response_data: Response data (JSON-serializable)
            ttl: Time-to-live in seconds (default: 24 hours)
        """
        url_hash = self._get_cache_key(url)
        cache_file = self.http_cache_dir / f"{url_hash}.json"

        cache_data = {
            'cached_at': datetime.now().isoformat(),
            'url': url,
            'ttl': ttl or self.default_ttl['http_response'],
            'data': response_data
        }

        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False)

    def get_cached_http_response(self, url: str) -> Optional[Any]:
        """
        Get cached HTTP response

        Args:
            url: Request URL

        Returns:
            Cached response data or None if not cached/expired
        """
        url_hash = self._get_cache_key(url)
        cache_file = self.http_cache_dir / f"{url_hash}.json"

        if not cache_file.exists():
            return None

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            # Check TTL
            ttl = cache_data.get('ttl', self.default_ttl['http_response'])
            cached_at = datetime.fromisoformat(cache_data['cached_at'])
            age = (datetime.now() - cached_at).total_seconds()

            if age > ttl:
                return None

            return cache_data.get('data')
        except Exception:
            return None

    # ==========================================
    # Hot cache (in-memory) management
    # ==========================================

    def _add_to_hot_cache(self, key: str, data: Dict[str, Any]):
        """Add item to hot cache with LRU eviction"""
        # Move to end if already exists (mark as recently used)
        if key in self._hot_cache:
            self._hot_cache.pop(key)
        elif len(self._hot_cache) >= self._hot_cache_max_size:
            # Evict oldest (first) item
            oldest_key = next(iter(self._hot_cache))
            del self._hot_cache[oldest_key]

        self._hot_cache[key] = data

    def get_hot_cache_item(self, key: str) -> Optional[Any]:
        """Get item from hot cache (moves to end for LRU)"""
        if key in self._hot_cache:
            # Move to end (recently accessed)
            self._hot_cache[key] = self._hot_cache.pop(key)
            return self._hot_cache[key]
        return None

    def clear_hot_cache(self):
        """Clear in-memory hot cache"""
        self._hot_cache.clear()

    # ==========================================
    # Cache warming
    # ==========================================

    def warm_popular_series(self, series_list: list):
        """
        Pre-load popular series into hot cache

        Args:
            series_list: List of popular series slugs to warm
        """
        logger.info(f"Warming cache for {len(series_list)} popular series...")

        for series_slug in series_list:
            # Load metadata files for this series
            pattern = f"{series_slug}_S*E*.json"
            for metadata_file in self.metadata_dir.glob(pattern):
                try:
                    with open(metadata_file, 'r', encoding='utf-8') as f:
                        cache_data = json.load(f)

                    episode_key = metadata_file.stem
                    self._add_to_hot_cache(episode_key, cache_data)
                except Exception:
                    continue

        logger.info(f"Cache warmed with {len(self._hot_cache)} items")

    # ==========================================
    # Cache statistics
    # ==========================================

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        stats = {
            'images': {
                'count': len(list(self.images_dir.glob('*'))),
                'size_mb': self._get_dir_size(self.images_dir) / (1024 * 1024)
            },
            'metadata': {
                'count': len(list(self.metadata_dir.glob('*.json'))),
                'size_mb': self._get_dir_size(self.metadata_dir) / (1024 * 1024)
            },
            'http_responses': {
                'count': len(list(self.http_cache_dir.glob('*.json'))),
                'size_mb': self._get_dir_size(self.http_cache_dir) / (1024 * 1024)
            },
            'hot_cache': {
                'count': len(self._hot_cache),
                'max_size': self._hot_cache_max_size
            },
            'total_size_mb': self._get_dir_size(self.cache_dir) / (1024 * 1024)
        }

        return stats

    def _get_dir_size(self, directory: Path) -> int:
        """Get total size of directory in bytes"""
        total_size = 0
        for file in directory.rglob('*'):
            if file.is_file():
                total_size += file.stat().st_size
        return total_size

    # ==========================================
    # Cache cleanup
    # ==========================================

    def cleanup_expired(self):
        """Remove expired cache entries"""
        removed_count = 0

        # Cleanup metadata
        for cache_file in self.metadata_dir.glob('*.json'):
            try:
                if self._is_expired(cache_file, self.default_ttl['episode']):
                    cache_file.unlink(missing_ok=True)
                    removed_count += 1
            except OSError:
                pass

        # Cleanup images
        for image_file in self.images_dir.glob('*'):
            try:
                if self._is_expired(image_file, self.default_ttl['cover_image']):
                    image_file.unlink(missing_ok=True)
                    removed_count += 1
            except OSError:
                pass

        # Cleanup HTTP responses
        for cache_file in self.http_cache_dir.glob('*.json'):
            try:
                if self._is_expired(cache_file, self.default_ttl['http_response']):
                    cache_file.unlink(missing_ok=True)
                    removed_count += 1
            except OSError:
                pass

        logger.info(f"Cleaned up {removed_count} expired cache entries")
        return removed_count

    def clear_all(self):
        """Clear all caches"""
        # Clear disk caches
        for cache_file in self.images_dir.glob('*'):
            cache_file.unlink(missing_ok=True)
        for cache_file in self.metadata_dir.glob('*.json'):
            cache_file.unlink(missing_ok=True)
        for cache_file in self.http_cache_dir.glob('*.json'):
            cache_file.unlink(missing_ok=True)

        # Clear hot cache
        self.clear_hot_cache()

        logger.info("All caches cleared")


# Global cache manager instance
_cache_manager: Optional[CacheManager] = None


def get_cache_manager() -> CacheManager:
    """Get global cache manager instance"""
    global _cache_manager
    if _cache_manager is None:
        from app.config import PROJECT_ROOT
        _cache_manager = CacheManager(cache_dir=str(PROJECT_ROOT / 'cache'))
    return _cache_manager
