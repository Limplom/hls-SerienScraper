#!/usr/bin/env python3
"""
HLS Downloader - Web GUI
Modern web interface for the HLS video downloader
"""

from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, emit
import asyncio
import threading
import queue
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, Future
from threading import Event, RLock
from typing import Dict, Optional, Set
import traceback
import atexit
import signal
import logging
from app.hls_downloader_final import (
    HLSExtractor,
    parse_episode_range,
    VideoMetadata,
    parse_flexible_url,
    detect_series_info
)
from app.series_cache import (
    load_from_cache,
    save_to_cache,
    get_series_needing_update,
    get_uncached_series_from_catalog,
    get_cache_stats as get_series_cache_stats
)
from app.download_queue import DownloadQueueManager, DownloadStatus
from app.series_catalog import (
    scrape_catalog,
    scrape_series_catalog,
    load_catalog_cache,
    save_catalog_cache,
    is_catalog_stale,
    get_catalog_stats,
    get_all_sources
)
from app.config import Config
from app.file_verification import FileVerifier, format_duration, format_file_size
from app.services.cache_manager import get_cache_manager
import subprocess
import re
import argparse
import time
import uuid

logger = logging.getLogger(__name__)

# Get the project root directory (parent of 'app' folder)
project_root = Path(__file__).parent.parent

# Load config
app_config = Config()

# Create Flask app with correct template and static folders
app = Flask(__name__,
            template_folder=str(project_root / 'templates'),
            static_folder=str(project_root / 'static'))

# Filter Flask request logs to reduce console spam
# Only suppress repetitive polling requests, show important requests
class RequestFilter(logging.Filter):
    """Filter out repetitive API polling requests to reduce console clutter."""

    IGNORED_PATHS = {
        '/api/queue',
        '/api/queue/duplicates',
    }

    def filter(self, record):
        # Check if this is a request log message
        if hasattr(record, 'getMessage'):
            msg = record.getMessage()
            # Suppress logs for ignored paths
            for path in self.IGNORED_PATHS:
                if path in msg and ('GET' in msg or 'POST' in msg):
                    return False
        return True

log = logging.getLogger('werkzeug')
log.addFilter(RequestFilter())

# Security: Use environment variable for secret key, generate random default for development
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', os.urandom(24).hex())

# CORS configuration for SocketIO
# For local development, allow all origins. For production, set CORS_ORIGINS env variable.
if os.getenv('CORS_ORIGINS'):
    ALLOWED_ORIGINS = os.getenv('CORS_ORIGINS').split(',')
else:
    # Local development - allow all origins
    ALLOWED_ORIGINS = "*"
socketio = SocketIO(app, cors_allowed_origins=ALLOWED_ORIGINS, async_mode='threading')

# Register blueprints
from app.routes import settings_bp, catalog_bp
app.register_blueprint(settings_bp)
app.register_blueprint(catalog_bp)

# Thread-safe wrapper for active_downloads dict
class ThreadSafeDict:
    """Thread-safe dictionary wrapper that automatically handles locking."""

    def __init__(self):
        self._data = {}
        self._lock = threading.RLock()  # RLock allows nested access from same thread

    def __getitem__(self, key):
        with self._lock:
            return self._data[key]

    def __setitem__(self, key, value):
        with self._lock:
            self._data[key] = value

    def __delitem__(self, key):
        with self._lock:
            del self._data[key]

    def __contains__(self, key):
        with self._lock:
            return key in self._data

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def items(self):
        with self._lock:
            return list(self._data.items())

    def keys(self):
        with self._lock:
            return list(self._data.keys())

    def values(self):
        with self._lock:
            return list(self._data.values())

    def __len__(self):
        with self._lock:
            return len(self._data)

    def cleanup_old_entries(self, max_age_hours: int = 1):
        """Remove completed/failed/cancelled downloads older than max_age_hours."""
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        with self._lock:
            to_remove = [
                sid for sid, data in self._data.items()
                if data.get('status') in ('completed', 'failed', 'cancelled')
                and data.get('completed_at', datetime.now()) < cutoff
            ]
            for sid in to_remove:
                del self._data[sid]
            if to_remove:
                logger.info(f"Cleaned up {len(to_remove)} old download entries")
            return len(to_remove)


# Global state with thread-safety
active_downloads = ThreadSafeDict()
download_queue = queue.Queue()

# Initialize queue manager
queue_manager = DownloadQueueManager()

# ==========================================
# GLOBAL EPISODE SEMAPHORE
# ==========================================
# This semaphore limits the TOTAL number of concurrent episode downloads
# across ALL series. When max_parallel=5, only 5 episodes can download
# at once, regardless of how many series are in the queue.

class GlobalEpisodeSemaphore:
    """
    Thread-safe global semaphore for limiting concurrent episode downloads.
    Uses threading.Semaphore since downloads run in separate threads.
    """
    def __init__(self, max_concurrent: int):
        self._max = max_concurrent
        self._semaphore = threading.Semaphore(max_concurrent)
        self._lock = threading.Lock()
        self._active_count = 0

    @property
    def max_concurrent(self) -> int:
        with self._lock:
            return self._max

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active_count

    @property
    def available_slots(self) -> int:
        with self._lock:
            return self._max - self._active_count

    def update_max(self, new_max: int):
        """
        Update the maximum concurrent downloads.
        This adjusts the semaphore by releasing or acquiring slots.
        """
        with self._lock:
            diff = new_max - self._max
            self._max = new_max

            if diff > 0:
                # Increase: release additional slots
                for _ in range(diff):
                    self._semaphore.release()
                logger.info(f"Global semaphore increased to {new_max} slots (+{diff})")
            elif diff < 0:
                # Decrease: try to acquire slots (non-blocking)
                # This won't immediately reduce active downloads but will
                # prevent new ones from starting until count drops
                acquired = 0
                for _ in range(-diff):
                    if self._semaphore.acquire(blocking=False):
                        acquired += 1
                if acquired > 0:
                    logger.info(f"Global semaphore decreased to {new_max} slots (-{acquired} acquired)")
                else:
                    logger.info(f"Global semaphore target set to {new_max} (will take effect as downloads complete)")

    def acquire(self, timeout: float = None) -> bool:
        """Acquire a download slot. Returns True if acquired, False on timeout.
        Use timeout=0 for non-blocking attempt."""
        if timeout == 0:
            result = self._semaphore.acquire(blocking=False)
        else:
            result = self._semaphore.acquire(blocking=True, timeout=timeout)
        if result:
            with self._lock:
                self._active_count += 1
        return result

    def release(self):
        """Release a download slot."""
        with self._lock:
            if self._active_count > 0:
                self._active_count -= 1
        self._semaphore.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


# Initialize global episode semaphore with configured max parallel downloads
global_episode_semaphore = GlobalEpisodeSemaphore(app_config.MAX_PARALLEL_DOWNLOADS)

# Global ThreadPoolExecutor for download operations (reused instead of creating new ones)
# This avoids ~50-100ms overhead per download from executor creation
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(
    max_workers=app_config.MAX_PARALLEL_LIMIT + 5,
    thread_name_prefix="DownloadExecutor"
)

import atexit
atexit.register(lambda: DOWNLOAD_EXECUTOR.shutdown(wait=False))


# ==========================================
# DYNAMIC PARALLELISM LIMITER
# ==========================================

class DynamicParallelismLimiter:
    """
    Monitors system memory and dynamically adjusts the parallel download limit.
    Reduces parallelism under memory pressure, restores when memory is available.
    """

    def __init__(self, semaphore: GlobalEpisodeSemaphore, check_interval: int = 30):
        self._semaphore = semaphore
        self._check_interval = check_interval
        self._configured_max = semaphore.max_concurrent  # User-configured max
        self._thread: Optional[threading.Thread] = None
        self._shutdown_event = Event()
        self._is_running = False
        # Memory thresholds (percent of total RAM)
        self._warn_threshold = 75   # Start reducing at 75%
        self._critical_threshold = 85  # Aggressively reduce at 85%
        self._recovery_threshold = 65  # Restore when below 65%

    def start(self):
        if self._is_running:
            return
        self._shutdown_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="ParallelismLimiter",
            daemon=True
        )
        self._is_running = True
        self._thread.start()
        logger.info(f"Dynamic parallelism limiter started (thresholds: warn={self._warn_threshold}%, critical={self._critical_threshold}%)")

    def stop(self):
        self._shutdown_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._is_running = False

    def update_configured_max(self, new_max: int):
        """Called when user changes the parallel limit via settings."""
        self._configured_max = new_max

    def _get_memory_percent(self) -> float:
        """Get current memory usage percentage."""
        try:
            import psutil
            return psutil.virtual_memory().percent
        except ImportError:
            # Fallback: read from /proc/meminfo on Linux
            try:
                with open('/proc/meminfo', 'r') as f:
                    lines = f.readlines()
                total = int(lines[0].split()[1])
                available = int(lines[2].split()[1])
                return (1 - available / total) * 100
            except Exception:
                return 0.0  # Can't determine, don't limit

    def _monitor_loop(self):
        while not self._shutdown_event.is_set():
            try:
                mem_pct = self._get_memory_percent()
                current_max = self._semaphore.max_concurrent

                if mem_pct >= self._critical_threshold:
                    # Critical: reduce to 1
                    target = 1
                    if current_max > target:
                        logger.warning(f"Memory critical ({mem_pct:.0f}%), reducing parallel to {target}")
                        self._semaphore.update_max(target)
                elif mem_pct >= self._warn_threshold:
                    # Warning: reduce by half (but minimum 1)
                    target = max(1, self._configured_max // 2)
                    if current_max > target:
                        logger.warning(f"Memory high ({mem_pct:.0f}%), reducing parallel to {target}")
                        self._semaphore.update_max(target)
                elif mem_pct < self._recovery_threshold:
                    # Recovery: restore to configured max
                    if current_max < self._configured_max:
                        logger.info(f"Memory recovered ({mem_pct:.0f}%), restoring parallel to {self._configured_max}")
                        self._semaphore.update_max(self._configured_max)

            except Exception as e:
                logger.debug(f"Parallelism limiter error: {e}")

            self._shutdown_event.wait(timeout=self._check_interval)


# Initialize the dynamic limiter
dynamic_limiter = DynamicParallelismLimiter(global_episode_semaphore)


# ==========================================
# ROBUST QUEUE PROCESSOR CLASS
# ==========================================

class RobustQueueProcessor:
    """
    Thread-safe, robust queue processor with:
    - Graceful shutdown support
    - Automatic recovery from crashes
    - Health monitoring for stuck downloads
    - Proper resource cleanup
    """

    def __init__(self, max_parallel: int = 3):
        self.max_parallel = max_parallel
        self._lock = RLock()  # Reentrant lock for nested calls
        self._shutdown_event = Event()
        self._wakeup_event = Event()  # Event to wake up processor immediately
        self._processor_thread: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._active_futures: Dict[str, Future] = {}  # session_id -> Future
        self._active_sessions: Set[str] = set()  # Currently processing session IDs
        self._last_activity: Dict[str, datetime] = {}  # session_id -> last activity time
        self._is_running = False
        self._restart_count = 0
        self._max_restarts = 5
        self._health_check_interval = 30  # seconds
        self._download_timeout = 3600  # 1 hour max per download (for very large series)

        # Statistics
        self._stats = {
            'total_processed': 0,
            'total_completed': 0,
            'total_failed': 0,
            'restarts': 0,
            'started_at': None
        }

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running and self._processor_thread is not None and self._processor_thread.is_alive()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active_sessions)

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                **self._stats,
                'is_running': self.is_running,
                'active_downloads': self.active_count,
                'max_parallel': self.max_parallel,
                'restart_count': self._restart_count
            }

    def start(self) -> bool:
        """Start the queue processor. Returns True if started, False if already running."""
        with self._lock:
            if self._is_running:
                logger.warning("Queue processor already running")
                return False

            # Reset shutdown event
            self._shutdown_event.clear()

            # Create new executor with max limit capacity (not current setting)
            # This allows dynamic increases without recreating the executor
            max_limit = app_config.MAX_PARALLEL_LIMIT
            self._executor = ThreadPoolExecutor(
                max_workers=max_limit + 2,  # Use max limit + overhead
                thread_name_prefix="DownloadWorker"
            )

            # Start processor thread
            self._processor_thread = threading.Thread(
                target=self._run_processor_loop,
                name="QueueProcessor",
                daemon=True
            )
            self._is_running = True
            self._stats['started_at'] = datetime.now().isoformat()
            self._processor_thread.start()

            logger.info(f"Queue processor started | Max parallel: {self.max_parallel}")
            logger.debug(f"Thread: {self._processor_thread.name} | ID: {self._processor_thread.ident}")
            return True

    def stop(self, timeout: float = 30.0) -> bool:
        """Gracefully stop the queue processor."""
        with self._lock:
            if not self._is_running:
                logger.warning("Queue processor not running")
                return True

            logger.info("Stopping queue processor...")
            self._shutdown_event.set()

        # Wait for processor thread to finish
        if self._processor_thread:
            self._processor_thread.join(timeout=timeout)
            if self._processor_thread.is_alive():
                logger.warning("Queue processor thread did not stop cleanly")
                return False

        # Shutdown executor
        with self._lock:
            if self._executor:
                self._executor.shutdown(wait=True, cancel_futures=False)
                self._executor = None

            self._is_running = False
            self._processor_thread = None
            logger.info("Queue processor stopped")
            return True

    def _run_processor_loop(self):
        """Main processor loop with automatic recovery."""
        logger.info("Queue processor loop started")
        last_health_check = time.time()
        last_cache_cleanup = time.time()

        while not self._shutdown_event.is_set():
            try:
                # Clear wakeup event at start of each iteration
                self._wakeup_event.clear()

                # Health check for stuck downloads and memory cleanup
                current_time = time.time()
                if current_time - last_health_check > self._health_check_interval:
                    self._check_stuck_downloads()
                    active_downloads.cleanup_old_entries(max_age_hours=1)
                    last_health_check = current_time

                # Periodic cache cleanup (every hour)
                if current_time - last_cache_cleanup > 3600:
                    try:
                        cache_mgr = get_cache_manager()
                        cache_mgr.cleanup_expired()
                    except Exception as e:
                        logger.warning(f"Cache cleanup failed: {e}")
                    last_cache_cleanup = current_time

                # Start as many series as possible
                # Note: max_parallel now controls EPISODE parallelism via global_episode_semaphore
                # Series-level limit is separate: allow many series to be active, but their
                # episodes will compete for global semaphore slots
                MAX_ACTIVE_SERIES = app_config.MAX_PARALLEL_LIMIT + 5  # Allow more series than episode slots
                downloads_started = 0
                while not self._shutdown_event.is_set():
                    # Check if we can start more series
                    with self._lock:
                        current_active = len(self._active_sessions)

                    if current_active >= MAX_ACTIVE_SERIES:
                        # At series capacity, stop starting new series
                        break

                    # Get next queued item
                    item = queue_manager.get_next_queued()

                    if not item:
                        # No more items in queue
                        break

                    # Start download (series will compete for global episode slots)
                    self._start_download(item)
                    downloads_started += 1

                    # Tiny delay between starts to prevent race conditions
                    time.sleep(0.05)

                # If we started downloads, no need to wait long
                if downloads_started > 0:
                    time.sleep(0.1)
                else:
                    # Nothing to do, wait for wakeup or timeout
                    self._wait_for_event(timeout=2.0)

            except Exception as e:
                logger.error(f"Queue processor error: {e}")
                traceback.print_exc()
                # Brief pause before retry
                self._wait_for_event(timeout=5.0)

        logger.info("Queue processor loop ended")

    def _wait_for_event(self, timeout: float):
        """Wait for shutdown, wakeup, or timeout - whichever comes first."""
        # Wait for either event with timeout
        start = time.time()
        while time.time() - start < timeout:
            if self._shutdown_event.is_set():
                return
            if self._wakeup_event.is_set():
                self._wakeup_event.clear()
                return
            time.sleep(0.1)

    def wakeup(self):
        """Wake up the processor to check for new work immediately."""
        self._wakeup_event.set()

    def _start_download(self, item):
        """Start a download for the given queue item."""
        session_id = item.session_id

        with self._lock:
            if session_id in self._active_sessions:
                logger.warning(f"Session {session_id[:8]} already active, skipping")
                return

            self._active_sessions.add(session_id)
            self._last_activity[session_id] = datetime.now()
            self._stats['total_processed'] += 1

        try:
            logger.info(f"Starting: {item.series_name} (Session: {session_id[:8]}...)")
            logger.debug(f"URL: {item.url}")
            logger.debug(f"Active series: {len(self._active_sessions)} | Global episode slots: {global_episode_semaphore.active_count}/{global_episode_semaphore.max_concurrent}")

            # Mark as processing
            queue_manager.update_status(
                session_id,
                DownloadStatus.PROCESSING,
                started_at=datetime.now().isoformat()
            )

            # Update active_downloads for WebSocket
            if session_id in active_downloads:
                active_downloads[session_id]['status'] = 'processing'

            # Submit to executor
            future = self._executor.submit(self._download_worker, item)
            future.add_done_callback(lambda f: self._on_download_complete(session_id, f))

            with self._lock:
                self._active_futures[session_id] = future

        except Exception as e:
            logger.error(f"Failed to start download: {e}")
            self._cleanup_session(session_id, success=False, error=str(e))

    def _download_worker(self, item):
        """Worker function that processes a single download."""
        session_id = item.session_id

        try:
            logger.info(f"Worker started: {item.series_name}")
            logger.debug(f"Language option: {item.options.get('language', 'NOT SET')}")

            # Run the actual download
            run_download_thread(session_id, item.url, item.options)

            return {'success': True, 'session_id': session_id}

        except Exception as e:
            logger.error(f"Worker error: {item.series_name} - {e}")
            traceback.print_exc()
            return {'success': False, 'session_id': session_id, 'error': str(e)}

    def _on_download_complete(self, session_id: str, future: Future):
        """Callback when download completes (success or failure)."""
        try:
            result = future.result()
            success = result.get('success', False)

            if success:
                queue_manager.update_status(
                    session_id,
                    DownloadStatus.COMPLETED,
                    completed_at=datetime.now().isoformat()
                )
                with self._lock:
                    self._stats['total_completed'] += 1
                logger.info(f"Completed: {session_id[:8]}...")
            else:
                error = result.get('error', 'Unknown error')
                queue_manager.update_status(
                    session_id,
                    DownloadStatus.FAILED,
                    completed_at=datetime.now().isoformat()
                )
                with self._lock:
                    self._stats['total_failed'] += 1
                logger.error(f"Failed: {session_id[:8]}... - {error}")

        except Exception as e:
            logger.error(f"Download callback error: {e}")
            queue_manager.update_status(
                session_id,
                DownloadStatus.FAILED,
                completed_at=datetime.now().isoformat()
            )
            with self._lock:
                self._stats['total_failed'] += 1

        finally:
            self._cleanup_session(session_id, success=True)

    def _cleanup_session(self, session_id: str, success: bool = True, error: str = None):
        """Clean up after a download session."""
        with self._lock:
            self._active_sessions.discard(session_id)
            self._active_futures.pop(session_id, None)
            self._last_activity.pop(session_id, None)

        logger.info(f"Session cleaned up: {session_id[:8]}... | Active: {len(self._active_sessions)}/{self.max_parallel}")

        if not success and error:
            queue_manager.update_status(
                session_id,
                DownloadStatus.FAILED,
                completed_at=datetime.now().isoformat()
            )

        # Wake up processor immediately to start next download
        self.wakeup()

    def _check_stuck_downloads(self):
        """Check for downloads that appear to be stuck."""
        now = datetime.now()
        stuck_sessions = []

        with self._lock:
            for session_id, last_active in self._last_activity.items():
                idle_time = (now - last_active).total_seconds()
                if idle_time > self._download_timeout:
                    stuck_sessions.append((session_id, idle_time))

        for session_id, idle_time in stuck_sessions:
            logger.warning(f"Stuck download detected: {session_id[:8]}... (idle for {idle_time:.0f}s)")
            # Cancel the stuck download
            self.cancel_download(session_id)

    def cancel_download(self, session_id: str) -> bool:
        """Cancel a specific download."""
        with self._lock:
            if session_id not in self._active_sessions:
                return False

            future = self._active_futures.get(session_id)
            if future:
                future.cancel()

        # Update status
        queue_manager.update_status(
            session_id,
            DownloadStatus.CANCELLED,
            completed_at=datetime.now().isoformat()
        )

        self._cleanup_session(session_id)
        logger.info(f"Cancelled download: {session_id[:8]}...")
        return True

    def update_activity(self, session_id: str):
        """Update the last activity timestamp for a session."""
        with self._lock:
            if session_id in self._active_sessions:
                self._last_activity[session_id] = datetime.now()

    def set_max_parallel(self, max_parallel: int):
        """Update maximum parallel episode downloads via global semaphore."""
        # Use configurable limit from Config
        max_limit = app_config.MAX_PARALLEL_LIMIT
        new_max = max(1, min(max_limit, max_parallel))

        # Update the GLOBAL episode semaphore (this is what actually limits downloads)
        old_max = global_episode_semaphore.max_concurrent
        global_episode_semaphore.update_max(new_max)

        # Update dynamic limiter's configured max so it knows the user's intent
        dynamic_limiter.update_configured_max(new_max)

        # Also update local reference (for stats/display purposes)
        with self._lock:
            self.max_parallel = new_max
            logger.info(f"Max parallel episode downloads set to: {new_max} (limit: {max_limit})")

        # If limit was increased, wake up processor to start more downloads immediately
        if new_max > old_max:
            logger.info(f"Limit increased, waking up processor to start more downloads...")
            self.wakeup()

    def get_max_limit(self) -> int:
        """Get the absolute maximum parallel downloads allowed."""
        return app_config.MAX_PARALLEL_LIMIT


