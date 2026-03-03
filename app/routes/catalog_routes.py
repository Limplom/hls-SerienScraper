"""
Catalog Routes
Handles series/anime catalog operations
"""
from flask import Blueprint, request, jsonify
import asyncio
import logging

logger = logging.getLogger(__name__)
from app.series_catalog import (
    scrape_catalog,
    load_catalog_cache,
    save_catalog_cache,
    is_catalog_stale,
    get_catalog_stats,
    get_all_sources
)


catalog_bp = Blueprint('catalog', __name__)


@catalog_bp.route('/api/catalog/sources', methods=['GET'])
def get_sources():
    """Get all available catalog sources"""
    sources = get_all_sources()
    return jsonify({'sources': sources})


@catalog_bp.route('/api/catalog', methods=['GET'])
def get_catalog():
    """
    Returns catalog for specified source
    Query params:
      - source: 'series' or 'anime' (default: series)
      - force_refresh: bool (default: false)

    Response:
    {
      'source': 'series',
      'genres': {...},
      'total_items': 1234,
      'last_updated': '...',
      'from_cache': true/false
    }
    """
    source = request.args.get('source', 'series')
    force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'

    # Try cache first unless force refresh
    if not force_refresh:
        cached = load_catalog_cache(source)
        if cached and not is_catalog_stale(source):
            logger.info(f"Using cached {source} catalog")
            return jsonify({**cached, 'from_cache': True})

    # Scrape fresh catalog
    logger.info(f"Scraping fresh {source} catalog...")
    try:
        loop = asyncio.new_event_loop()
        try:
            catalog_data = loop.run_until_complete(scrape_catalog(source))
        finally:
            loop.close()


        # Save to cache
        save_catalog_cache(catalog_data, source)

        return jsonify({**catalog_data, 'from_cache': False})

    except Exception as e:
        logger.error(f"Error scraping {source} catalog: {e}")
        return jsonify({'error': str(e)}), 500


@catalog_bp.route('/api/catalog/search', methods=['GET'])
def search_catalog():
    """
    Search content by name, alternative title, or genre
    Query params:
      - source: 'series' or 'anime' (default: series)
      - q: search query
      - genre: filter by genre (optional)

    Response:
    {
      'results': [
        {'name': '...', 'slug': '...', 'genre': '...', 'url': '...', 'source': '...'},
        ...
      ],
      'total': 42
    }
    """
    source = request.args.get('source', 'series')
    query = request.args.get('q', '').lower().strip()
    genre_filter = request.args.get('genre', None)

    catalog = load_catalog_cache(source)
    if not catalog:
        return jsonify({'error': f'{source.capitalize()} catalog not loaded. Call /api/catalog?source={source} first.'}), 404

    # Search logic: match name or alternative_titles
    results = []
    for genre, series_list in catalog['genres'].items():
        if genre_filter and genre != genre_filter:
            continue

        for series in series_list:
            if (query in series['name'].lower() or
                query in series.get('alternative_titles', '').lower()):
                results.append({**series, 'genre': genre})

    return jsonify({'results': results, 'total': len(results)})


@catalog_bp.route('/api/catalog/stats', methods=['GET'])
def get_catalog_stats_route():
    """Get catalog statistics for a source"""
    source = request.args.get('source', 'series')
    stats = get_catalog_stats(source)
    return jsonify(stats)
