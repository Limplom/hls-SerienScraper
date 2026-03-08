"""
Series Catalog Module
Handles scraping and caching of catalogs from multiple sources.

Optimized with parallel scraping for faster catalog updates.
"""

import json
import os
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from playwright.async_api import async_playwright
import re
import asyncio

logger = logging.getLogger(__name__)

# Fix Windows console encoding for emojis
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from app.config import Config, PROJECT_ROOT

# Cache files - use PROJECT_ROOT for consistent paths regardless of CWD
CATALOG_CACHE_FILE = PROJECT_ROOT / "series_cache" / "catalog_index.json"
ANIME_CACHE_FILE = PROJECT_ROOT / "series_cache" / "anime_catalog_index.json"
CATALOG_MAX_AGE_HOURS = 24


def _get_sources() -> Dict:
    """Get source definitions with configurable URLs."""
    return {
        'series': {
            'base_url': Config.SERIEN_BASE_URL,
            'catalog_path': '/serien',
            'content_path': '/serie/stream/',
            'name': 'Serien',
            'cache_file': CATALOG_CACHE_FILE
        },
        'anime': {
            'base_url': Config.ANIME_BASE_URL,
            'catalog_path': '/animes',
            'content_path': '/anime/stream/',
            'name': 'Anime',
            'cache_file': ANIME_CACHE_FILE
        }
    }


# For backwards compatibility
SOURCES = _get_sources()