# Initialize the robust queue processor with config value
queue_processor = RobustQueueProcessor(max_parallel=app_config.MAX_PARALLEL_DOWNLOADS)


# ==========================================
# BACKGROUND AUTO-SCRAPER CLASS
# ==========================================

class BackgroundAutoScraper:
    """
    Automatic background scraper that runs when the system is idle.
    Updates expired cache entries and scrapes uncached series.

    Features:
    - Runs only when no downloads are active
    - Prioritizes expired/expiring cache entries
    - Scrapes uncached series from catalog
    - Parallel batch scraping with ThreadPoolExecutor
    - Rate-limited to avoid overloading
    - Graceful shutdown support
    """

    def __init__(self, queue_processor_ref: RobustQueueProcessor):
        self.queue_processor = queue_processor_ref
        self._lock = RLock()
        self._shutdown_event = Event()
        self._scraper_thread: Optional[threading.Thread] = None
        self._is_running = False

        # Configuration from Config class
        self._enabled = Config.AUTO_SCRAPER_ENABLED
        self._idle_threshold_seconds = Config.AUTO_SCRAPER_IDLE_THRESHOLD
        self._scrape_interval_seconds = Config.AUTO_SCRAPER_INTERVAL
        self._batch_size = Config.AUTO_SCRAPER_BATCH_SIZE
        self._min_idle_between_scrapes = Config.AUTO_SCRAPER_MIN_IDLE
        self._max_parallel_scrapes = min(3, self._batch_size)  # Max parallel scrapes

        # Statistics
        self._stats = {
            'total_scraped': 0,
            'total_updated': 0,
            'total_errors': 0,
            'last_scrape': None,
            'started_at': None,
            'current_status': 'stopped'
        }

        # Scraping state
        self._last_scrape_time = None
        self._currently_scraping = None
        self._scrape_executor = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running and self._scraper_thread is not None and self._scraper_thread.is_alive()

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                **self._stats,
                'is_running': self.is_running,
                'enabled': self._enabled,
                'currently_scraping': self._currently_scraping,
                'config': {
                    'idle_threshold_seconds': self._idle_threshold_seconds,
                    'scrape_interval_seconds': self._scrape_interval_seconds,
                    'batch_size': self._batch_size,
                    'min_idle_between_scrapes': self._min_idle_between_scrapes
                }
            }

    def start(self) -> bool:
        """Start the background auto-scraper."""
        with self._lock:
            if not self._enabled:
                logger.info("Auto-scraper disabled in config")
                self._stats['current_status'] = 'disabled'
                return False

            if self._is_running:
                logger.warning("Auto-scraper already running")
                return False

            self._shutdown_event.clear()
            self._scraper_thread = threading.Thread(
                target=self._run_scraper_loop,
                name="BackgroundAutoScraper",
                daemon=True
            )
            self._is_running = True
            self._stats['started_at'] = datetime.now().isoformat()
            self._stats['current_status'] = 'running'
            self._scraper_thread.start()

            logger.info(f"Background Auto-Scraper started (idle: {self._idle_threshold_seconds}s, interval: {self._scrape_interval_seconds}s)")
            return True

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the background auto-scraper."""
        with self._lock:
            if not self._is_running:
                return True

            logger.info("Stopping Auto-Scraper...")
            self._shutdown_event.set()

        if self._scraper_thread:
            self._scraper_thread.join(timeout=timeout)

        with self._lock:
            self._is_running = False
            self._scraper_thread = None
            self._stats['current_status'] = 'stopped'
            logger.info("Auto-Scraper stopped")
            return True

    def _is_system_idle(self) -> bool:
        """Check if the system is idle (no active downloads)."""
        return self.queue_processor.active_count == 0

    def _run_scraper_loop(self):
        """Main scraper loop."""
        logger.info("Auto-Scraper loop started")
        logger.debug(f"Config: idle_threshold={self._idle_threshold_seconds}s, interval={self._scrape_interval_seconds}s, batch={self._batch_size}")
        idle_start_time = None
        last_status_log = 0

        while not self._shutdown_event.is_set():
            try:
                current_time = time.time()

                # Check if system is idle
                if self._is_system_idle():
                    if idle_start_time is None:
                        idle_start_time = current_time
                        logger.info(f"Auto-Scraper: System idle, waiting {self._idle_threshold_seconds}s before scraping...")
                        with self._lock:
                            self._stats['current_status'] = 'waiting_for_idle'

                    # Check if we've been idle long enough
                    idle_duration = current_time - idle_start_time
                    if idle_duration >= self._idle_threshold_seconds:
                        # Check scrape interval
                        if self._last_scrape_time is None or \
                           (current_time - self._last_scrape_time) >= self._scrape_interval_seconds:
                            self._perform_scrape_cycle()
                        elif current_time - last_status_log > 60:
                            # Log status every 60 seconds when waiting for interval
                            wait_remaining = self._scrape_interval_seconds - (current_time - self._last_scrape_time)
                            logger.info(f"Auto-Scraper: Waiting {wait_remaining:.0f}s until next scrape cycle")
                            last_status_log = current_time
                else:
                    # Downloads active - reset idle timer
                    if idle_start_time is not None:
                        logger.info("Auto-Scraper: Downloads active, pausing...")
                    idle_start_time = None
                    with self._lock:
                        self._stats['current_status'] = 'paused_downloads_active'

                # Wait before next check
                self._shutdown_event.wait(timeout=5.0)

            except Exception as e:
                logger.error(f"Auto-Scraper error: {e}")
                traceback.print_exc()
                with self._lock:
                    self._stats['total_errors'] += 1
                self._shutdown_event.wait(timeout=30.0)

        logger.info("Auto-Scraper loop ended")

    def _perform_scrape_cycle(self):
        """Perform one scrape cycle with parallel batch scraping."""
        with self._lock:
            self._stats['current_status'] = 'scraping'

        try:
            # Priority 1: Update expired/expiring cache entries
            series_to_update = get_series_needing_update(limit=self._batch_size)

            if series_to_update:
                slugs_with_reasons = [
                    (info['series_slug'], f"cache_{info.get('status', 'unknown')}")
                    for info in series_to_update
                ]
                logger.info(f"Auto-Scraper: Found {len(slugs_with_reasons)} series needing update")
                self._scrape_batch(slugs_with_reasons)

            # Priority 2: Scrape uncached series from catalog (if still idle)
            elif self._is_system_idle():
                uncached = get_uncached_series_from_catalog()
                if uncached:
                    to_scrape = [(slug, "new_series") for slug in uncached[:self._batch_size]]
                    logger.info(f"Auto-Scraper: Found {len(uncached)} uncached series, scraping {len(to_scrape)}")
                    self._scrape_batch(to_scrape)
                else:
                    catalog_needs_refresh = self._check_catalog_needs_refresh()
                    if catalog_needs_refresh:
                        logger.info("Auto-Scraper: Catalog empty or stale, refreshing...")
                        self._refresh_catalog()
                    else:
                        logger.info("Auto-Scraper: All series cached, nothing to do.")
                        with self._lock:
                            self._stats['current_status'] = 'idle_all_cached'

            self._last_scrape_time = time.time()

        except Exception as e:
            logger.error(f"Scrape cycle error: {e}")
            traceback.print_exc()
            with self._lock:
                self._stats['total_errors'] += 1

        with self._lock:
            self._stats['current_status'] = 'idle'

    def _scrape_batch(self, slugs_with_reasons: list):
        """Scrape multiple series in parallel using ThreadPoolExecutor."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Split into chunks of max_parallel_scrapes
        for i in range(0, len(slugs_with_reasons), self._max_parallel_scrapes):
            if self._shutdown_event.is_set() or not self._is_system_idle():
                logger.info("Auto-Scraper: Batch interrupted (download started or shutdown)")
                break

            batch = slugs_with_reasons[i:i + self._max_parallel_scrapes]
            batch_slugs = [s for s, _ in batch]

            with self._lock:
                self._currently_scraping = batch_slugs if len(batch_slugs) > 1 else batch_slugs[0]

            logger.info(f"Auto-Scraper: Parallel batch [{i+1}-{i+len(batch)}/{len(slugs_with_reasons)}]: {batch_slugs}")

            with ThreadPoolExecutor(max_workers=self._max_parallel_scrapes, thread_name_prefix="scraper") as executor:
                futures = {
                    executor.submit(self._scrape_series, slug, reason): slug
                    for slug, reason in batch
                }
                for future in as_completed(futures):
                    slug = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Auto-Scraper: Batch scrape failed for '{slug}': {e}")

            # Brief pause between batches
            if i + self._max_parallel_scrapes < len(slugs_with_reasons):
                self._shutdown_event.wait(timeout=self._min_idle_between_scrapes)

    def _scrape_series(self, series_slug: str, reason: str = ""):
        """Scrape a single series and update cache."""
        with self._lock:
            self._currently_scraping = series_slug

        try:
            logger.info(f"Auto-Scraper: Scraping '{series_slug}' ({reason})")

            # Determine base URL from slug or catalog
            base_url = self._get_series_url(series_slug)
            if not base_url:
                logger.warning(f"Could not determine URL for {series_slug}")
                return

            # Run the async scrape
            result = self._run_async_scrape(base_url, series_slug)

            if result and 'error' not in result:
                # Save to cache
                is_ongoing = result.get('is_ongoing', True)
                cache_data = {
                    'url_type': result.get('url_type'),
                    'series_slug': series_slug,
                    'season': result.get('season'),
                    'start_episode': result.get('start_episode'),
                    'total_seasons': result.get('total_seasons'),
                    'seasons_data': result.get('seasons_data', {}),
                    'series_cover_url': result.get('series_cover_url'),
                    'series_description': result.get('series_description'),
                    'series_name': series_slug.replace('-', ' ').title(),
                    'end_date': result.get('end_date'),
                    'is_ongoing': is_ongoing,
                    'languages': result.get('languages', [])
                }
                save_to_cache(series_slug, cache_data)

                with self._lock:
                    self._stats['total_scraped'] += 1
                    self._stats['total_updated'] += 1
                    self._stats['last_scrape'] = datetime.now().isoformat()

                status_text = "ongoing" if is_ongoing else f"completed ({result.get('end_date', 'unknown')})"
                logger.info(f"Auto-Scraper: Updated '{series_slug}' [{status_text}]")
            else:
                logger.warning(f"Auto-Scraper: Failed to scrape '{series_slug}'")
                with self._lock:
                    self._stats['total_errors'] += 1

        except Exception as e:
            logger.error(f"Auto-Scraper error for {series_slug}: {e}")
            with self._lock:
                self._stats['total_errors'] += 1

        finally:
            with self._lock:
                self._currently_scraping = None

    def _get_series_url(self, series_slug: str) -> Optional[str]:
        """Get the full URL for a series slug."""
        # Try to find in catalog cache
        from app.series_catalog import SOURCES, load_catalog_cache

        for source_id, config in SOURCES.items():
            catalog = load_catalog_cache(source_id)
            if catalog:
                for genre_series in catalog.get('genres', {}).values():
                    for series in genre_series:
                        if series.get('slug') == series_slug:
                            # Found it - construct URL
                            return f"{config['base_url']}{config['content_path']}{series_slug}"

        # Fallback: assume it's a series (not anime)
        return f"http://186.2.175.5/serie/stream/{series_slug}"

    def _check_catalog_needs_refresh(self) -> bool:
        """Check if the catalog needs to be refreshed."""
        from app.series_catalog import load_catalog_cache, is_catalog_stale

        # Check both catalogs
        series_catalog = load_catalog_cache('series')
        anime_catalog = load_catalog_cache('anime')

        # If both are missing or empty, we need to refresh
        series_empty = not series_catalog or not series_catalog.get('genres')
        anime_empty = not anime_catalog or not anime_catalog.get('genres')

        if series_empty and anime_empty:
            return True

        # Also refresh if stale (older than 24 hours)
        if is_catalog_stale('series') or is_catalog_stale('anime'):
            return True

        return False

    def _scrape_single_catalog(self, source_id: str):
        """Scrape and save a single catalog source."""
        from app.series_catalog import scrape_catalog, save_catalog_cache

        logger.info(f"Auto-Scraper: Scraping {source_id} catalog...")
        loop = asyncio.new_event_loop()
        try:
            catalog = loop.run_until_complete(scrape_catalog(source_id))
            if catalog and catalog.get('genres'):
                save_catalog_cache(catalog, source_id)
                logger.info(f"Auto-Scraper: {source_id.capitalize()} catalog updated ({catalog.get('total_items', 0)} items)")
        except Exception as e:
            logger.error(f"Auto-Scraper: Failed to scrape {source_id} catalog: {e}")
        finally:
            loop.close()

    def _refresh_catalog(self):
        """Refresh the series and anime catalogs."""
        with self._lock:
            self._stats['current_status'] = 'refreshing_catalog'

        try:
            self._scrape_single_catalog('series')

            self._shutdown_event.wait(timeout=5.0)

            if self._is_system_idle() and not self._shutdown_event.is_set():
                self._scrape_single_catalog('anime')

        except Exception as e:
            logger.error(f"Auto-Scraper: Catalog refresh error: {e}")
            traceback.print_exc()

    def _run_async_scrape(self, url: str, series_slug: str) -> Optional[dict]:
        """Run async scraping in a new event loop."""
        from app.browser_pool import BrowserPool

        async def do_scrape():
            result = {
                'series_slug': series_slug,
                'total_seasons': 1,
                'seasons_data': {},
                'series_cover_url': None,
                'series_description': None,
                'end_date': None,
                'is_ongoing': True,
                'languages': []
            }

            try:
                # Parse URL to get base info
                # parse_flexible_url returns: (base_url, series_slug, season, episode, url_type)
                parsed = parse_flexible_url(url)
                if not parsed:
                    return None

                # Unpack tuple result
                parsed_base_url, parsed_slug, parsed_season, parsed_episode, url_type = parsed
                base_url = parsed_base_url or f"http://186.2.175.5/serie/stream/{series_slug}"

                # Scrape series page for total seasons and metadata
                series_data = await self._async_scrape_series_page(base_url)

                if series_data is None:
                    return None

                result['url_type'] = url_type
                result['total_seasons'] = series_data.get('seasons') or 1
                result['series_cover_url'] = series_data.get('cover')
                result['series_description'] = series_data.get('description')
                result['end_date'] = series_data.get('end_date')
                result['is_ongoing'] = series_data.get('is_ongoing', True)
                result['languages'] = series_data.get('languages', [])

                total_seasons = result['total_seasons']

                # Scrape all seasons
                if total_seasons and total_seasons > 0:
                    async with BrowserPool(pool_size=min(2, total_seasons)) as pool:
                        tasks = []
                        for s in range(1, total_seasons + 1):
                            season_url = f"{base_url}/staffel-{s}"
                            tasks.append(self._async_scrape_season(season_url, s, pool))

                        season_results = await asyncio.gather(*tasks, return_exceptions=True)

                        for s_num, s_result in enumerate(season_results, 1):
                            if isinstance(s_result, Exception):
                                result['seasons_data'][s_num] = {'episodes': [], 'episode_details': {}}
                            else:
                                result['seasons_data'][s_num] = s_result

                return result

            except Exception as e:
                logger.error(f"Async scrape error: {e}")
                return None

        # Run in new event loop
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(do_scrape())
        finally:
            loop.close()

    async def _async_scrape_series_page(self, series_url: str):
        """Scrape series page for metadata - optimized version."""
        from playwright.async_api import async_playwright

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--mute-audio']
                )

                context = await browser.new_context()
                page = await context.new_page()
                page.set_default_timeout(20000)

                await page.goto(series_url, wait_until="domcontentloaded", timeout=15000)

                # Extract all data in ONE JavaScript call
                data = await page.evaluate(r'''
                    () => {
                        const result = {
                            seasons: null,
                            seasonTabs: [],
                            extraTabs: [],
                            cover: null,
                            description: null,
                            endDate: null,
                            isOngoing: true,
                            languages: []
                        };

                        // Extract available languages from changeLanguageBox - DYNAMIC detection
                        const langBox = document.querySelector('.changeLanguageBox');
                        if (langBox) {
                            const langImages = langBox.querySelectorAll('img[data-lang-key]');
                            langImages.forEach(img => {
                                const langKey = img.getAttribute('data-lang-key');
                                const title = img.getAttribute('title') || '';
                                const alt = img.getAttribute('alt') || '';
                                const isSelected = img.classList.contains('selectedLanguage');
                                const src = img.getAttribute('src') || '';

                                // Extract filename from src path (e.g., "german.svg", "japanese-english.svg")
                                const srcFilename = src.split('/').pop().replace('.svg', '').toLowerCase();

                                // Use title as display name (most descriptive), fallback to first part of alt
                                const langName = title || (alt ? alt.split(',')[0].trim() : srcFilename);

                                result.languages.push({
                                    key: langKey,
                                    name: langName,
                                    selected: isSelected,
                                    icon: src,
                                    // Store raw attributes for flexible backend interpretation
                                    srcFile: srcFilename,
                                    title: title,
                                    alt: alt
                                });
                            });
                        }

                        // Read season tabs directly from DOM to distinguish real seasons from extras
                        const seasonLinks = document.querySelectorAll(
                            '.hosterSiteDirect498Link, ' +
                            'a[href*="/staffel-"], ' +
                            'a[href*="/filme"], ' +
                            'a[href*="/specials"], ' +
                            'a[href*="/ova"], ' +
                            'a[href*="/movies"], ' +
                            '.seasonLink, ' +
                            '#stream ul li a, ' +
                            '.seriesListContainer a'
                        );
                        const numericSeasons = [];
                        const extraTabs = [];

                        seasonLinks.forEach(link => {
                            const href = link.getAttribute('href') || '';
                            const text = link.textContent.trim().toLowerCase();

                            // Check if it's a numeric season
                            const seasonMatch = href.match(/\/staffel-(\d+)/);
                            if (seasonMatch) {
                                numericSeasons.push(parseInt(seasonMatch[1]));
                            }
                            // Check for extra tabs by href pattern
                            else if (href.includes('/filme')) {
                                extraTabs.push('filme');
                            }
                            else if (href.includes('/specials')) {
                                extraTabs.push('specials');
                            }
                            else if (href.includes('/ova')) {
                                extraTabs.push('ova');
                            }
                            else if (href.includes('/movies')) {
                                extraTabs.push('movies');
                            }
                            // Check by text content
                            else if (text === 'filme' || text === 'films' || text === 'movie' || text === 'movies') {
                                extraTabs.push('filme');
                            }
                            else if (text === 'specials' || text === 'special') {
                                extraTabs.push('specials');
                            }
                            else if (text === 'ova' || text === 'ovas') {
                                extraTabs.push('ova');
                            }
                        });

                        // Use actual numeric seasons count, not numberOfSeasons meta (which includes extras)
                        if (numericSeasons.length > 0) {
                            result.seasons = Math.max(...numericSeasons);
                            result.seasonTabs = [...new Set(numericSeasons)].sort((a, b) => a - b);
                        } else {
                            // Fallback to meta tag if no season links found
                            const seasonsMeta = document.querySelector('meta[itemprop="numberOfSeasons"]');
                            if (seasonsMeta) {
                                const metaSeasons = parseInt(seasonsMeta.getAttribute('content'));
                                result.seasons = Math.max(1, metaSeasons - extraTabs.length);
                            }
                        }

                        result.extraTabs = [...new Set(extraTabs)];

                        // Get cover image - try new structure first, fallback to old
                        let coverImg = document.querySelector('picture img.img-fluid.w-100.loaded[alt]');
                        if (!coverImg || coverImg.getAttribute('src')?.includes('base64')) {
                            // Try to find img with data-src attribute containing '/channel/'
                            const allImgs = document.querySelectorAll('picture img[data-src*="/channel/"]');
                            if (allImgs.length > 0) coverImg = allImgs[0];
                        }
                        if (!coverImg) {
                            // Fallback to old selector
                            coverImg = document.querySelector('.seriesCoverBox img');
                        }
                        if (coverImg) {
                            result.cover = coverImg.getAttribute('data-src') || coverImg.getAttribute('src');
                        }

                        // Get description - try new structure first, fallback to old
                        let descElem = document.querySelector('span.description-text');
                        if (descElem) {
                            result.description = descElem.textContent.trim();
                        } else {
                            // Fallback to old selector
                            descElem = document.querySelector('p.seri_des[data-full-description]');
                            if (descElem) {
                                let desc = descElem.getAttribute('data-full-description');
                                if (desc && desc.startsWith('[')) {
                                    const bracketEnd = desc.indexOf(']');
                                    if (bracketEnd !== -1) desc = desc.substring(bracketEnd + 1).trim();
                                }
                                result.description = desc;
                            }
                        }

                        // Check endDate - if not "Heute", series is completed
                        const endDateElem = document.querySelector('span[itemprop="endDate"]');
                        if (endDateElem) {
                            const endDateText = endDateElem.textContent.trim();
                            result.endDate = endDateText;
                            // Series is ongoing only if endDate contains "Heute"
                            result.isOngoing = endDateText.toLowerCase().includes('heute');
                        }

                        return result;
                    }
                ''')

                await browser.close()

                cover = data.get('cover')
                if cover and cover.startswith('/'):
                    from urllib.parse import urlparse
                    parsed = urlparse(series_url)
                    cover = f"{parsed.scheme}://{parsed.netloc}{cover}"

                return {
                    'seasons': data.get('seasons'),
                    'season_tabs': data.get('seasonTabs', []),
                    'extra_tabs': data.get('extraTabs', []),
                    'cover': cover,
                    'description': data.get('description'),
                    'end_date': data.get('endDate'),
                    'is_ongoing': data.get('isOngoing', True),
                    'languages': data.get('languages', [])
                }

        except Exception as e:
            logger.error(f"Series page scrape error: {e}")
            return None

    async def _async_scrape_season(self, season_url: str, season_num: int, browser_pool):
        """Scrape a single season - optimized version."""
        from app.browser_pool import PooledPageContext

        season_data = {'episodes': [], 'episode_details': {}}

        try:
            async with PooledPageContext(browser_pool) as page:
                await page.goto(season_url, wait_until="domcontentloaded", timeout=15000)

                episode_data = await page.evaluate('''
                    () => {
                        const episodes = new Set();
                        const details = {};

                        // Get all episode AND film links (for Filme pages)
                        document.querySelectorAll('a[href*="/episode-"], a[href*="/film-"]').forEach(link => {
                            const href = link.getAttribute('href');
                            if (href) {
                                // Handle both /episode-N and /film-N patterns
                                let match = null;
                                if (href.includes('/episode-')) {
                                    match = href.split('/episode-')[1];
                                } else if (href.includes('/film-')) {
                                    match = href.split('/film-')[1];
                                }
                                if (match) {
                                    const epNum = parseInt(match.split('/')[0].split('?')[0]);
                                    if (!isNaN(epNum)) episodes.add(epNum);
                                }
                            }
                        });

                        document.querySelectorAll('tr[data-episode-id], .episodeWrapper').forEach(row => {
                            const epLink = row.querySelector('a[href*="/episode-"], a[href*="/film-"]');
                            if (epLink) {
                                const href = epLink.getAttribute('href');
                                let epNum = null;
                                if (href.includes('/episode-')) {
                                    epNum = parseInt(href.split('/episode-')[1].split('/')[0]);
                                } else if (href.includes('/film-')) {
                                    epNum = parseInt(href.split('/film-')[1].split('/')[0]);
                                }
                                if (epNum && !isNaN(epNum)) {
                                    const deTitleElem = row.querySelector('.episodeGermanTitle, .seasonEpisodeTitle strong');
                                    const enTitleElem = row.querySelector('.episodeEnglishTitle, small');
                                    details[epNum] = {
                                        title_de: deTitleElem ? deTitleElem.textContent.trim() : '',
                                        title_en: enTitleElem ? enTitleElem.textContent.trim() : ''
                                    };
                                }
                            }
                        });

                        return {
                            episodes: Array.from(episodes).sort((a, b) => a - b),
                            details: details
                        };
                    }
                ''')

                season_data['episodes'] = episode_data['episodes']
                season_data['episode_details'] = episode_data['details']

        except Exception as e:
            logger.error(f"Season {season_num} scrape error: {e}")
            season_data['episodes'] = list(range(1, 13))  # Fallback

        return season_data


