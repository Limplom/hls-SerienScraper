"""
Series Data Caching Module
Caches scraped series data to avoid redundant scraping operations
"""

import json
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

from app.config import PROJECT_ROOT

# Cache configuration - use PROJECT_ROOT for consistent paths regardless of CWD
CACHE_DIR = PROJECT_ROOT / "series_cache"
CACHE_EXPIRY_DAYS = 7

# Ensure cache directory exists on module load
CACHE_DIR.mkdir(exist_ok=True)


def get_cache_path(series_slug: str) -> Path:
    """
    Get the file path for a series cache file

    Args:
        series_slug: The series identifier (e.g., 'invincible')

    Returns:
        Path object pointing to the cache file
    """
    return CACHE_DIR / f"{series_slug}.json"


def load_from_cache(series_slug: str) -> Optional[Dict[str, Any]]:
    """
    Load series data from cache if it exists and is valid

    Args:
        series_slug: The series identifier

    Returns:
        Cached data dictionary if valid, None otherwise
    """
    cache_file = get_cache_path(series_slug)

    if not cache_file.exists():
        return None

    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Check if cache has expired
        if not is_cache_valid(data):
            # Delete expired cache file
            cache_file.unlink()
            return None

        return data
    except (json.JSONDecodeError, KeyError, Exception):
        # If cache is corrupted or unreadable, delete it
        try:
            cache_file.unlink()
        except Exception:
            pass
        return None


def is_cache_valid(cache_data: Dict[str, Any]) -> bool:
    """
    Check if cached data has not expired

    Args:
        cache_data: The cached data dictionary with 'expires_at' field

    Returns:
        True if cache is still valid, False otherwise
    """
    try:
        expires_at = datetime.fromisoformat(cache_data['expires_at'])
        return datetime.now() < expires_at
    except (KeyError, ValueError):
        return False


def save_to_cache(series_slug: str, data: Dict[str, Any]) -> bool:
    """
    Save scraped series data to cache

    Args:
        series_slug: The series identifier
        data: The scraped data to cache (must include series_name, cover_url, etc.)

    Returns:
        True if saved successfully, False otherwise
    """
    try:
        # Build cache entry with metadata
        cache_data = {
            "series_slug": series_slug,
            "cached_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(days=CACHE_EXPIRY_DAYS)).isoformat(),
            **data  # Include all scraped data
        }

        cache_file = get_cache_path(series_slug)

        # Write to cache file with pretty formatting
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)

        return True
    except Exception as e:
        logger.error(f"Error saving cache for {series_slug}: {e}")
        return False


def clear_cache(series_slug: Optional[str] = None) -> int:
    """
    Clear cached series data

    Args:
        series_slug: Specific series to clear, or None to clear all

    Returns:
        Number of cache files deleted
    """
    deleted_count = 0

    try:
        if not CACHE_DIR.exists():
            return 0

        if series_slug:
            # Clear specific series cache
            cache_file = get_cache_path(series_slug)
            if cache_file.exists():
                cache_file.unlink()
                deleted_count = 1
        else:
            # Clear all cache files
            for cache_file in CACHE_DIR.glob("*.json"):
                try:
                    cache_file.unlink()
                    deleted_count += 1
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")

    return deleted_count


def cleanup_expired_cache() -> int:
    """
    Remove all expired cache files

    Returns:
        Number of expired cache files deleted
    """
    deleted_count = 0

    try:
        if not CACHE_DIR.exists():
            return 0

        for cache_file in CACHE_DIR.glob("*.json"):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                if not is_cache_valid(data):
                    cache_file.unlink()
                    deleted_count += 1
            except Exception:
                # Delete corrupted cache files
                try:
                    cache_file.unlink()
                    deleted_count += 1
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Error cleaning up cache: {e}")

    return deleted_count


def get_cache_stats() -> Dict[str, Any]:
    """
    Get statistics about the cache

    Returns:
        Dictionary with cache statistics including ongoing/completed counts
    """
    try:
        if not CACHE_DIR.exists():
            return {
                "total_cached": 0,
                "valid_cached": 0,
                "expired_cached": 0,
                "ongoing_series": 0,
                "completed_series": 0,
                "cache_size_mb": 0
            }

        total = 0
        valid = 0
        expired = 0
        ongoing = 0
        completed = 0
        total_size = 0

        for cache_file in CACHE_DIR.glob("*.json"):
            # Skip catalog index files
            if cache_file.name in ['catalog_index.json', 'anime_catalog_index.json']:
                continue

            try:
                total += 1
                total_size += cache_file.stat().st_size

                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                if is_cache_valid(data):
                    valid += 1
                else:
                    expired += 1

                # Count ongoing vs completed
                if data.get('is_ongoing', True):
                    ongoing += 1
                else:
                    completed += 1

            except Exception:
                expired += 1

        return {
            "total_cached": total,
            "valid_cached": valid,
            "expired_cached": expired,
            "ongoing_series": ongoing,
            "completed_series": completed,
            "cache_size_mb": round(total_size / (1024 * 1024), 2)
        }
    except Exception:
        return {
            "total_cached": 0,
            "valid_cached": 0,
            "expired_cached": 0,
            "ongoing_series": 0,
            "completed_series": 0,
            "cache_size_mb": 0
        }