async def scrape_catalog(source='series', max_workers=3) -> Dict:
    """
    Scrapes catalog page to get all content organized by genre
    OPTIMIZED with parallel processing for faster scraping

    Args:
        source: 'series' or 'anime'
        max_workers: Number of parallel browser contexts (default: 3)

    Returns:
    {
      'source': 'series',
      'genres': {
        'Abenteuer': [...],
        'Action': [...]
      },
      'total_items': 1234,
      'last_updated': '2024-12-24T17:00:00'
    }
    """
    if source not in SOURCES:
        raise ValueError(f"Unknown source: {source}")

    src_config = SOURCES[source]
    base_url = src_config['base_url']
    catalog_path = src_config['catalog_path']
    content_path = src_config['content_path']

    logger.info(f"Scraping {src_config['name']} catalog from {base_url}{catalog_path}...")
    logger.info(f"Using {max_workers} parallel workers for faster processing")

    # Use genre sorting parameter to get old structure
    catalog_url = f"{base_url}{catalog_path}?by=genre"

    async with async_playwright() as p:
        # Use headless mode for better performance
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--mute-audio',
                '--disable-gpu',
                '--disable-dev-shm-usage',
                '--ignore-certificate-errors'
            ]
        )

        try:
            # Step 1: Get HTML structure with single page
            logger.info("Fetching page HTML...")
            context = await browser.new_context(
                ignore_https_errors=True,
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            page = await context.new_page()
            page.set_default_timeout(30000)

            await page.goto(catalog_url, wait_until='domcontentloaded', timeout=30000)

            # Wait for the series list to load (works for both genre and alphabetic views)
            await page.wait_for_selector('ul.series-list', timeout=10000)
            logger.info("Page loaded successfully")

            # Get HTML for parallel processing
            html_content = await page.content()
            await context.close()

            # Step 2: Extract data using JavaScript evaluation (faster than querying)
            # The new structure uses h3.h5 headers for both genre and alphabetic views
            logger.info("Extracting catalog structure...")
            context2 = await browser.new_context()
            try:
                page2 = await context2.new_page()
                await page2.set_content(html_content)

                # Extract using the new unified structure (h3.h5 + ul.series-list)
                genres_raw = await page2.evaluate('''
                    () => {
                        const results = [];
                        const headers = document.querySelectorAll('h3.h5');

                        headers.forEach(header => {
                            const categoryName = header.textContent.trim();

                            // Find the corresponding series list
                            const seriesList = header.parentElement.nextElementSibling;
                            if (!seriesList || !seriesList.classList.contains('series-list')) return;

                            const links = seriesList.querySelectorAll('li.series-item a');
                            const series = [];

                            links.forEach(link => {
                                series.push({
                                    name: link.textContent.trim(),
                                    href: link.getAttribute('href'),
                                    alternative_titles: link.parentElement.getAttribute('data-search') || ''
                                });
                            });

                            if (series.length > 0) {
                                results.push({ genre: categoryName, series: series });
                            }
                        });

                        return results;
                    }
                ''')
            finally:
                await context2.close()

            # Step 3: Process extracted data in parallel batches
            logger.info(f"Found {len(genres_raw)} genre categories")
            logger.info("Processing series data in parallel...")

            genres_data = {}
            total_count = 0

            # Process all genres in parallel
            async def process_series_item(series_raw):
                try:
                    # Extract slug from href (format: /serie/slug or /anime/slug)
                    # Updated pattern to match both old (/serie/stream/slug) and new (/serie/slug) formats
                    slug_match = re.search(r'/(?:serie|anime)(?:/stream)?/([^/\s]+)', series_raw['href'])
                    if not slug_match:
                        return None

                    return {
                        'name': series_raw['name'],
                        'slug': slug_match.group(1),
                        'url': series_raw['href'],
                        'alternative_titles': series_raw['alternative_titles'],
                        'source': source
                    }
                except Exception:
                    return None

            # Process each genre
            for genre_data in genres_raw:
                genre_name = genre_data['genre']
                series_raw_list = genre_data['series']

                # Process all series in this genre in parallel
                tasks = [process_series_item(s) for s in series_raw_list]
                processed_series = await asyncio.gather(*tasks)

                # Filter out None results
                series_list = [s for s in processed_series if s is not None]

                if series_list:
                    genres_data[genre_name] = series_list
                    total_count += len(series_list)
                    logger.info(f"{genre_name}: {len(series_list)} items")

        finally:
            await browser.close()

    if not genres_data or total_count == 0:
        raise ValueError(f"Catalog scrape returned empty data for {source} - page may not have loaded correctly")

    catalog = {
        'source': source,
        'source_name': src_config['name'],
        'base_url': base_url,
        'genres': genres_data,
        'total_items': total_count,
        'last_updated': datetime.now().isoformat()
    }

    logger.info(f"Catalog scraped: {total_count} items across {len(genres_data)} genres")
    return catalog


# Backwards compatibility wrapper
async def scrape_series_catalog(base_url="http://186.2.175.5") -> Dict:
    """Legacy function - redirects to scrape_catalog"""
    return await scrape_catalog('series')


def load_catalog_cache(source='series') -> Optional[Dict]:
    """Load cached catalog index for specific source"""
    if source not in SOURCES:
        return None

    cache_file = SOURCES[source]['cache_file']

    if not cache_file.exists():
        return None

    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Validate structure
        if 'genres' not in data or 'last_updated' not in data:
            return None

        return data

    except Exception as e:
        logger.warning(f"Error loading {source} catalog cache: {e}")
        return None


def save_catalog_cache(catalog_data: Dict, source='series') -> bool:
    """Save catalog to cache with timestamp"""
    if source not in SOURCES:
        return False

    cache_file = SOURCES[source]['cache_file']

    try:
        # Ensure cache directory exists
        cache_file.parent.mkdir(exist_ok=True)

        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(catalog_data, f, ensure_ascii=False, separators=(',', ':'))

        logger.info(f"{source.capitalize()} catalog saved to cache: {cache_file}")
        return True

    except Exception as e:
        logger.error(f"Error saving {source} catalog cache: {e}")
        return False


def is_catalog_stale(source='series', max_age_hours=CATALOG_MAX_AGE_HOURS) -> bool:
    """Check if catalog needs refresh based on file mtime (avoids loading entire file)"""
    if source not in SOURCES:
        return True

    cache_file = SOURCES[source]['cache_file']
    if not cache_file.exists():
        return True

    try:
        file_age_seconds = datetime.now().timestamp() - cache_file.stat().st_mtime
        max_age_seconds = max_age_hours * 3600
        is_stale = file_age_seconds > max_age_seconds

        if is_stale:
            logger.info(f"{source.capitalize()} catalog is {file_age_seconds / 3600:.1f} hours old (max: {max_age_hours}h)")

        return is_stale

    except Exception as e:
        logger.warning(f"Error checking {source} catalog age: {e}")
        return True


def get_catalog_stats(source='series') -> Dict:
    """Get statistics about the cached catalog"""
    catalog = load_catalog_cache(source)
    if not catalog:
        return {
            'cached': False,
            'source': source,
            'total_items': 0,
            'total_genres': 0,
            'last_updated': None
        }

    return {
        'cached': True,
        'source': source,
        'source_name': catalog.get('source_name', source.capitalize()),
        'total_items': catalog.get('total_items', 0),
        'total_genres': len(catalog.get('genres', {})),
        'last_updated': catalog.get('last_updated'),
        'is_stale': is_catalog_stale(source)
    }


def get_all_sources() -> Dict:
    """Get list of all available sources"""
    return {
        source_id: {
            'name': config['name'],
            'base_url': config['base_url']
        }
        for source_id, config in SOURCES.items()
    }