# Initialize the background auto-scraper
auto_scraper = BackgroundAutoScraper(queue_processor)

# Note: Global episode semaphore is defined earlier (GlobalEpisodeSemaphore class)
# It controls the total number of concurrent episode downloads across ALL series

# Aggregated progress tracker for parallel downloads
# Structure: {session_id: {episode_key: percent, ...}}
parallel_progress_tracker = {}
parallel_progress_lock = threading.Lock()


# ==========================================
# GRACEFUL SHUTDOWN HANDLERS
# ==========================================

def graceful_shutdown(signum=None, frame=None):
    """Handle graceful shutdown on SIGINT/SIGTERM."""
    logger.info("Shutdown signal received, cleaning up...")
    auto_scraper.stop(timeout=10.0)
    queue_processor.stop(timeout=30.0)
    # Save queue state
    queue_manager.save_queue()
    logger.info("Cleanup complete")

# Register shutdown handlers
atexit.register(graceful_shutdown)
try:
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
except (ValueError, OSError):
    # Signal handling not supported (e.g., not main thread)
    pass


def generate_session_id():
    """Generate a unique session ID for downloads"""
    return str(uuid.uuid4())


class PrintFilter:
    """Context manager that filters print() output and forwards critical messages to WebLogger.
    Used to capture print output from external libraries (e.g. yt-dlp) during downloads."""

    CRITICAL_PATTERNS = [
        '🚀 Download started!', 'Detected URL type:', 'Series:', 'Season:',
        '🎯 Found m3u8:', '📥 Starting download:', '📊 Download progress:',
        '📊 FFmpeg progress:', '✅ Download complete:', '❌', '⚠️'
    ]

    def __init__(self, web_logger):
        self.web_logger = web_logger

    def __enter__(self):
        import builtins
        self._original_print = builtins.print

        def filtered_print(*args, **kwargs):
            message = ' '.join(map(str, args))
            self._original_print(*args, **kwargs)
            if any(p in message for p in self.CRITICAL_PATTERNS):
                self.web_logger.log(message, "info")

        builtins.print = filtered_print
        return self

    def __exit__(self, *exc):
        import builtins
        builtins.print = self._original_print
        return False


class WebLogger:
    """Custom logger that emits to WebSocket"""
    def __init__(self, session_id, episode_key=None):
        self.session_id = session_id
        self.episode_key = episode_key  # e.g., "S01E05" for tracking parallel progress
        self.last_progress_id = None

    def log(self, message, level="info", update_last=False):
        """Send log message to web client"""
        socketio.emit('log', {
            'session_id': self.session_id,
            'message': message,
            'level': level,
            'update_last': update_last  # Flag to update last log entry
        })

    def info(self, message):
        self.log(message, level="info")

    def error(self, message):
        self.log(message, level="error")

    def warning(self, message):
        self.log(message, level="warning")

    def debug(self, message):
        self.log(message, level="debug")

    def log_progress(self, message, level="info"):
        """Log progress message that updates the previous progress entry"""
        socketio.emit('log', {
            'session_id': self.session_id,
            'message': message,
            'level': level,
            'update_last': True,
            'is_progress': True
        })

    def log_download_progress(self, percent, speed=None, eta=None):
        """Send download progress and track for parallel aggregation"""
        # Track progress for this episode (for parallel download averaging)
        if self.episode_key:
            with parallel_progress_lock:
                if self.session_id not in parallel_progress_tracker:
                    parallel_progress_tracker[self.session_id] = {}
                parallel_progress_tracker[self.session_id][self.episode_key] = float(percent)

            # Update episode status in queue manager (downloading with progress)
            queue_manager.update_episode_status(self.session_id, self.episode_key, status='downloading', progress=float(percent))

            # Emit individual episode status update for real-time UI
            socketio.emit('episode_status_update', {
                'session_id': self.session_id,
                'episode_key': self.episode_key,
                'status': 'downloading',
                'progress': float(percent),
                'speed': speed,
                'eta': eta
            })

        # Send individual progress event
        socketio.emit('download_progress', {
            'session_id': self.session_id,
            'episode_key': self.episode_key,
            'percent': float(percent),
            'speed': speed,
            'eta': eta
        })

        # Update activity tracking for health monitoring
        queue_processor.update_activity(self.session_id)


def get_aggregated_progress(session_id):
    """Calculate average progress across all parallel downloads for a session"""
    with parallel_progress_lock:
        if session_id not in parallel_progress_tracker:
            return None
        episodes = parallel_progress_tracker[session_id]
        if not episodes:
            return None
        # Calculate average of all episode progress values
        avg_progress = sum(episodes.values()) / len(episodes)
        return {
            'average_percent': avg_progress,
            'active_episodes': len(episodes),
            'episodes': dict(episodes)  # Copy for safety
        }


def clear_progress_tracker(session_id, episode_key=None):
    """Clear progress tracker for completed episodes or entire session"""
    with parallel_progress_lock:
        if session_id in parallel_progress_tracker:
            if episode_key:
                # Remove single episode
                parallel_progress_tracker[session_id].pop(episode_key, None)
            else:
                # Clear entire session
                del parallel_progress_tracker[session_id]


def emit_aggregated_progress(session_id, total_episodes, completed_episodes):
    """Emit aggregated progress update for parallel downloads"""
    agg = get_aggregated_progress(session_id)
    if agg and agg['active_episodes'] > 0:
        # Calculate combined progress:
        # - completed_episodes contribute 100% each
        # - active episodes contribute their current percentage
        active_progress_sum = sum(agg['episodes'].values())
        total_progress = (completed_episodes * 100 + active_progress_sum) / total_episodes if total_episodes > 0 else 0

        socketio.emit('aggregated_progress', {
            'session_id': session_id,
            'total_percent': min(100, total_progress),
            'completed_episodes': completed_episodes,
            'total_episodes': total_episodes,
            'active_downloads': agg['active_episodes'],
            'average_active_percent': agg['average_percent']
        })


def _verify_downloaded_file(output_path, logger=None):
    """Verify a downloaded file's integrity if enabled in settings."""
    from app.config import _json_settings
    if not _json_settings.get('verify_downloads', True):
        return
    if logger:
        logger.log("🔍 Verifying file integrity...", "info")
    from app.ffmpeg_setup import get_ffprobe_executable
    verifier = FileVerifier(ffprobe_path=get_ffprobe_executable())
    verification = verifier.verify_file(str(output_path))
    if verification.is_valid:
        if logger:
            logger.log("✅ File verification passed", "success")
            if verification.duration:
                logger.log(f"   Duration: {format_duration(verification.duration)}", "info")
            if verification.resolution:
                logger.log(f"   Resolution: {verification.resolution}", "info")
            if verification.video_codec:
                logger.log(f"   Video: {verification.video_codec}", "info")
            if verification.audio_codec:
                logger.log(f"   Audio: {verification.audio_codec}", "info")
    else:
        if logger:
            logger.log(f"⚠️ File verification failed: {verification.error}", "warning")
            logger.log(f"   File size: {format_file_size(verification.file_size)}", "warning")