def get_series_needing_update(limit: int = 10, min_age_days: float = 5.0, include_completed: bool = False) -> List[Dict[str, Any]]:
    """
    Get list of cached series that need updating (near expiration or expired).
    Prioritizes series that are closest to expiring or already expired.

    Completed series (is_ongoing=False) are skipped by default since they won't
    receive new episodes.

    Args:
        limit: Maximum number of series to return
        min_age_days: Minimum age in days before considering for update (default 5 days)
        include_completed: If True, also include completed series (default False)

    Returns:
        List of dicts with series_slug, cached_at, expires_at, priority_score
    """
    try:
        if not CACHE_DIR.exists():
            return []

        candidates = []
        now = datetime.now()
        min_age_threshold = now - timedelta(days=min_age_days)
        skipped_completed = 0

        for cache_file in CACHE_DIR.glob("*.json"):
            # Skip catalog index files
            if cache_file.name in ['catalog_index.json', 'anime_catalog_index.json']:
                continue

            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                series_slug = data.get('series_slug', cache_file.stem)

                # Skip completed series unless explicitly requested
                is_ongoing = data.get('is_ongoing', True)
                if not include_completed and not is_ongoing:
                    skipped_completed += 1
                    continue

                cached_at_str = data.get('cached_at')
                expires_at_str = data.get('expires_at')

                if not cached_at_str or not expires_at_str:
                    # No timestamp data - high priority for update
                    candidates.append({
                        'series_slug': series_slug,
                        'cached_at': None,
                        'expires_at': None,
                        'priority_score': 1000,  # Highest priority
                        'age_days': None,
                        'status': 'missing_metadata',
                        'is_ongoing': is_ongoing
                    })
                    continue

                cached_at = datetime.fromisoformat(cached_at_str)
                expires_at = datetime.fromisoformat(expires_at_str)

                # Only include if older than min_age_days
                if cached_at > min_age_threshold:
                    continue

                # Calculate priority score (higher = more urgent)
                # Expired: high priority, near expiry: medium, recent: low
                time_until_expiry = (expires_at - now).total_seconds()
                age_days = (now - cached_at).total_seconds() / 86400

                if time_until_expiry <= 0:
                    # Already expired
                    priority_score = 500 + abs(time_until_expiry / 3600)  # Higher the longer expired
                    status = 'expired'
                elif time_until_expiry < 86400:  # Less than 1 day
                    priority_score = 300 + (86400 - time_until_expiry) / 3600
                    status = 'expiring_soon'
                elif time_until_expiry < 172800:  # Less than 2 days
                    priority_score = 100 + (172800 - time_until_expiry) / 3600
                    status = 'expiring'
                else:
                    priority_score = age_days
                    status = 'valid'

                candidates.append({
                    'series_slug': series_slug,
                    'cached_at': cached_at_str,
                    'expires_at': expires_at_str,
                    'priority_score': priority_score,
                    'age_days': round(age_days, 1),
                    'status': status,
                    'is_ongoing': is_ongoing
                })

            except Exception:
                # Corrupted file - add with high priority
                candidates.append({
                    'series_slug': cache_file.stem,
                    'cached_at': None,
                    'expires_at': None,
                    'priority_score': 900,
                    'age_days': None,
                    'status': 'corrupted',
                    'is_ongoing': True  # Assume ongoing if unknown
                })

        if skipped_completed > 0:
            logger.info(f"Skipped {skipped_completed} completed series (no new episodes expected)")

        # Sort by priority (highest first) and return top N
        candidates.sort(key=lambda x: x['priority_score'], reverse=True)
        return candidates[:limit]

    except Exception as e:
        logger.error(f"Error getting series needing update: {e}")
        return []


def get_uncached_series_from_catalog() -> List[str]:
    """
    Compare catalog with cache to find series that haven't been cached yet.

    Returns:
        List of series slugs that are in catalog but not in cache
    """
    try:
        # Load catalog indexes
        catalog_files = [
            CACHE_DIR / 'catalog_index.json',
            CACHE_DIR / 'anime_catalog_index.json'
        ]

        all_catalog_slugs = set()

        for catalog_file in catalog_files:
            if catalog_file.exists():
                try:
                    with open(catalog_file, 'r', encoding='utf-8') as f:
                        catalog = json.load(f)

                    genres = catalog.get('genres', {})
                    for genre_series in genres.values():
                        for series in genre_series:
                            slug = series.get('slug')
                            if slug:
                                all_catalog_slugs.add(slug)
                except Exception:
                    pass

        if not all_catalog_slugs:
            return []

        # Get cached series
        cached_slugs = set()
        if CACHE_DIR.exists():
            for cache_file in CACHE_DIR.glob("*.json"):
                if cache_file.name not in ['catalog_index.json', 'anime_catalog_index.json']:
                    cached_slugs.add(cache_file.stem)

        # Return uncached series
        uncached = list(all_catalog_slugs - cached_slugs)
        return uncached

    except Exception as e:
        logger.error(f"Error finding uncached series: {e}")
        return []