def download_video_with_progress(m3u8_url, output_path, quality="best", codec="auto", logger=None, audio_only=False, audio_format="mp3", audio_bitrate="0"):
    """Synchronous download function with FFmpeg progress tracking for WebUI

    Args:
        m3u8_url: URL to the m3u8 stream
        output_path: Path to save the video
        quality: Quality preference (best, 1080p, 720p, etc.)
        codec: Video codec preference (auto, h264, h265, av1)
        logger: Optional WebLogger instance for progress tracking
        audio_only: If True, extract only audio track (no video)
        audio_format: Audio format (mp3, flac, aac, ogg, wav, opus)
        audio_bitrate: Audio bitrate in kbps (0 = best quality)
    """
    output_path = Path(output_path)

    # Map audio formats to extensions
    audio_extensions = {
        'mp3': '.mp3',
        'flac': '.flac',
        'aac': '.m4a',
        'ogg': '.ogg',
        'wav': '.wav',
        'opus': '.opus'
    }

    # Change extension to audio format if audio_only
    if audio_only:
        ext = audio_extensions.get(audio_format, '.mp3')
        output_path = output_path.with_suffix(ext)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if logger:
        mode_text = "audio" if audio_only else "download"
        logger.log(f"📥 Starting {mode_text}: {output_path.name}", "info")

    # Build format selector based on codec preference and audio_only mode
    if audio_only:
        # Audio only: extract best audio
        format_selector = "bestaudio/best"
    elif codec == "h264":
        # Prefer H.264/AVC codec
        format_selector = f"bestvideo[vcodec^=avc]+bestaudio/best[vcodec^=avc]/bestvideo+bestaudio/{quality}"
    elif codec == "h265":
        # Prefer H.265/HEVC codec
        format_selector = f"bestvideo[vcodec^=hvc]+bestaudio/bestvideo[vcodec^=hev]+bestaudio/best[vcodec^=hvc]/best[vcodec^=hev]/bestvideo+bestaudio/{quality}"
    elif codec == "av1":
        # Prefer AV1 codec
        format_selector = f"bestvideo[vcodec^=av01]+bestaudio/best[vcodec^=av01]/bestvideo+bestaudio/{quality}"
    else:
        # Auto: use quality setting directly
        format_selector = quality

    # Try yt-dlp first
    command = [
        sys.executable, "-m", "yt_dlp",
        "-o", str(output_path),
        "-f", format_selector,
        "--no-warnings",
        "--newline",  # Progress on new lines for easier parsing
    ]

    # Add audio extraction options if audio_only
    if audio_only:
        command.extend([
            "-x",  # Extract audio
            "--audio-format", audio_format,
        ])
        # Add bitrate if specified (0 = best quality)
        bitrate = int(audio_bitrate) if audio_bitrate else 0
        if bitrate > 0:
            command.extend(["--audio-quality", f"{bitrate}K"])
        else:
            command.extend(["--audio-quality", "0"])  # Best quality

    command.append(m3u8_url)

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )

        last_progress = None
        for line in process.stdout:
            line = line.strip()

            # Parse yt-dlp progress: [download]  45.2% of 123.45MiB at 1.23MiB/s ETA 00:45
            if '[download]' in line and '%' in line:
                # Extract percentage, speed, and ETA
                match = re.search(r'(\d+\.?\d*)%', line)
                if match:
                    percent = match.group(1)
                    speed_match = re.search(r'at\s+([\d.]+\s*\w+/s)', line)
                    eta_match = re.search(r'ETA\s+(\S+)', line)
                    speed = speed_match.group(1) if speed_match else None
                    eta = eta_match.group(1) if eta_match else None
                    # Send download progress to status tab
                    if logger:
                        logger.log_download_progress(percent, speed=speed, eta=eta)
                    last_progress = int(float(percent))

        return_code = process.wait()

        if return_code == 0:
            if logger:
                logger.log(f"✅ Download complete: {output_path.name}", "success")

            _verify_downloaded_file(output_path, logger)

            return True
        else:
            raise subprocess.CalledProcessError(return_code, command)

    except subprocess.CalledProcessError as e:
        if logger:
            logger.log("⚠️ yt-dlp failed, trying ffmpeg...", "warning")

        # Fallback to ffmpeg
        from app.ffmpeg_setup import get_ffmpeg_executable
        ffmpeg_bin = get_ffmpeg_executable()
        try:
            if audio_only:
                # Map audio format to ffmpeg codec
                audio_codecs = {
                    'mp3': ('libmp3lame', ['-q:a', '0']),
                    'flac': ('flac', []),
                    'aac': ('aac', ['-b:a', '256k']),
                    'ogg': ('libvorbis', ['-q:a', '6']),
                    'wav': ('pcm_s16le', []),
                    'opus': ('libopus', ['-b:a', '128k'])
                }
                codec_name, codec_opts = audio_codecs.get(audio_format, ('libmp3lame', ['-q:a', '0']))

                # Adjust for bitrate if specified
                bitrate = int(audio_bitrate) if audio_bitrate else 0
                if bitrate > 0 and audio_format in ['mp3', 'aac', 'opus']:
                    codec_opts = ['-b:a', f'{bitrate}k']

                # FFmpeg audio extraction
                ffmpeg_command = [
                    ffmpeg_bin,
                    "-i", m3u8_url,
                    "-vn",  # No video
                    "-acodec", codec_name,
                ] + codec_opts + [
                    str(output_path),
                    "-y",  # Overwrite
                    "-progress", "pipe:1",
                    "-loglevel", "error"
                ]
            else:
                # Standard video download
                ffmpeg_command = [
                    ffmpeg_bin,
                    "-i", m3u8_url,
                    "-c", "copy",
                    "-bsf:a", "aac_adtstoasc",
                    str(output_path),
                    "-y",  # Overwrite
                    "-progress", "pipe:1",  # Progress to stdout
                    "-loglevel", "error"
                ]

            process = subprocess.Popen(
                ffmpeg_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1
            )

            # Track ffmpeg progress
            duration = None

            for line in process.stdout:
                line = line.strip()

                # Parse duration (total time)
                if line.startswith('Duration:'):
                    duration_match = re.search(r'Duration: (\d{2}):(\d{2}):(\d{2})', line)
                    if duration_match:
                        h, m, s = map(int, duration_match.groups())
                        duration = h * 3600 + m * 60 + s

                # Parse current time
                if line.startswith('out_time_ms='):
                    try:
                        time_us = int(line.split('=')[1])
                        current_time = time_us / 1000000  # Convert to seconds

                        if duration and current_time > 0:
                            percent = min(100, (current_time / duration) * 100)
                            # Update progress display continuously
                            if logger:
                                logger.log_progress(f"📊 FFmpeg: {int(percent)}%", "info")
                    except Exception:
                        pass

            return_code = process.wait()

            if return_code == 0:
                if logger:
                    logger.log(f"✅ Download complete: {output_path.name}", "success")

                _verify_downloaded_file(output_path, logger)

                return True
            else:
                if logger:
                    logger.log("❌ FFmpeg download failed", "error")
                return False

        except Exception as e:
            if logger:
                logger.log(f"❌ FFmpeg error: {str(e)}", "error")
            return False


async def download_video(m3u8_url, output_path, quality="best", codec="auto", logger=None, audio_only=False, audio_format="mp3", audio_bitrate="0"):
    """Async wrapper for download with progress - uses global DOWNLOAD_EXECUTOR"""
    import asyncio
    from functools import partial

    loop = asyncio.get_event_loop()

    # Use partial to pass all arguments including codec, audio_only, and audio options
    download_func = partial(
        download_video_with_progress,
        m3u8_url,
        output_path,
        quality,
        codec,
        logger,
        audio_only,
        audio_format,
        audio_bitrate
    )

    # Use global executor instead of creating a new one (saves ~50-100ms per download)
    result = await loop.run_in_executor(DOWNLOAD_EXECUTOR, download_func)

    return result


async def process_single_episode(extractor, url, args, browser_id="", logger=None):
    """Verarbeitet eine einzelne Episode - WebUI version"""
    if browser_id:
        if logger:
            logger.log(f"{browser_id} Processing: {url}", "info")

    # Get language preference from args
    language = getattr(args, 'language', '')

    m3u8_urls = await extractor.extract_metadata_and_m3u8(
        url,
        wait_time=args.wait,
        use_adblock=not args.no_adblock,
        browser_id=browser_id,
        language=language
    )

    if not m3u8_urls:
        if logger:
            logger.log(f"❌ No m3u8 found for {url}", "error")
        return False

    if args.series_display:
        extractor.metadata.series_name_display = args.series_display

    output_path = extractor.metadata.get_full_path(
        base_path=args.base_path,
        use_english_title=args.english_title,
        quality=args.quality_tag,
        format_ext=args.format
    )

    if output_path.exists() and not args.force:
        if logger:
            logger.log(f"⚠️ File exists, skipping: {output_path.name}", "warning")
        return True

    download_url = extractor.master_playlist if extractor.master_playlist else m3u8_urls[0]

    if not args.no_download:
        codec = getattr(args, 'codec', 'auto')  # Get codec preference, default to auto
        audio_only = getattr(args, 'audio_only', False)  # Get audio_only preference
        audio_format = getattr(args, 'audio_format', 'mp3')  # Get audio format preference
        audio_bitrate = getattr(args, 'audio_bitrate', '0')  # Get audio bitrate preference
        success = await download_video(download_url, output_path, args.quality, codec, logger, audio_only, audio_format, audio_bitrate)
        return success
    else:
        if logger:
            logger.log("✅ Metadata extracted (download skipped)", "success")
        return True


async def process_episode_with_semaphore(ep_num, base_url, season, args, episode_idx, total_episodes, browser_num, logger=None, on_complete=None, cancel_check=None):
    """Verarbeitet eine Episode mit globalem Semaphore (für Parallelisierung) - WebUI version

    Uses the global_episode_semaphore to limit total concurrent downloads across ALL series.

    Args:
        cancel_check: Optional callable that returns True if download should be cancelled
    """
    browser_id = f"[B{browser_num}]"
    episode_key = f"S{season:02d}E{ep_num:02d}"

    # Check if cancelled before even waiting for slot
    if cancel_check and cancel_check():
        if logger:
            logger.log(f"{browser_id} ⏹️ {episode_key} skipped (cancelled before queue)", "warning")
        return None

    # Log when waiting for global semaphore
    active = global_episode_semaphore.active_count
    logger.info(f"{episode_key} waiting for global slot... (active: {active}/{global_episode_semaphore.max_concurrent})")

    # Acquire global semaphore slot using non-blocking polling to avoid wasting executor threads
    acquired = False
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 3600
    while loop.time() < deadline:
        if global_episode_semaphore.acquire(timeout=0):
            acquired = True
            break
        # Check cancellation while waiting
        if cancel_check and cancel_check():
            if logger:
                logger.log(f"{browser_id} ⏹️ {episode_key} cancelled while waiting for slot", "warning")
            return None
        await asyncio.sleep(0.5)

    if not acquired:
        if logger:
            logger.log(f"{browser_id} ⏹️ {episode_key} timeout waiting for slot", "error")
        return False

    try:
        logger.info(f"{episode_key} acquired global slot, starting download... (active: {global_episode_semaphore.active_count}/{global_episode_semaphore.max_concurrent})")

        # Check if cancelled after acquiring slot
        if cancel_check and cancel_check():
            if logger:
                logger.log(f"{browser_id} ⏹️ {episode_key} skipped (cancelled)", "warning")
            return None

        url = f"{base_url}/staffel-{season}/episode-{ep_num}"

        if logger:
            logger.log(f"{browser_id} Starting {episode_key} ({episode_idx}/{total_episodes})", "info")

        try:
            extractor = HLSExtractor()
            result = await process_single_episode(extractor, url, args, browser_id, logger)

            # Check again after processing (in case cancelled during download)
            if cancel_check and cancel_check():
                if logger:
                    logger.log(f"{browser_id} ⏹️ {episode_key} cancelled after processing", "warning")
                return None

            if result:
                if logger:
                    logger.log(f"{browser_id} ✅ {episode_key} SUCCESS ({episode_idx}/{total_episodes})", "success")
            else:
                if logger:
                    logger.log(f"{browser_id} ❌ {episode_key} FAILED ({episode_idx}/{total_episodes})", "error")

            # Call completion callback if provided (for live progress updates)
            if on_complete:
                on_complete(ep_num, season, result)

            return result
        except asyncio.CancelledError:
            if logger:
                logger.log(f"{browser_id} ⏹️ {episode_key} cancelled", "warning")
            return None
        except Exception as e:
            if logger:
                logger.log(f"{browser_id} ❌ {episode_key} ERROR: {e}", "error")
            # Call completion callback for errors too
            if on_complete:
                on_complete(ep_num, season, False)
            return False
    finally:
        # Always release the global semaphore slot
        global_episode_semaphore.release()
        logger.info(f"{episode_key} released global slot (active: {global_episode_semaphore.active_count}/{global_episode_semaphore.max_concurrent})")


async def process_download_async(session_id, url, options):
    """Process download with async support"""
    try:
        logger = WebLogger(session_id)

        # Ensure session exists in active_downloads (for retries or late starts)
        if session_id not in active_downloads:
            active_downloads[session_id] = {
                'url': url,
                'status': 'processing',
                'current': 0,
                'total': 0,
                'options': options
            }
        else:
            # Update status
            active_downloads[session_id]['status'] = 'processing'
        socketio.emit('status', {
            'session_id': session_id,
            'status': 'processing',
            'message': 'Starting download process...'
        })

        # Parse URL with flexible format support
        base_url, series_slug, season, start_episode, url_type = parse_flexible_url(url)

        if not base_url:
            raise ValueError("Invalid URL format. Supported: .../NAME, .../NAME/staffel-N, .../NAME/staffel-N/episode-N")

        logger.log(f"Detected URL type: {url_type}", "info")
        logger.log(f"Series: {series_slug}", "info")

        # Parse season range if provided
        seasons_to_download = []
        if options.get('seasons'):
            season_range = str(options['seasons'])
            if season_range.lower() == 'all':
                # Auto-detect all seasons
                logger.log("Detecting all available seasons...", "info")
                total_seasons, _ = await detect_series_info(url)
                if total_seasons:
                    seasons_to_download = list(range(1, total_seasons + 1))
                    logger.log(f"Found {total_seasons} seasons: {seasons_to_download}", "info")
                else:
                    raise ValueError("Could not detect seasons")
            else:
                # Parse season range (e.g., "1-3" or "1,3,5")
                seasons_to_download = parse_episode_range(season_range)
                logger.log(f"Downloading seasons: {seasons_to_download}", "info")
        elif season:
            # Single season from URL
            seasons_to_download = [season]
        else:
            # No season specified, use season 1
            seasons_to_download = [1]

        # Handle different URL types (skip if using visual selector or batch mode with seasons='all')
        if not options.get('episodes_per_season') and not (options.get('seasons') and str(options['seasons']).lower() == 'all'):
            if url_type == 'series':
                # No season specified
                if options.get('season'):
                    season = options['season']
                    logger.log(f"Using specified season: {season}", "info")
                else:
                    # Auto-detect
                    logger.log("Auto-detecting seasons...", "info")
                    total_seasons, _ = await detect_series_info(url)

                    if total_seasons:
                        logger.log(f"Found {total_seasons} season(s)", "success")
                        if total_seasons == 1:
                            season = 1
                        else:
                            raise ValueError(f"Multiple seasons found ({total_seasons}). Please specify season.")
                    else:
                        raise ValueError("Could not detect seasons. Please specify season.")

            elif url_type == 'season':
                # Season specified, but no episode
                logger.log(f"Season: {season}", "info")

                if not options.get('episodes'):
                    # Auto-detect episodes
                    logger.log(f"Auto-detecting episodes for Season {season}...", "info")
                    season_url = f"{base_url}/staffel-{season}"
                    _, available_episodes = await detect_series_info(season_url)

                    if available_episodes:
                        logger.log(f"Found {len(available_episodes)} episode(s): {available_episodes[0]}-{available_episodes[-1]}", "success")
                        start_episode = available_episodes[0]
                    else:
                        logger.log("Could not detect episodes, defaulting to Episode 1", "warning")
                        start_episode = 1
        elif options.get('episodes_per_season'):
            logger.log("Using visual selector - skipping URL type validation", "info")
        else:
            logger.log("Using batch mode with seasons='all' - will auto-detect all episodes", "info")

        # Global counters for all seasons
        total_successful = 0
        total_failed = 0
        all_failed_episodes = []
        total_episodes_all_seasons = 0
        current_episode_global = 0

        # First pass: Count total episodes across all seasons
        episodes_per_season = {}
        episodes_per_extra = {}  # For Filme, Specials, etc.
        extras_to_download = []  # List of extra type names like 'filme', 'specials'

        # Check if episodes_per_season is provided from visual selector
        if options.get('episodes_per_season') and isinstance(options['episodes_per_season'], dict):
            logger.log("Using visual selector episode data", "info")
            # Convert string keys to int and ensure episodes are lists
            # Also handle extra_ prefixes for Filme, Specials, etc.
            for season_key, episodes_list in options['episodes_per_season'].items():
                if not episodes_list or len(episodes_list) == 0:
                    continue

                # Check if this is an extra tab (e.g., "extra_filme", "extra_specials")
                if str(season_key).startswith('extra_'):
                    extra_type = str(season_key).replace('extra_', '')
                    episodes_per_extra[extra_type] = episodes_list
                    total_episodes_all_seasons += len(episodes_list)
                    if extra_type not in extras_to_download:
                        extras_to_download.append(extra_type)
                    logger.log(f"📎 Found {len(episodes_list)} {extra_type.capitalize()} to download", "info")
                else:
                    # Regular season
                    season_num = int(season_key)
                    episodes_per_season[season_num] = episodes_list
                    total_episodes_all_seasons += len(episodes_list)
                    if season_num not in seasons_to_download:
                        seasons_to_download.append(season_num)

            # Sort seasons
            seasons_to_download.sort()
        else:
            # Fallback to old method
            if options.get('episodes'):
                episode_range = str(options['episodes'])
                if episode_range.lower() == 'all':
                    # Parallel detection of all seasons' episodes
                    async def detect_season_episodes(s_num):
                        season_url = f"{base_url}/staffel-{s_num}"
                        _, avail = await detect_series_info(season_url)
                        return s_num, avail

                    detection_tasks = [detect_season_episodes(s) for s in seasons_to_download]
                    detection_results = await asyncio.gather(*detection_tasks, return_exceptions=True)

                    for result in detection_results:
                        if isinstance(result, Exception):
                            continue
                        s_num, available_episodes = result
                        if available_episodes:
                            episodes_per_season[s_num] = available_episodes
                            total_episodes_all_seasons += len(available_episodes)
                        else:
                            logger.log(f"Could not detect episodes for Season {s_num}", "warning")
                            episodes_per_season[s_num] = []
                else:
                    episodes = parse_episode_range(episode_range)
                    for season_num in seasons_to_download:
                        episodes_per_season[season_num] = episodes
                        total_episodes_all_seasons += len(episodes)
            else:
                for season_num in seasons_to_download:
                    if start_episode:
                        episodes_per_season[season_num] = [start_episode]
                    else:
                        episodes_per_season[season_num] = [1]
                    total_episodes_all_seasons += 1

        # Set global total
        active_downloads[session_id]['total'] = total_episodes_all_seasons
        active_downloads[session_id]['current'] = 0
        active_downloads[session_id]['failed_episodes'] = []

        # Update queue manager with total episodes
        queue_manager.update_progress(
            session_id,
            total=total_episodes_all_seasons,
            completed=0
        )

        # Build info string including extras
        info_parts = []
        if seasons_to_download:
            info_parts.append(f"{len(seasons_to_download)} season(s)")
        if extras_to_download:
            info_parts.append(f"{len(extras_to_download)} extra(s): {', '.join(extras_to_download)}")
        logger.log(f"📊 Total episodes to download: {total_episodes_all_seasons} across {' + '.join(info_parts)}", "info")

        # Initialize episode status tracking for all episodes
        all_episode_keys = []
        for season_num in seasons_to_download:
            for ep_num in episodes_per_season.get(season_num, []):
                all_episode_keys.append(f"S{season_num:02d}E{ep_num:02d}")
        # Also add extras (use X prefix, e.g., XFilme01)
        for extra_type in extras_to_download:
            for ep_num in episodes_per_extra.get(extra_type, []):
                extra_label = extra_type.capitalize()[:4]  # e.g., "Film", "Spec"
                all_episode_keys.append(f"X{extra_label}{ep_num:02d}")
        queue_manager.init_episode_status(session_id, all_episode_keys)

        # Emit initial episode status to frontend
        socketio.emit('episode_status_init', {
            'session_id': session_id,
            'episodes': {key: {'status': 'queued', 'progress': 0} for key in all_episode_keys}
        })

        # Create args object ONCE (used for all seasons)
        class Args:
            def __init__(self, options):
                self.wait = options.get('wait', app_config.DEFAULT_WAIT_TIME)
                self.no_adblock = not options.get('adblock', True)
                self.base_path = Config.get_download_path()
                self.english_title = options.get('english_title', False)
                self.series_display = options.get('series_display', None)
                self.quality = options.get('quality', 'best')
                self.quality_tag = options.get('quality_tag', app_config.DEFAULT_QUALITY)
                self.format = options.get('format', app_config.DEFAULT_FORMAT)
                self.codec = options.get('codec', 'auto')
                self.no_download = False
                self.force = options.get('force', False)
                self.parallel = options.get('parallel', 1)
                self.audio_only = options.get('audio_only', False)
                self.audio_format = options.get('audio_format', 'mp3')
                self.audio_bitrate = options.get('audio_bitrate', '0')
                self.language = options.get('language', '')

        args = Args(options)
        parallel = args.parallel

        # =====================================================
        # PARALLEL MODE: Collect ALL tasks across ALL seasons
        # Then execute with a SINGLE gather() for true parallelism
        # =====================================================
        if parallel > 1 and total_episodes_all_seasons > 1:
            logger.log(f"🚀 Starting parallel processing (global limit: {global_episode_semaphore.max_concurrent})...", "info")
            logger.log(f"📊 Collecting tasks for {total_episodes_all_seasons} episodes across {len(seasons_to_download)} season(s)...", "info")

            with PrintFilter(logger):
                # Thread-safe counters for live progress updates
                import threading
                progress_lock = threading.Lock()

                def on_episode_complete(ep_num, ep_season, success):
                    """Callback called when each episode completes - sends live progress update"""
                    nonlocal total_successful, total_failed, all_failed_episodes
                    episode_key = f"S{ep_season:02d}E{ep_num:02d}"

                    with progress_lock:
                        new_status = 'completed' if success else 'failed'
                        queue_manager.update_episode_status(session_id, episode_key, status=new_status, progress=100 if success else 0)

                        socketio.emit('episode_status_update', {
                            'session_id': session_id,
                            'episode_key': episode_key,
                            'status': new_status,
                            'progress': 100 if success else 0
                        })

                        if success:
                            total_successful += 1
                            queue_manager.reset_retry_count(session_id, episode_key)
                        else:
                            total_failed += 1
                            all_failed_episodes.append(episode_key)
                            logger.log(f"❌ {episode_key} failed - use manual retry if needed", "error")

                        clear_progress_tracker(session_id, episode_key)

                        queue_manager.update_progress(
                            session_id,
                            completed=total_successful,
                            failed_episodes=all_failed_episodes
                        )

                        socketio.emit('progress', {
                            'session_id': session_id,
                            'current': total_successful,
                            'total': total_episodes_all_seasons,
                            'episode': f"{episode_key} {'✓' if success else '✗'}",
                            'completed': total_successful
                        })

                        emit_aggregated_progress(session_id, total_episodes_all_seasons, total_successful)

                def is_cancelled():
                    return active_downloads.get(session_id, {}).get('status') == 'cancelled'

                # =====================================================
                # COLLECT ALL TASKS FROM ALL SEASONS (no gather yet!)
                # =====================================================
                all_tasks = []
                task_info = []  # Track (season, ep_num) for each task
                max_concurrent = global_episode_semaphore.max_concurrent
                global_episode_idx = 0

                for season_num in seasons_to_download:
                    if is_cancelled():
                        break

                    episodes = episodes_per_season.get(season_num, [])
                    if not episodes:
                        continue

                    for ep_num in episodes:
                        global_episode_idx += 1
                        browser_num = ((global_episode_idx - 1) % max_concurrent) + 1
                        episode_key = f"S{season_num:02d}E{ep_num:02d}"
                        episode_logger = WebLogger(session_id, episode_key=episode_key)

                        task = process_episode_with_semaphore(
                            ep_num,
                            base_url,
                            season_num,
                            args,
                            global_episode_idx,
                            total_episodes_all_seasons,
                            browser_num,
                            episode_logger,
                            on_complete=on_episode_complete,
                            cancel_check=is_cancelled
                        )
                        all_tasks.append(task)
                        task_info.append((season_num, ep_num))

                logger.log(f"📋 Collected {len(all_tasks)} tasks, starting parallel execution...", "info")

                # Update progress for queued
                active_downloads[session_id]['current'] = 0
                socketio.emit('progress', {
                    'session_id': session_id,
                    'current': total_successful,
                    'total': total_episodes_all_seasons,
                    'episode': f"Starting {len(all_tasks)} episodes (parallel across all seasons)",
                    'completed': total_successful
                })

                # Periodic progress emitter
                progress_emitter_running = [True]

                async def periodic_progress_emitter():
                    while progress_emitter_running[0]:
                        await asyncio.sleep(2)
                        if progress_emitter_running[0]:
                            with progress_lock:
                                current_completed = total_successful
                            emit_aggregated_progress(session_id, total_episodes_all_seasons, current_completed)

                emitter_task = asyncio.create_task(periodic_progress_emitter())

                try:
                    # =====================================================
                    # SINGLE gather() FOR ALL TASKS ACROSS ALL SEASONS
                    # This allows true cross-season parallelism!
                    # =====================================================
                    results = await asyncio.gather(*all_tasks, return_exceptions=True)
                finally:
                    progress_emitter_running[0] = False
                    emitter_task.cancel()
                    try:
                        await emitter_task
                    except asyncio.CancelledError:
                        pass
                    clear_progress_tracker(session_id)

                # Process results
                cancelled_count = 0
                for i, result in enumerate(results):
                    season_num, ep_num = task_info[i]
                    episode_key = f"S{season_num:02d}E{ep_num:02d}"
                    if result is None:
                        cancelled_count += 1
                    elif isinstance(result, Exception):
                        with progress_lock:
                            if episode_key not in all_failed_episodes:
                                all_failed_episodes.append(episode_key)
                                logger.log(f"❌ {episode_key} - Exception: {str(result)}", "error")

                if cancelled_count > 0:
                    logger.log(f"⏹️ {cancelled_count} episodes were cancelled/skipped", "warning")

                queue_manager.update_progress(
                    session_id,
                    completed=total_successful,
                    failed_episodes=all_failed_episodes
                )

                socketio.emit('progress', {
                    'session_id': session_id,
                    'current': total_successful,
                    'total': total_episodes_all_seasons,
                    'episode': f"All seasons: {total_successful}/{total_episodes_all_seasons} completed",
                    'completed': total_successful
                })

                socketio.emit('status', {
                    'session_id': session_id,
                    'status': 'processing',
                    'message': f'Parallel processing completed: {total_successful} successful, {total_failed} failed',
                    'successful': total_successful,
                    'failed': total_failed,
                    'failed_episodes': all_failed_episodes
                })

        else:
            # =====================================================
            # SEQUENTIAL MODE: Process seasons one by one
            # =====================================================
            for season_num in seasons_to_download:
                if active_downloads[session_id]['status'] == 'cancelled':
                    logger.log("Download cancelled by user", "warning")
                    break

                logger.log(f"📺 Starting Season {season_num}", "info")
                season = season_num

                episodes = episodes_per_season.get(season_num, [])
                if not episodes:
                    logger.log(f"No episodes found for Season {season}, skipping", "warning")
                    continue

                total = len(episodes)
                successful = 0
                failed = 0
                failed_episodes = []

                # SEQUENTIAL MODE
                if len(episodes) > 1:
                    logger.log("📝 Sequential processing", "info")

                for i, ep_num in enumerate(episodes, 1):
                    if active_downloads[session_id]['status'] == 'cancelled':
                        logger.log("Download cancelled by user", "warning")
                        break

                    episode_url = f"{base_url}/staffel-{season}/episode-{ep_num}"

                    # Increment global counter
                    current_episode_global += 1

                    logger.log(f"Processing episode {current_episode_global}/{total_episodes_all_seasons}: S{season:02d}E{ep_num:02d}", "info")

                    # Update progress with global counters
                    active_downloads[session_id]['current'] = current_episode_global
                    socketio.emit('progress', {
                        'session_id': session_id,
                        'current': current_episode_global,
                        'total': total_episodes_all_seasons,
                        'episode': f"S{season:02d}E{ep_num:02d}"
                    })

                    try:
                        extractor = HLSExtractor()

                        # Create episode-specific logger with episode_key for progress tracking
                        episode_key = f"S{season:02d}E{ep_num:02d}"
                        episode_logger = WebLogger(session_id, episode_key=episode_key)

                        with PrintFilter(logger):
                            result = await process_single_episode(extractor, episode_url, args, "", episode_logger)

                        if result:
                            successful += 1
                            total_successful += 1

                            # Update queue manager with completed count
                            queue_manager.update_progress(
                                session_id,
                                completed=total_successful
                            )

                            # Emit progress event with COMPLETED count (not just started)
                            socketio.emit('progress', {
                                'session_id': session_id,
                                'current': total_successful,  # COMPLETED episodes
                                'total': total_episodes_all_seasons,
                                'episode': f"S{season:02d}E{ep_num:02d} ✓",
                                'completed': total_successful  # Explicit completed count
                            })

                            # Update success counter immediately with global totals
                            socketio.emit('status', {
                                'session_id': session_id,
                                'status': 'processing',
                                'message': f'Episode S{season:02d}E{ep_num:02d} completed',
                                'successful': total_successful,
                                'failed': total_failed
                            })
                        else:
                            failed += 1
                            total_failed += 1
                            failed_episodes.append(ep_num)
                            all_failed_episodes.append(f"S{season:02d}E{ep_num:02d}")
                            # Update failed counter immediately with global totals
                            socketio.emit('status', {
                                'session_id': session_id,
                                'status': 'processing',
                                'message': f'Episode S{season:02d}E{ep_num:02d} failed',
                                'successful': total_successful,
                                'failed': total_failed,
                                'failed_episodes': all_failed_episodes
                            })

                    except Exception as e:
                        failed += 1
                        total_failed += 1
                        failed_episodes.append(ep_num)
                        all_failed_episodes.append(f"S{season:02d}E{ep_num:02d}")
                        logger.log(f"❌ Episode {ep_num} - Error: {str(e)}", "error")
                        # Update failed counter immediately with global totals
                        socketio.emit('status', {
                            'session_id': session_id,
                            'status': 'processing',
                            'message': f'Episode S{season:02d}E{ep_num:02d} error',
                            'successful': total_successful,
                            'failed': total_failed,
                            'failed_episodes': all_failed_episodes
                        })

                # Update failed episodes in session
                active_downloads[session_id]['failed_episodes'] = all_failed_episodes

                logger.log(f"✅ Season {season} completed: {successful} successful, {failed} failed (Total: {total_successful}/{total_episodes_all_seasons})", "info")

            # End of season loop

        # Process extras (Filme, Specials, etc.) after seasons
        for extra_type in extras_to_download:
            if active_downloads[session_id]['status'] == 'cancelled':
                logger.log("Download cancelled by user", "warning")
                break

            extra_label = extra_type.capitalize()
            logger.log(f"📎 Starting {extra_label}", "info")

            # Get episodes for this extra
            episodes = episodes_per_extra.get(extra_type, [])
            if not episodes:
                logger.log(f"No episodes found for {extra_label}, skipping", "warning")
                continue

            total = len(episodes)
            successful = 0
            failed = 0
            failed_episodes = []

            # Determine URL pattern for this extra type
            # filme -> /filme/film-N
            # specials -> /specials/episode-N (specials usually use episode-N)
            # ova -> /ova/episode-N
            if extra_type == 'filme':
                url_suffix = 'film'
            else:
                url_suffix = 'episode'

            # Sequential processing for extras (simpler for now)
            for i, ep_num in enumerate(episodes, 1):
                if active_downloads[session_id]['status'] == 'cancelled':
                    logger.log("Download cancelled by user", "warning")
                    break

                # Build extra URL: base_url/filme/film-1 or base_url/specials/episode-1
                episode_url = f"{base_url}/{extra_type}/{url_suffix}-{ep_num}"

                # Increment global counter
                current_episode_global += 1

                # Episode key for tracking (e.g., XFilm01)
                episode_key = f"X{extra_label[:4]}{ep_num:02d}"

                logger.log(f"Processing {extra_label} {ep_num} ({current_episode_global}/{total_episodes_all_seasons})", "info")

                # Update progress
                active_downloads[session_id]['current'] = current_episode_global
                socketio.emit('progress', {
                    'session_id': session_id,
                    'current': current_episode_global,
                    'total': total_episodes_all_seasons,
                    'episode': f"{extra_label} {ep_num}"
                })

                try:
                    extractor = HLSExtractor()

                    # Create episode-specific logger with episode_key for progress tracking
                    episode_logger = WebLogger(session_id, episode_key=episode_key)

                    with PrintFilter(logger):
                        result = await process_single_episode(extractor, episode_url, args, "", episode_logger)

                    if result:
                        successful += 1
                        total_successful += 1

                        queue_manager.update_progress(session_id, completed=total_successful)

                        socketio.emit('progress', {
                            'session_id': session_id,
                            'current': total_successful,
                            'total': total_episodes_all_seasons,
                            'episode': f"{extra_label} {ep_num} ✓",
                            'completed': total_successful
                        })

                        socketio.emit('status', {
                            'session_id': session_id,
                            'status': 'processing',
                            'message': f'{extra_label} {ep_num} completed',
                            'successful': total_successful,
                            'failed': total_failed
                        })
                    else:
                        failed += 1
                        total_failed += 1
                        failed_episodes.append(ep_num)
                        all_failed_episodes.append(episode_key)
                        logger.log(f"❌ {extra_label} {ep_num} - Failed", "error")

                except Exception as e:
                    failed += 1
                    total_failed += 1
                    failed_episodes.append(ep_num)
                    all_failed_episodes.append(episode_key)
                    logger.log(f"❌ {extra_label} {ep_num} - Error: {str(e)}", "error")

            # Update failed episodes in session
            active_downloads[session_id]['failed_episodes'] = all_failed_episodes

            logger.log(f"✅ {extra_label} completed: {successful} successful, {failed} failed (Total: {total_successful}/{total_episodes_all_seasons})", "info")

        # End of extras loop

        # =====================================================
        # CHECK FOR PENDING MERGED EPISODES
        # Episodes that were added via merge_episodes() while download was running
        # =====================================================
        while True:
            queue_item = queue_manager.get_item(session_id)
            if not queue_item or not queue_item.pending_merged_episodes:
                break

            # Check if cancelled
            if active_downloads.get(session_id, {}).get('status') == 'cancelled':
                break

            # Get and clear pending episodes
            pending_eps = dict(queue_item.pending_merged_episodes)
            queue_item.pending_merged_episodes = {}
            queue_manager.save_queue()

            pending_count = sum(len(eps) for eps in pending_eps.values())
            logger.log(f"🔗 Processing {pending_count} merged episodes...", "info")

            # Update total count
            total_episodes_all_seasons += pending_count
            active_downloads[session_id]['total'] = total_episodes_all_seasons

            # Process pending episodes (sequential mode for simplicity)
            for season_num, episodes in sorted(pending_eps.items()):
                if active_downloads.get(session_id, {}).get('status') == 'cancelled':
                    break

                for ep_num in sorted(episodes):
                    if active_downloads.get(session_id, {}).get('status') == 'cancelled':
                        break

                    episode_key = f"S{season_num:02d}E{ep_num:02d}"
                    episode_url = f"{base_url}/staffel-{season_num}/episode-{ep_num}"
                    current_episode_global += 1

                    logger.log(f"🔗 Merged: {episode_key} ({current_episode_global}/{total_episodes_all_seasons})", "info")

                    # Update progress
                    active_downloads[session_id]['current'] = current_episode_global
                    socketio.emit('progress', {
                        'session_id': session_id,
                        'current': current_episode_global,
                        'total': total_episodes_all_seasons,
                        'episode': f"{episode_key} (merged)"
                    })

                    try:
                        extractor = HLSExtractor()
                        # Create episode-specific logger with episode_key for progress tracking
                        episode_logger = WebLogger(session_id, episode_key=episode_key)
                        result = await process_single_episode(extractor, episode_url, args, "", episode_logger)

                        if result:
                            total_successful += 1
                            queue_manager.update_progress(session_id, completed=total_successful)
                            queue_manager.update_episode_status(session_id, episode_key, 'completed', 100)

                            socketio.emit('progress', {
                                'session_id': session_id,
                                'current': total_successful,
                                'total': total_episodes_all_seasons,
                                'episode': f"{episode_key} ✓",
                                'completed': total_successful
                            })
                        else:
                            total_failed += 1
                            all_failed_episodes.append(episode_key)
                            queue_manager.update_episode_status(session_id, episode_key, 'failed', 0)
                            logger.log(f"❌ {episode_key} - Failed", "error")

                    except Exception as e:
                        total_failed += 1
                        all_failed_episodes.append(episode_key)
                        queue_manager.update_episode_status(session_id, episode_key, 'failed', 0)
                        logger.log(f"❌ {episode_key} - Error: {str(e)}", "error")

            # Update failed episodes
            active_downloads[session_id]['failed_episodes'] = all_failed_episodes
            queue_manager.update_progress(session_id, completed=total_successful, failed_episodes=all_failed_episodes)

        # Final status for all seasons and extras
        if session_id in active_downloads and active_downloads[session_id]['status'] != 'cancelled':
            active_downloads[session_id]['status'] = 'completed'
            active_downloads[session_id]['completed_at'] = datetime.now()  # For memory cleanup

            # Build message with season and extras summary
            content_count = len(seasons_to_download) + len(extras_to_download)
            if content_count > 1:
                parts = []
                if seasons_to_download:
                    parts.append(f"{len(seasons_to_download)} season(s)")
                if extras_to_download:
                    parts.append(f"{len(extras_to_download)} extra(s)")
                message = f'All {" + ".join(parts)} completed! Total Success: {total_successful}, Total Failed: {total_failed}'
                if all_failed_episodes:
                    failed_str = ', '.join(all_failed_episodes[:10])  # Show first 10
                    if len(all_failed_episodes) > 10:
                        failed_str += f' ... and {len(all_failed_episodes) - 10} more'
                    message += f' (Failed: {failed_str})'
            else:
                message = f'Download completed! Success: {total_successful}, Failed: {total_failed}'
                if all_failed_episodes:
                    failed_str = ', '.join(all_failed_episodes)
                    message += f' (Failed: {failed_str})'

            socketio.emit('status', {
                'session_id': session_id,
                'status': 'completed',
                'message': message,
                'successful': total_successful,
                'failed': total_failed,
                'failed_episodes': all_failed_episodes
            })

            # Final update of completed episodes count in queue
            queue_manager.update_progress(
                session_id,
                completed=total_successful,
                failed_episodes=all_failed_episodes
            )

            # If this is a retry, update the original item's stats
            if '_retry_' in session_id:
                original_session_id = session_id.split('_retry_')[0]
                original_item = queue_manager.get_item(original_session_id)
                if original_item:
                    # Get successfully retried episodes (those we downloaded minus those that failed again)
                    successful_retries = []
                    for season_num in episodes_per_season:
                        for ep_num in episodes_per_season.get(season_num, []):
                            ep_key = f"S{season_num:02d}E{ep_num:02d}"
                            if ep_key not in all_failed_episodes:
                                successful_retries.append(ep_key)

                    if successful_retries:
                        # Remove successful retries from original's failed_episodes
                        updated_failed = [ep for ep in original_item.failed_episodes if ep not in successful_retries]
                        # Update original's completed count
                        new_completed = original_item.completed_episodes + len(successful_retries)
                        queue_manager.update_progress(
                            original_session_id,
                            completed=new_completed,
                            failed_episodes=updated_failed
                        )
                        logger.log(f"✅ Updated original item: +{len(successful_retries)} completed, {len(updated_failed)} still failed", "info")

                        # Remove retry item from queue (merged into original)
                        queue_manager.remove_item(session_id)
                        logger.log(f"🗑️ Removed retry item (merged into original)", "info")

    except Exception as e:
        if session_id in active_downloads:
            active_downloads[session_id]['status'] = 'error'
        socketio.emit('status', {
            'session_id': session_id,
            'status': 'error',
            'message': f'Error: {str(e)}'
        })


def run_download_thread(session_id, url, options):
    """Run download in a separate thread with its own asyncio event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_download_async(session_id, url, options))
    finally:
        loop.close()


@app.route('/')
def index():
    """Main page"""
    # Pass config values to template for dynamic limits
    config_values = {
        'max_parallel_limit': app_config.MAX_PARALLEL_LIMIT,
        'max_parallel_downloads': app_config.MAX_PARALLEL_DOWNLOADS,
        'default_format': app_config.DEFAULT_FORMAT,
        'default_quality': app_config.DEFAULT_QUALITY,
        'default_wait_time': app_config.DEFAULT_WAIT_TIME
    }
    return render_template('index.html', config=config_values)


@app.route('/api/start', methods=['POST'])
def start_download():
    """Add download to queue (or merge with existing if same series)"""
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    options = data.get('options', {})

    # DEBUG: Log incoming options including language
    logger.debug(f"/api/start received:")
    logger.debug(f"URL: {url}")
    logger.debug(f"Language in options: '{options.get('language', 'NOT FOUND')}'")
    logger.debug(f"All options keys: {list(options.keys())}")
    episodes_per_season = data.get('episodes_per_season')
    logger.debug(f"episodes_per_season: {episodes_per_season}")
    logger.debug(f"seasons: {options.get('seasons')}")
    logger.debug(f"episodes: {options.get('episodes')}")

    # Extract series name from URL if not already provided
    if not options.get('series_display'):
        try:
            base_url, series_slug, season, start_episode, url_type = parse_flexible_url(url)
            if series_slug:
                # Convert slug to readable name (replace hyphens with spaces and title case)
                series_name = series_slug.replace('-', ' ').title()
                options['series_display'] = series_name
        except Exception as e:
            # If parsing fails, use a default name
            logger.warning(f"Could not extract series name from URL: {e}")
            options['series_display'] = 'Unknown Series'

    # Check if this series is already in the queue (queued or processing)
    existing_item = queue_manager.find_existing_series(url)

    if existing_item and episodes_per_season:
        # Merge episodes into existing queue item
        merge_result = queue_manager.merge_episodes(existing_item.session_id, episodes_per_season)

        if merge_result['success']:
            # Start queue processor if not running
            start_queue_processor()

            return jsonify({
                'session_id': existing_item.session_id,
                'status': 'merged',
                'merged_into_existing': True,
                'added_episodes': merge_result['added_episodes'],
                'already_exists': merge_result['already_exists'],
                'total_new': merge_result['total_new'],
                'series_name': existing_item.series_name,
                'message': f"{merge_result['total_new']} new episode(s) added to existing download queue"
            })

    # No existing item found or no episodes specified - create new queue entry
    import uuid
    session_id = str(uuid.uuid4())

    # Add to queue
    queue_item = queue_manager.add_to_queue(
        session_id=session_id,
        url=url,
        options=options,
        episodes_per_season=episodes_per_season
    )

    # Store in active_downloads for backward compatibility
    active_downloads[session_id] = {
        'url': url,
        'status': 'queued',
        'current': 0,
        'total': 0,
        'options': options,
        'series_name': options.get('series_display', 'Unknown Series')
    }

    # Start queue processor if not running
    start_queue_processor()

    return jsonify({
        'session_id': session_id,
        'status': 'queued',
        'merged_into_existing': False,
        'queue_position': queue_manager.get_queue_position(session_id),
        'series_name': options.get('series_display', 'Unknown Series')
    })


@app.route('/api/cancel/<session_id>', methods=['POST'])
def cancel_download(session_id):
    """Cancel a download (both active and in queue)"""
    cancelled = False

    # Update active_downloads status (stops the download loop)
    if session_id in active_downloads:
        active_downloads[session_id]['status'] = 'cancelled'
        cancelled = True
        logger.info(f"Cancelled active download: {session_id}")

    # Also update queue status so UI shows cancelled
    queue_result = queue_manager.update_status(session_id, DownloadStatus.CANCELLED)
    if queue_result:
        cancelled = True
        logger.info(f"Updated queue status to cancelled: {session_id}")

    # Emit status update via WebSocket for immediate UI update
    if cancelled:
        socketio.emit('status', {
            'session_id': session_id,
            'status': 'cancelled',
            'message': 'Download abgebrochen'
        })
        return jsonify({'status': 'cancelled'})

    return jsonify({'error': 'Session not found'}), 404


@app.route('/api/status/<session_id>')
def get_status(session_id):
    """Get download status"""
    if session_id in active_downloads:
        return jsonify(active_downloads[session_id])
    return jsonify({'error': 'Session not found'}), 404


def start_queue_processor():
    """
    Start the robust queue processor and background auto-scraper.
    Uses the new RobustQueueProcessor class for better stability.
    """
    logger.info(f"start_queue_processor() called | Currently running: {queue_processor.is_running}")

    # Use the new robust processor
    started = queue_processor.start()

    # Start the background auto-scraper and dynamic limiter
    if started:
        auto_scraper.start()
        dynamic_limiter.start()

    return started


def stop_queue_processor(timeout: float = 30.0):
    """
    Gracefully stop the queue processor and auto-scraper.
    """
    logger.info(f"stop_queue_processor() called")

    # Stop auto-scraper and dynamic limiter first
    auto_scraper.stop(timeout=10.0)
    dynamic_limiter.stop()

    stopped = queue_processor.stop(timeout=timeout)

    return stopped


@app.route('/api/parse-url', methods=['POST'])
def parse_url():
    """Parse URL and detect available episodes - Full scraping with caching"""
    data = request.json
    url = data.get('url')
    force_refresh = data.get('force_refresh', False)

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    try:
        # Parse URL
        base_url, series_slug, season, start_episode, url_type = parse_flexible_url(url)

        if not base_url:
            return jsonify({'error': 'Invalid URL format'}), 400

        # Try loading from cache (unless force_refresh is requested)
        if not force_refresh:
            cached_data = load_from_cache(series_slug)
            if cached_data:
                logger.info(f"Using cached data for {series_slug}")
                # Return cached data with cache indicator
                return jsonify({
                    'from_cache': True,
                    **cached_data
                })

        # Create async function to scrape ALL seasons and episodes
        async def scrape_all_seasons():
            result = {
                'url_type': url_type,
                'series_slug': series_slug,
                'series_name': None,  # Real series name from page (not URL slug)
                'season': season,
                'start_episode': start_episode,
                'total_seasons': None,
                'season_tabs': [],  # List of actual numeric seasons
                'extra_tabs': [],   # List of extra tabs like 'filme', 'specials'
                'seasons_data': {},  # { season_num: { episodes: [...], episode_details: {...} } }
                'extras_data': {},   # { 'filme': { episodes: [...], episode_details: {...} }, ... }
                'series_cover_url': None,  # Cover image URL
                'series_description': None,  # Series description
                'languages': []  # Available languages (extracted from first season)
            }

            try:
                # Step 1: Detect total number of seasons, scrape cover and description
                # Remove /staffel-N, /filme, /specials, /ova, /movies from URL to get base series URL
                series_url = base_url
                for suffix in ['/staffel-', '/filme', '/specials', '/ova', '/movies']:
                    if suffix in series_url:
                        series_url = series_url.rsplit(suffix, 1)[0]
                        break
                series_page_data = await scrape_series_page(series_url)

                total_seasons = series_page_data['total_seasons']
                season_tabs = series_page_data['season_tabs']
                extra_tabs = series_page_data['extra_tabs']
                cover_url = series_page_data['cover_url']
                description = series_page_data['description']
                series_name = series_page_data.get('series_name')

                result['series_name'] = series_name
                result['series_cover_url'] = cover_url
                result['series_description'] = description
                result['season_tabs'] = season_tabs
                result['extra_tabs'] = extra_tabs

                if not total_seasons:
                    # If we can't detect from series page, try to infer from URL
                    if url_type in ['season', 'full'] and season:
                        total_seasons = season  # At least this many seasons
                    else:
                        total_seasons = 1

                result['total_seasons'] = total_seasons

                # Step 2: Scrape ALL seasons in parallel with browser pool
                # Calculate total tasks (seasons + extra tabs)
                total_tasks = total_seasons + len(extra_tabs)
                logger.info(f"Scraping {total_seasons} season(s)" + (f" + {len(extra_tabs)} extra tab(s)" if extra_tabs else "") + f" for {series_slug}...")

                # Use browser pool for efficient parallel scraping
                from app.browser_pool import BrowserPool

                async with BrowserPool(pool_size=min(3, max(total_tasks, 1))) as browser_pool:
                    # Create tasks for regular seasons
                    season_tasks = []
                    for s in range(1, total_seasons + 1):
                        s_url = f"{series_url}/staffel-{s}"
                        season_tasks.append(scrape_season_details_pooled(s_url, s, browser_pool))

                    # Create tasks for extra tabs (Filme, Specials, etc.)
                    extra_tasks = []
                    for extra_tab in extra_tabs:
                        extra_url = f"{series_url}/{extra_tab}"
                        extra_tasks.append(scrape_season_details_pooled(extra_url, extra_tab, browser_pool))

                    # Execute all scrapes in parallel
                    all_tasks = season_tasks + extra_tasks
                    all_results = await asyncio.gather(*all_tasks, return_exceptions=True)

                # Process season results
                season_results = all_results[:total_seasons]
                for s_num, s_result in enumerate(season_results, 1):
                    if isinstance(s_result, Exception):
                        logger.warning(f"Season {s_num} failed: {s_result}")
                        result['seasons_data'][s_num] = {
                            'episodes': [],
                            'episode_details': {}
                        }
                    else:
                        result['seasons_data'][s_num] = s_result
                        logger.info(f"Season {s_num}: {len(s_result['episodes'])} episodes")

                        # Extract languages from first successful season (if not already set)
                        if not result['languages'] and s_result.get('languages'):
                            result['languages'] = s_result['languages']
                            lang_names = [l.get('name', l.get('key', '?')) for l in result['languages']]
                            logger.info(f"Found languages: {', '.join(lang_names)}")

                # Process extra tab results
                extra_results = all_results[total_seasons:]
                for idx, extra_tab in enumerate(extra_tabs):
                    e_result = extra_results[idx]
                    if isinstance(e_result, Exception):
                        logger.warning(f"{extra_tab.capitalize()} failed: {e_result}")
                        result['extras_data'][extra_tab] = {
                            'episodes': [],
                            'episode_details': {}
                        }
                    else:
                        result['extras_data'][extra_tab] = e_result
                        logger.info(f"{extra_tab.capitalize()}: {len(e_result['episodes'])} episodes")

            except Exception as e:
                logger.error(f"Error scraping series: {e}")
                # Fallback: at least return the requested season
                if url_type in ['season', 'full'] and season:
                    result['total_seasons'] = season
                    result['seasons_data'][season] = {
                        'episodes': list(range(1, 13)),
                        'episode_details': {}
                    }

            return result

        # Helper function to scrape series page for seasons count, cover, and description
        async def scrape_series_page(series_url):
            """Scrape series page for total seasons, cover image, description, extra tabs, and languages"""
            from playwright.async_api import async_playwright
            import asyncio

            result = {
                'total_seasons': None,
                'season_tabs': [],
                'extra_tabs': [],
                'cover_url': None,
                'description': None,
                'languages': []
            }

            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        headless=True,
                        args=['--mute-audio', '--disable-dev-shm-usage']
                    )

                    context = await browser.new_context()
                    page = await context.new_page()
                    page.set_default_timeout(30000)  # 30 seconds timeout

                    # OPTIMIZED: Use domcontentloaded instead of networkidle (2-3x faster)
                    await page.goto(series_url, wait_until="domcontentloaded", timeout=15000)

                    # OPTIMIZED: Extract all data in ONE JavaScript evaluation
                    page_data = await page.evaluate('''
                        () => {
                            const data = {
                                seasons: null,
                                seasonTabs: [],
                                extraTabs: [],
                                cover: null,
                                description: null,
                                languages: [],
                                seriesName: null
                            };

                            // Extract series name from h1[itemprop="name"]
                            const h1Element = document.querySelector('h1[itemprop="name"]');
                            if (h1Element) {
                                // Try to get the span inside first (cleaner name)
                                const spanElement = h1Element.querySelector('span');
                                if (spanElement) {
                                    data.seriesName = spanElement.textContent.trim();
                                } else {
                                    data.seriesName = h1Element.textContent.trim();
                                }
                            }

                            // Read season tabs directly from DOM to distinguish real seasons from extras
                            // Try multiple selectors to find season/extra tabs
                            const seasonLinks = document.querySelectorAll(
                                '.hosterSiteDirect498Link, ' +
                                'a[href*="/staffel-"], ' +
                                'a[href*="/filme"], ' +
                                'a[href*="/specials"], ' +
                                'a[href*="/ova"], ' +
                                'a[href*="/movies"], ' +
                                '.seasonLink, ' +
                                '#stream ul li a, ' +
                                '.seriesListContainer a'
                            );
                            const numericSeasons = [];
                            const extraTabs = [];

                            seasonLinks.forEach(link => {
                                const href = link.getAttribute('href') || '';
                                const text = link.textContent.trim().toLowerCase();

                                // Check if it's a numeric season
                                const seasonMatch = href.match(/\\/staffel-(\\d+)/);
                                if (seasonMatch) {
                                    numericSeasons.push(parseInt(seasonMatch[1]));
                                }
                                // Check for extra tabs by href pattern
                                else if (href.includes('/filme')) {
                                    extraTabs.push('filme');
                                }
                                else if (href.includes('/specials')) {
                                    extraTabs.push('specials');
                                }
                                else if (href.includes('/ova')) {
                                    extraTabs.push('ova');
                                }
                                else if (href.includes('/movies')) {
                                    extraTabs.push('movies');
                                }
                                // Check by text content if href doesn't match
                                else if (text === 'filme' || text === 'films' || text === 'movie' || text === 'movies') {
                                    extraTabs.push('filme');
                                }
                                else if (text === 'specials' || text === 'special') {
                                    extraTabs.push('specials');
                                }
                                else if (text === 'ova' || text === 'ovas') {
                                    extraTabs.push('ova');
                                }
                            });

                            // Use actual numeric seasons count, not numberOfSeasons meta (which includes extras)
                            if (numericSeasons.length > 0) {
                                data.seasons = Math.max(...numericSeasons);
                                data.seasonTabs = [...new Set(numericSeasons)].sort((a, b) => a - b);
                            } else {
                                // Fallback to meta tag if no season links found
                                const seasonsMeta = document.querySelector('meta[itemprop="numberOfSeasons"]');
                                if (seasonsMeta) {
                                    const metaSeasons = parseInt(seasonsMeta.getAttribute('content'));
                                    // If we found extra tabs but no numeric seasons, the meta count might include extras
                                    // Subtract extras from count
                                    data.seasons = Math.max(1, metaSeasons - extraTabs.length);
                                }
                            }

                            data.extraTabs = [...new Set(extraTabs)];

                            // Get cover image - try new structure first, fallback to old
                            let coverImg = document.querySelector('picture img.img-fluid.w-100.loaded[alt]');
                            if (!coverImg || (coverImg.getAttribute('src') && coverImg.getAttribute('src').includes('base64'))) {
                                // Try to find img with data-src attribute containing '/channel/'
                                const allImgs = document.querySelectorAll('picture img[data-src*="/channel/"]');
                                if (allImgs.length > 0) coverImg = allImgs[0];
                            }
                            if (!coverImg) {
                                // Fallback to old selector
                                coverImg = document.querySelector('.seriesCoverBox img');
                            }
                            if (coverImg) {
                                data.cover = coverImg.getAttribute('data-src') || coverImg.getAttribute('src');
                            }

                            // Get description - try new structure first, fallback to old
                            let descElem = document.querySelector('span.description-text');
                            if (descElem) {
                                data.description = descElem.textContent.trim();
                            } else {
                                // Fallback to old selector
                                descElem = document.querySelector('p.seri_des[data-full-description]');
                                if (descElem) {
                                    let desc = descElem.getAttribute('data-full-description');
                                    // Remove series name prefix [Name]
                                    if (desc && desc.startsWith('[')) {
                                        const bracketEnd = desc.indexOf(']');
                                        if (bracketEnd !== -1) {
                                            desc = desc.substring(bracketEnd + 1).trim();
                                        }
                                    }
                                    data.description = desc;
                                }
                            }

                            // Extract available languages from changeLanguageBox - DYNAMIC detection
                            const langBox = document.querySelector('.changeLanguageBox');
                            if (langBox) {
                                const langImages = langBox.querySelectorAll('img[data-lang-key]');
                                langImages.forEach(img => {
                                    const langKey = img.getAttribute('data-lang-key');
                                    const title = img.getAttribute('title') || '';
                                    const alt = img.getAttribute('alt') || '';
                                    const isSelected = img.classList.contains('selectedLanguage');
                                    const src = img.getAttribute('src') || '';
                                    const srcFilename = src.split('/').pop().replace('.svg', '').toLowerCase();
                                    const langName = title || (alt ? alt.split(',')[0].trim() : srcFilename);

                                    data.languages.push({
                                        key: langKey,
                                        name: langName,
                                        selected: isSelected,
                                        icon: src,
                                        srcFile: srcFilename,
                                        title: title,
                                        alt: alt
                                    });
                                });
                            }

                            return data;
                        }
                    ''')

                    result['total_seasons'] = page_data.get('seasons')
                    result['season_tabs'] = page_data.get('seasonTabs', [])
                    result['extra_tabs'] = page_data.get('extraTabs', [])
                    result['cover_url'] = page_data.get('cover')
                    result['description'] = page_data.get('description')
                    result['languages'] = page_data.get('languages', [])
                    result['series_name'] = page_data.get('seriesName')

                    # Log series name if found
                    if result['series_name']:
                        logger.debug(f"Series name: {result['series_name']}")

                    # Log languages if found
                    if result['languages']:
                        lang_names = [l.get('name', l.get('key', '?')) for l in result['languages']]
                        logger.debug(f"Found languages: {', '.join(lang_names)}")

                    # Make absolute URL if relative
                    if result['cover_url'] and result['cover_url'].startswith('/'):
                        from urllib.parse import urlparse
                        parsed = urlparse(series_url)
                        result['cover_url'] = f"{parsed.scheme}://{parsed.netloc}{result['cover_url']}"

                    # Log extra tabs if found
                    if result['extra_tabs']:
                        logger.debug(f"Found extra tabs: {result['extra_tabs']}")

                    await browser.close()

            except Exception as e:
                logger.error(f"Failed to scrape series page: {e}")

            return result

        # Pooled version using browser pool for parallel scraping
        async def scrape_season_details_pooled(season_url, season_num, browser_pool, max_retries=2):
            """Scrape episode list using browser pool - OPTIMIZED for parallel execution"""
            from app.browser_pool import PooledPageContext

            season_data = {
                'episodes': [],
                'episode_details': {},
                'languages': []  # Available languages on this season page
            }

            for attempt in range(max_retries + 1):
                try:
                    # Use pooled page context
                    async with PooledPageContext(browser_pool) as page:
                        # Navigate to season page
                        await page.goto(season_url, wait_until="domcontentloaded", timeout=15000)

                        # Extract ALL episode data in ONE JavaScript evaluation
                        # Also handles /film- links for Filme pages
                        episode_data = await page.evaluate('''
                            () => {
                                const episodes = new Set();
                                const details = {};
                                const languages = [];

                                // Helper function to extract language from flag image
                                const extractLanguageFromFlag = (img) => {
                                    const src = img.getAttribute('src') || '';
                                    const title = img.getAttribute('title') || '';
                                    const alt = img.getAttribute('alt') || '';
                                    const srcFilename = src.split('/').pop().replace('.svg', '').toLowerCase();

                                    // Build a readable name from available attributes
                                    // For anime: japanese-german.svg -> "Japanisch mit deutschem Untertitel"
                                    let langName = srcFilename;
                                    if (title) {
                                        // Use full title, not split - keep "Mit deutschem Untertitel" etc.
                                        langName = title.trim();
                                    } else if (alt) {
                                        langName = alt.split(',')[0].trim();
                                    } else {
                                        // Fallback: Generate readable name from srcFile
                                        const readableNames = {
                                            'german': 'Deutsch',
                                            'english': 'Englisch',
                                            'japanese': 'Japanisch',
                                            'japanese-german': 'Japanisch (Deutsche UT)',
                                            'japanese-english': 'Japanisch (Englische UT)',
                                            'english-german': 'Englisch (Deutsche UT)'
                                        };
                                        langName = readableNames[srcFilename] || srcFilename;
                                    }

                                    // Map SVG filename to actual data-lang-key values used on the site
                                    const langKeyMap = {
                                        'german': '1',
                                        'english': '2',
                                        'english-german': '3',  // Englisch mit deutschen Untertiteln
                                        'japanese-german': '3', // Japanisch mit dt. UT
                                        'japanese-english': 'japanese-english', // Keep as-is for anime
                                        'french': '4',
                                        'spanish': '5',
                                        'gersub': '3',          // German subtitles variant
                                        'engsub': '3'           // English subtitles variant
                                    };
                                    const langKey = langKeyMap[srcFilename] || srcFilename;

                                    return {
                                        key: langKey,
                                        name: langName,
                                        icon: src,
                                        srcFile: srcFilename,
                                        title: title,  // Store raw title for frontend
                                        alt: alt       // Store raw alt for frontend
                                    };
                                };

                                // Get all episode AND film links (for Filme pages)
                                const links = document.querySelectorAll('a[href*="/episode-"], a[href*="/film-"]');
                                links.forEach(link => {
                                    const href = link.getAttribute('href');
                                    if (href) {
                                        try {
                                            // Handle both /episode-N and /film-N patterns
                                            let epMatch = null;
                                            if (href.includes('/episode-')) {
                                                epMatch = href.split('/episode-')[1];
                                            } else if (href.includes('/film-')) {
                                                epMatch = href.split('/film-')[1];
                                            }
                                            if (epMatch) {
                                                const epNum = parseInt(epMatch.split('/')[0].split('?')[0]);
                                                if (!isNaN(epNum)) {
                                                    episodes.add(epNum);
                                                }
                                            }
                                        } catch (e) {}
                                    }
                                });

                                // Get episode titles AND languages from rows
                                const rows = document.querySelectorAll('tr[data-episode-id], .episodeWrapper');
                                const allLangsSet = new Set();
                                rows.forEach(row => {
                                    try {
                                        const epLink = row.querySelector('a[href*="/episode-"], a[href*="/film-"]');
                                        if (epLink) {
                                            const href = epLink.getAttribute('href');
                                            let epNum = null;
                                            if (href.includes('/episode-')) {
                                                epNum = parseInt(href.split('/episode-')[1].split('/')[0]);
                                            } else if (href.includes('/film-')) {
                                                epNum = parseInt(href.split('/film-')[1].split('/')[0]);
                                            }

                                            if (epNum && !isNaN(epNum)) {
                                                const deTitleElem = row.querySelector('.episodeGermanTitle, .seasonEpisodeTitle strong');
                                                const enTitleElem = row.querySelector('.episodeEnglishTitle, small');

                                                const titleDe = deTitleElem ? deTitleElem.textContent.trim() : '';
                                                const titleEn = enTitleElem ? enTitleElem.textContent.trim() : '';

                                                // Extract languages for this specific episode
                                                const episodeLangs = [];
                                                const seenInEpisode = new Set();
                                                const flagsInRow = row.querySelectorAll('td.editFunctions img.flag, .editFunctions img.flag, img.flag');
                                                flagsInRow.forEach(img => {
                                                    const lang = extractLanguageFromFlag(img);
                                                    if (!seenInEpisode.has(lang.srcFile)) {
                                                        seenInEpisode.add(lang.srcFile);
                                                        episodeLangs.push(lang);
                                                        allLangsSet.add(JSON.stringify(lang));
                                                    }
                                                });

                                                details[epNum] = {
                                                    title_de: titleDe,
                                                    title_en: titleEn,
                                                    languages: episodeLangs
                                                };
                                            }
                                        }
                                    } catch (e) {}
                                });

                                // Build overall languages list from all episodes
                                allLangsSet.forEach(langJson => {
                                    languages.push(JSON.parse(langJson));
                                });

                                // Fallback: Also check changeLanguageBox (on episode pages)
                                if (languages.length === 0) {
                                    const langBox = document.querySelector('.changeLanguageBox');
                                    if (langBox) {
                                        const langImages = langBox.querySelectorAll('img[data-lang-key]');
                                        langImages.forEach(img => {
                                            const langKey = img.getAttribute('data-lang-key');
                                            const title = img.getAttribute('title') || '';
                                            const alt = img.getAttribute('alt') || '';
                                            const isSelected = img.classList.contains('selectedLanguage');
                                            const src = img.getAttribute('src') || '';
                                            const srcFilename = src.split('/').pop().replace('.svg', '').toLowerCase();
                                            const langName = title || (alt ? alt.split(',')[0].trim() : srcFilename);

                                            languages.push({
                                                key: langKey,
                                                name: langName,
                                                selected: isSelected,
                                                icon: src,
                                                srcFile: srcFilename
                                            });
                                        });
                                    }
                                }

                                return {
                                    episodes: Array.from(episodes).sort((a, b) => a - b),
                                    details: details,
                                    languages: languages
                                };
                            }
                        ''')

                        season_data['episodes'] = episode_data['episodes']
                        season_data['episode_details'] = episode_data['details']
                        season_data['languages'] = episode_data.get('languages', [])

                        # Success - break retry loop
                        if season_data['episodes']:
                            break
                        elif attempt < max_retries:
                            logger.info(f"Retry {attempt + 1}/{max_retries} for Season {season_num}")
                            await asyncio.sleep(1)

                except Exception as e:
                    logger.error(f"Attempt {attempt + 1} failed for Season {season_num}: {str(e)[:100]}")

                    if attempt < max_retries:
                        logger.info(f"Retrying Season {season_num} in 1 second...")
                        await asyncio.sleep(1)
                    else:
                        # Final fallback: return default episodes
                        logger.warning(f"Using fallback episodes (1-12) for Season {season_num}")
                        season_data['episodes'] = list(range(1, 13))

            return season_data

        # Cache miss or forced refresh - scrape fresh data
        logger.info(f"Scraping fresh data for {series_slug} (force_refresh={force_refresh})")

        # Run async scraping in new event loop
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(scrape_all_seasons())
        finally:
            loop.close()

        # Save scraped data to cache for future use
        cache_data = {
            'url_type': result.get('url_type'),
            'series_slug': result.get('series_slug'),
            'season': result.get('season'),
            'start_episode': result.get('start_episode'),
            'total_seasons': result.get('total_seasons'),
            'season_tabs': result.get('season_tabs', []),
            'extra_tabs': result.get('extra_tabs', []),
            'seasons_data': result.get('seasons_data', {}),
            'extras_data': result.get('extras_data', {}),
            'series_cover_url': result.get('series_cover_url'),
            'series_description': result.get('series_description'),
            'series_name': series_slug.replace('-', ' ').title(),
            'languages': result.get('languages', [])
        }
        save_to_cache(series_slug, cache_data)
        extra_info = f" + {len(result.get('extra_tabs', []))} extras" if result.get('extra_tabs') else ""
        logger.info(f"Saved {series_slug} to cache ({result.get('total_seasons')} seasons{extra_info})")

        # Add cache indicator to response
        result['from_cache'] = False

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/queue', methods=['GET'])
def get_queue():
    """Get current queue status"""
    return jsonify(queue_manager.get_queue_status())


@app.route('/api/queue/debug', methods=['GET'])
def debug_queue():
    """Debug endpoint to check queue processor state with detailed statistics"""
    queue_status = queue_manager.get_queue_status()
    next_item = queue_manager.get_next_queued()

    # Get processor stats from robust queue processor
    processor_stats = queue_processor.stats

    debug_info = {
        'queue_processor': {
            'running': processor_stats['is_running'],
            'active_downloads': processor_stats['active_downloads'],
            'max_parallel': processor_stats['max_parallel'],
            'total_processed': processor_stats['total_processed'],
            'total_completed': processor_stats['total_completed'],
            'total_failed': processor_stats['total_failed'],
            'restart_count': processor_stats['restart_count'],
            'started_at': processor_stats['started_at']
        },
        'queue_status': queue_status,
        'next_queued_item': next_item.to_dict() if next_item else None,
        'active_downloads_count': len(active_downloads),
        'active_downloads_keys': list(active_downloads.keys())[:10],  # Limit to 10
        'timestamp': datetime.now().isoformat()
    }

    logger.debug("=" * 60)
    logger.debug("QUEUE DEBUG INFO (Robust Processor)")
    logger.debug("=" * 60)
    logger.debug(f"Queue Processor Running: {processor_stats['is_running']}")
    logger.debug(f"Active Downloads: {processor_stats['active_downloads']}/{processor_stats['max_parallel']}")
    logger.debug(f"Total Processed: {processor_stats['total_processed']}")
    logger.debug(f"Completed: {processor_stats['total_completed']} | Failed: {processor_stats['total_failed']}")
    logger.debug(f"Queue - Total: {queue_status['total']} | Queued: {queue_status['queued']} | Processing: {queue_status['processing']}")
    logger.debug(f"Next Item: {next_item.series_name if next_item else 'None'}")
    logger.debug("=" * 60)

    return jsonify(debug_info)


@app.route('/api/queue/max-concurrent', methods=['GET', 'POST'])
def manage_max_concurrent():
    """Get or set max concurrent downloads"""
    # Get the configurable limit
    max_limit = queue_processor.get_max_limit()

    if request.method == 'GET':
        # Use global episode semaphore for accurate info
        return jsonify({
            'max_concurrent': global_episode_semaphore.max_concurrent,
            'currently_active': global_episode_semaphore.active_count,
            'available_slots': global_episode_semaphore.available_slots,
            'max_limit': max_limit,
            'active_series': queue_processor.active_count  # Number of series being processed
        })

    # POST - Update max concurrent downloads
    data = request.json
    new_max = data.get('max_concurrent')

    if not new_max or not isinstance(new_max, int) or new_max < 1 or new_max > max_limit:
        return jsonify({'error': f'max_concurrent must be an integer between 1 and {max_limit}'}), 400

    old_max = queue_processor.max_parallel
    # Update the robust processor
    queue_processor.set_max_parallel(new_max)

    logger.info(f"Max concurrent downloads changed: {old_max} -> {new_max} (limit: {max_limit})")

    return jsonify({
        'message': f'Max concurrent downloads updated to {new_max}',
        'old_value': old_max,
        'new_value': new_max,
        'max_limit': max_limit
    })


@app.route('/api/queue/processor', methods=['GET', 'POST'])
def manage_processor():
    """
    Manage the queue processor.

    GET: Get processor status and statistics
    POST: Control processor (start/stop/restart)
          Body: {"action": "start" | "stop" | "restart"}
    """
    if request.method == 'GET':
        stats = queue_processor.stats
        return jsonify({
            'status': 'running' if stats['is_running'] else 'stopped',
            'active_series': stats['active_downloads'],  # Number of series being processed
            'active_episodes': global_episode_semaphore.active_count,  # Actual concurrent episode downloads
            'max_parallel_episodes': global_episode_semaphore.max_concurrent,
            'available_episode_slots': global_episode_semaphore.available_slots,
            'statistics': {
                'total_processed': stats['total_processed'],
                'total_completed': stats['total_completed'],
                'total_failed': stats['total_failed'],
                'restart_count': stats['restart_count']
            },
            'started_at': stats['started_at']
        })

    # POST - Control processor
    data = request.json or {}
    action = data.get('action', '').lower()

    if action == 'start':
        started = start_queue_processor()
        return jsonify({
            'action': 'start',
            'success': started,
            'message': 'Processor started' if started else 'Processor already running'
        })

    elif action == 'stop':
        stopped = stop_queue_processor()
        return jsonify({
            'action': 'stop',
            'success': stopped,
            'message': 'Processor stopped' if stopped else 'Failed to stop processor'
        })

    elif action == 'restart':
        stop_queue_processor()
        time.sleep(1)  # Brief pause
        started = start_queue_processor()
        return jsonify({
            'action': 'restart',
            'success': started,
            'message': 'Processor restarted' if started else 'Failed to restart processor'
        })

    else:
        return jsonify({'error': 'Invalid action. Use: start, stop, restart'}), 400


@app.route('/api/auto-scraper', methods=['GET', 'POST'])
def manage_auto_scraper():
    """
    Manage the background auto-scraper.

    GET: Get auto-scraper status and statistics
    POST: Control auto-scraper (start/stop)
          Body: {"action": "start" | "stop"}
    """
    if request.method == 'GET':
        stats = auto_scraper.stats
        cache_stats = get_series_cache_stats()
        series_needing_update = get_series_needing_update(limit=5)

        return jsonify({
            'status': stats['current_status'],
            'is_running': stats['is_running'],
            'currently_scraping': stats['currently_scraping'],
            'statistics': {
                'total_scraped': stats['total_scraped'],
                'total_updated': stats['total_updated'],
                'total_errors': stats['total_errors'],
                'last_scrape': stats['last_scrape']
            },
            'started_at': stats['started_at'],
            'cache_stats': cache_stats,
            'series_needing_update': series_needing_update
        })

    # POST - Control auto-scraper
    data = request.json or {}
    action = data.get('action', '').lower()

    if action == 'start':
        started = auto_scraper.start()
        return jsonify({
            'action': 'start',
            'success': started,
            'message': 'Auto-scraper started' if started else 'Auto-scraper already running'
        })

    elif action == 'stop':
        stopped = auto_scraper.stop()
        return jsonify({
            'action': 'stop',
            'success': stopped,
            'message': 'Auto-scraper stopped' if stopped else 'Auto-scraper was not running'
        })

    else:
        return jsonify({'error': 'Invalid action. Use: start, stop'}), 400


@app.route('/api/auto-scraper/trigger', methods=['POST'])
def trigger_manual_scrape():
    """
    Manually trigger a scrape for specific series.

    Body: {"series_slug": "series-name"} or {"series_slugs": ["series1", "series2"]}
    """
    data = request.json or {}

    # Single series
    if 'series_slug' in data:
        slug = data['series_slug']
        if auto_scraper._currently_scraping:
            return jsonify({
                'error': 'Auto-scraper is currently busy',
                'currently_scraping': auto_scraper._currently_scraping
            }), 409

        # Trigger scrape in background thread
        def do_scrape():
            auto_scraper._scrape_series(slug, reason="manual_trigger")

        threading.Thread(target=do_scrape, daemon=True).start()

        return jsonify({
            'success': True,
            'message': f'Scrape triggered for {slug}',
            'series_slug': slug
        })

    # Multiple series
    elif 'series_slugs' in data:
        slugs = data['series_slugs']
        if not isinstance(slugs, list) or len(slugs) == 0:
            return jsonify({'error': 'series_slugs must be a non-empty list'}), 400

        if len(slugs) > 10:
            return jsonify({'error': 'Maximum 10 series per request'}), 400

        def do_batch_scrape():
            for slug in slugs:
                if auto_scraper._shutdown_event.is_set():
                    break
                auto_scraper._scrape_series(slug, reason="manual_batch")
                time.sleep(5)  # Rate limiting

        threading.Thread(target=do_batch_scrape, daemon=True).start()

        return jsonify({
            'success': True,
            'message': f'Batch scrape triggered for {len(slugs)} series',
            'series_slugs': slugs
        })

    else:
        return jsonify({'error': 'Provide series_slug or series_slugs'}), 400


@app.route('/api/cache/stats', methods=['GET'])
def get_cache_statistics():
    """Get detailed cache statistics."""
    cache_stats = get_series_cache_stats()
    series_needing_update = get_series_needing_update(limit=20)
    uncached_count = len(get_uncached_series_from_catalog())

    return jsonify({
        'cache': cache_stats,
        'series_needing_update': series_needing_update,
        'uncached_series_count': uncached_count
    })


@app.route('/api/retry/<session_id>', methods=['POST'])
def retry_failed_episodes(session_id):
    """Retry failed episodes from a completed download"""
    retry_item = queue_manager.retry_failed(session_id)

    if not retry_item:
        return jsonify({'error': 'No failed episodes to retry'}), 404

    # Store in active_downloads
    active_downloads[retry_item.session_id] = {
        'url': retry_item.url,
        'status': 'queued',
        'current': 0,
        'total': 0,
        'options': retry_item.options
    }

    # Ensure queue processor is running
    start_queue_processor()

    return jsonify({
        'session_id': retry_item.session_id,
        'status': 'queued',
        'retrying_episodes': len(retry_item.episodes_per_season)
    })


@app.route('/api/queue/<session_id>', methods=['DELETE'])
def cancel_queued_download(session_id):
    """Cancel a queued download"""
    # Update status to cancelled
    success = queue_manager.update_status(session_id, DownloadStatus.CANCELLED)

    if success:
        # Also update active_downloads
        if session_id in active_downloads:
            active_downloads[session_id]['status'] = 'cancelled'
        return jsonify({'status': 'cancelled'})

    return jsonify({'error': 'Session not found'}), 404


@app.route('/api/queue/<session_id>/remove', methods=['DELETE'])
def remove_queue_item(session_id):
    """Completely remove a queue item (for completed/failed/cancelled items)"""
    # First cancel if still active
    if session_id in active_downloads:
        active_downloads[session_id]['status'] = 'cancelled'
        del active_downloads[session_id]

    # Remove from queue
    success = queue_manager.remove_item(session_id)

    if success:
        logger.info(f"Removed queue item: {session_id[:8]}...")
        return jsonify({'status': 'removed', 'session_id': session_id})

    return jsonify({'error': 'Session not found'}), 404


@app.route('/api/queue/clear', methods=['POST'])
def clear_queue():
    """Clear completed downloads from queue"""
    keep_recent = request.json.get('keep_recent', 10) if request.json else 10
    removed_count = queue_manager.clear_completed(keep_recent)
    return jsonify({
        'removed': removed_count,
        'message': f'Cleared {removed_count} old downloads'
    })


@app.route('/api/queue/add-series', methods=['POST'])
def add_series_to_queue():
    """
    Add a series to download queue from catalog

    Request body:
    {
        "url": "http://186.2.175.5/serie/stream/series-slug",
        "series_name": "Series Name",
        "slug": "series-slug"
    }

    Response:
    {
        "session_id": "abc123",
        "status": "queued",
        "series_name": "Series Name"
    }
    """
    data = request.json

    if not data or 'url' not in data:
        return jsonify({'error': 'URL is required'}), 400

    url = data['url']
    series_name = data.get('series_name', 'Unknown Series')
    slug = data.get('slug', '')

    # Generate session ID
    session_id = generate_session_id()

    # Default options - use config settings
    options = {
        'quality': 'best',
        'format': app_config.DEFAULT_FORMAT,
        'wait': app_config.DEFAULT_WAIT_TIME,
        'parallel': queue_processor.max_parallel,  # Use queue processor setting
        'series_display': series_name,
        'english_title': False,
        'adblock': True,
        'force': False,
        'seasons': 'all',  # Auto-download ALL seasons from batch mode
        'episodes': 'all'  # Auto-download ALL episodes from batch mode
    }

    # NOTE: episodes_per_season will be determined by process_download_from_queue()
    # when the download is actually processed. The queue processor will call
    # run_download_thread() which handles URL parsing and season/episode detection.

    # For batch-added series, we add them with episodes_per_season=None
    # The download thread will automatically scrape and detect all seasons/episodes

    # Add to queue
    queue_item = queue_manager.add_to_queue(
        session_id=session_id,
        url=url,
        options=options,
        episodes_per_season=None  # Auto-detect all episodes
    )

    # Store in active_downloads
    active_downloads[session_id] = {
        'url': url,
        'status': 'queued',
        'current': 0,
        'total': 0,
        'options': options,
        'series_name': series_name,
        'slug': slug
    }

    # Ensure queue processor is running
    start_queue_processor()

    logger.info(f"Added to queue: {series_name} (will auto-download ALL seasons and episodes)")
    logger.debug(f"Session ID: {session_id}")
    logger.debug(f"URL: {url}")
    logger.debug(f"Options:")
    logger.debug(f"  - seasons: {options['seasons']}")
    logger.debug(f"  - episodes: {options['episodes']}")
    logger.debug(f"  - parallel: {options['parallel']}")
    logger.debug(f"  - download_path: {Config.get_download_path()}")
    logger.debug(f"  - quality: {options['quality']}")
    logger.debug(f"  - format: {options['format']}")

    return jsonify({
        'session_id': session_id,
        'status': 'queued',
        'series_name': series_name
    })


@app.route('/api/queue/<session_id>/priority', methods=['POST'])
def set_priority_endpoint(session_id):
    """
    Set download priority

    Request body:
    {
        "priority": 0-3  (0=LOW, 1=NORMAL, 2=HIGH, 3=URGENT)
    }
    """
    from app.download_queue import DownloadPriority

    data = request.json
    if not data or 'priority' not in data:
        return jsonify({'error': 'Priority level required'}), 400

    try:
        priority_value = int(data['priority'])
        priority = DownloadPriority(priority_value)
    except (ValueError, KeyError):
        return jsonify({'error': 'Invalid priority level. Must be 0-3'}), 400

    success = queue_manager.set_priority(session_id, priority)
    if success:
        return jsonify({
            'status': 'priority_updated',
            'session_id': session_id,
            'priority': priority.value,
            'priority_name': priority.name
        })
    return jsonify({'error': 'Failed to set priority'}), 400


@app.route('/api/queue/reorder', methods=['POST'])
def reorder_queue():
    """
    Reorder queue items via drag & drop

    Request body:
    {
        "order": ["session_id_1", "session_id_2", ...]  (list of session IDs in new order)
    }
    """
    data = request.json
    if not data or 'order' not in data:
        return jsonify({'error': 'order array required'}), 400

    order = data['order']
    if not isinstance(order, list):
        return jsonify({'error': 'order must be an array'}), 400

    success = queue_manager.reorder(order)

    if success:
        return jsonify({'status': 'reordered', 'count': len(order)})
    return jsonify({'error': 'Failed to reorder queue'}), 400


@app.route('/api/queue/duplicates', methods=['GET'])
def get_duplicate_series():
    """
    Find all series that have duplicate queue entries.
    Returns a list of URLs that have multiple entries.
    """
    duplicates = queue_manager.find_duplicate_series()
    return jsonify({
        'duplicates': duplicates,
        'count': len(duplicates)
    })


@app.route('/api/queue/consolidate', methods=['POST'])
def consolidate_series_endpoint():
    """
    Consolidate all queue entries for a specific series into one.

    Request body:
    {
        "url": "http://example.com/serie/stream/series-name"
    }

    Or consolidate all duplicates:
    {
        "consolidate_all": true
    }
    """
    data = request.json
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    # Consolidate all duplicates at once
    if data.get('consolidate_all'):
        duplicates = queue_manager.find_duplicate_series()
        results = []
        total_merged = 0

        for dup in duplicates:
            result = queue_manager.consolidate_series(dup['url'])
            if result['success']:
                results.append(result)
                total_merged += result.get('entries_merged', 0)

        return jsonify({
            'status': 'consolidated_all',
            'series_consolidated': len(results),
            'total_entries_merged': total_merged,
            'results': results
        })

    # Consolidate single series
    url = data.get('url')
    if not url:
        return jsonify({'error': 'url required'}), 400

    result = queue_manager.consolidate_series(url)

    if result['success']:
        return jsonify({
            'status': 'consolidated',
            **result
        })
    return jsonify({'error': result.get('message', 'Failed to consolidate')}), 400


@app.route('/api/queue/<session_id>/episode/<episode_key>/stop', methods=['POST'])
def stop_episode_endpoint(session_id, episode_key):
    """
    Stop a specific episode download

    This marks the episode as 'stopped' and signals the download process to skip it.
    The episode can be restarted later.
    """
    # Update episode status to stopped
    success = queue_manager.update_episode_status(session_id, episode_key, status='stopped')

    if success:
        # Emit status update to frontend
        socketio.emit('episode_status_update', {
            'session_id': session_id,
            'episode_key': episode_key,
            'status': 'stopped',
            'progress': 0
        })
        return jsonify({
            'status': 'stopped',
            'session_id': session_id,
            'episode_key': episode_key
        })
    return jsonify({'error': 'Failed to stop episode'}), 400


@app.route('/api/queue/<session_id>/episode/<episode_key>/cancel', methods=['POST'])
def cancel_episode_endpoint(session_id, episode_key):
    """
    Cancel a queued episode (remove from download queue)

    This removes the episode from the queue entirely.
    Only works for episodes with status 'queued'.
    """
    # Get current episode status
    item = queue_manager.get_item(session_id)
    if not item:
        return jsonify({'error': 'Session not found'}), 404

    episode_status = item.episode_status.get(episode_key)
    if not episode_status:
        return jsonify({'error': 'Episode not found'}), 404

    if episode_status.get('status') != 'queued':
        return jsonify({'error': 'Can only cancel queued episodes'}), 400

    # Update episode status to cancelled
    success = queue_manager.update_episode_status(session_id, episode_key, status='cancelled')

    if success:
        # Emit status update to frontend
        socketio.emit('episode_status_update', {
            'session_id': session_id,
            'episode_key': episode_key,
            'status': 'cancelled',
            'progress': 0
        })

        # Update total episode count
        item = queue_manager.get_item(session_id)
        if item:
            # Count non-cancelled episodes
            active_count = sum(1 for ep in item.episode_status.values()
                              if ep.get('status') not in ['cancelled', 'stopped'])
            item.total_episodes = active_count
            queue_manager.save_queue()

        return jsonify({
            'status': 'cancelled',
            'session_id': session_id,
            'episode_key': episode_key
        })
    return jsonify({'error': 'Failed to cancel episode'}), 400


@app.route('/api/queue/<session_id>/episode/<episode_key>/restart', methods=['POST'])
def restart_episode_endpoint(session_id, episode_key):
    """
    Restart a stopped/failed/cancelled episode

    This re-queues the episode for download.
    """
    # Update episode status back to queued
    success = queue_manager.update_episode_status(session_id, episode_key, status='queued', progress=0)

    if success:
        # Emit status update to frontend
        socketio.emit('episode_status_update', {
            'session_id': session_id,
            'episode_key': episode_key,
            'status': 'queued',
            'progress': 0
        })
        return jsonify({
            'status': 'restarted',
            'session_id': session_id,
            'episode_key': episode_key
        })
    return jsonify({'error': 'Failed to restart episode'}), 400


@app.route('/api/queue/<session_id>/episodes/reorder', methods=['POST'])
def reorder_episodes_endpoint(session_id):
    """
    Reorder episodes within a download session

    Request body:
    {
        "order": ["S01E03", "S01E01", "S01E02", ...]  (list of episode keys in new order)
    }

    Only queued episodes can be reordered. Episodes that are downloading, completed,
    failed, stopped, or cancelled keep their current position.
    """
    data = request.json
    if not data or 'order' not in data:
        return jsonify({'error': 'order array required'}), 400

    order = data['order']
    if not isinstance(order, list):
        return jsonify({'error': 'order must be an array'}), 400

    item = queue_manager.get_item(session_id)
    if not item:
        return jsonify({'error': 'Session not found'}), 404

    # Reorder episodes - only affects queued episodes
    # Create new episode_status dict with reordered keys
    old_status = item.episode_status
    new_status = {}

    # First, add episodes in the new order (only if they exist and are queued)
    for ep_key in order:
        if ep_key in old_status:
            new_status[ep_key] = old_status[ep_key]

    # Then add any remaining episodes that weren't in the order list
    for ep_key, status in old_status.items():
        if ep_key not in new_status:
            new_status[ep_key] = status

    item.episode_status = new_status
    queue_manager.save_queue()

    # Emit update to frontend
    socketio.emit('episode_order_update', {
        'session_id': session_id,
        'order': list(new_status.keys())
    })

    return jsonify({
        'status': 'reordered',
        'session_id': session_id,
        'episode_count': len(new_status)
    })


@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info('Client connected')
    emit('connected', {'status': 'Connected to HLS Downloader'})


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    logger.info('Client disconnected')
    pass


# ==========================================
# Catalog API Endpoints
# ==========================================

if __name__ == '__main__':
    logger.info("=" * 70)
    logger.info("HLS Video Downloader - Web GUI")
    logger.info("=" * 70)
    logger.info("Starting web server...")
    logger.info("Open your browser and go to: http://localhost:5000")
    logger.info("Press Ctrl+C to stop")

    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
