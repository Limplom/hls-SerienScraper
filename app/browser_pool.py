"""
Browser Pool Manager
Efficient browser context pooling for parallel scraping operations

Optimized for:
- Fast startup with minimal browser args
- Robust error recovery
- Memory-efficient context reuse with GC tracking
- Automatic pool reset under memory pressure
- Configurable timeouts
"""

import asyncio
import gc
import logging
import os
import time
from typing import List, Optional, Dict

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from app.config import Config

logger = logging.getLogger(__name__)


# Optimized browser launch arguments for speed and stability
BROWSER_ARGS = [
    '--no-sandbox',
    '--disable-blink-features=AutomationControlled',
    '--mute-audio',
    '--disable-gpu',
    '--disable-dev-shm-usage',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-breakpad',
    '--disable-component-update',
    '--disable-default-apps',
    '--disable-hang-monitor',
    '--disable-popup-blocking',
    '--disable-prompt-on-repost',
    '--disable-sync',
    '--disable-translate',
    '--metrics-recording-only',
    '--no-first-run',
    '--safebrowsing-disable-auto-update',
    '--enable-features=NetworkService,NetworkServiceInProcess',
    '--force-color-profile=srgb',
]

# User agents rotation for anti-detection
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]


def _get_process_memory_mb() -> float:
    """Get current process RSS memory in MB."""
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except ImportError:
        try:
            with open(f'/proc/{os.getpid()}/statm', 'r') as f:
                pages = int(f.read().split()[1])
                return pages * os.sysconf('SC_PAGE_SIZE') / (1024 * 1024)
        except Exception:
            return 0.0


class BrowserPool:
    """
    Manages a pool of browser contexts for parallel scraping.

    Features:
    - Lazy initialization
    - Automatic context recovery on errors
    - Memory monitoring with automatic pool reset
    - Context leak detection via GC tracing
    - Configurable pool size and timeouts
    """

    def __init__(self, pool_size: int = 3, headless: bool = True, default_timeout: int = 15000):
        self.pool_size = pool_size
        self.headless = headless
        self.default_timeout = default_timeout
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.contexts: List[BrowserContext] = []
        self.available_contexts: asyncio.Queue = asyncio.Queue()
        self._initialized = False
        self._context_use_count: Dict[int, int] = {}
        self._max_context_uses = min(Config.BROWSER_MAX_CONTEXT_USES, 50)  # Hard cap at 50
        self._context_created_at: Dict[int, float] = {}  # Track creation time for leak detection
        self._total_recycled = 0
        self._memory_at_init: float = 0.0
        self._memory_reset_threshold_mb = 500  # Reset pool if process uses >500MB above baseline
        self._last_memory_check: float = 0.0
        self._memory_check_interval = 60.0  # Check memory every 60s

    async def initialize(self):
        """Initialize the browser pool with optimized settings"""
        if self._initialized:
            return

        logger.info(f"Initializing browser pool ({self.pool_size} contexts, max uses: {self._max_context_uses})...")
        self._memory_at_init = _get_process_memory_mb()

        try:
            self.playwright = await async_playwright().start()

            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=BROWSER_ARGS
            )

            for i in range(self.pool_size):
                context = await self._create_context(i)
                self.contexts.append(context)
                await self.available_contexts.put(context)
                self._context_use_count[id(context)] = 0
                self._context_created_at[id(context)] = time.monotonic()

            self._initialized = True
            mem = _get_process_memory_mb()
            logger.info(f"Browser pool ready ({self.pool_size} contexts, {mem:.0f}MB RSS)")

        except Exception as e:
            logger.error(f"Failed to initialize browser pool: {e}")
            await self.close()
            raise

    async def _create_context(self, index: int = 0) -> BrowserContext:
        """Create a new browser context with optimized settings"""
        ua = USER_AGENTS[index % len(USER_AGENTS)]

        context = await self.browser.new_context(
            viewport={
                'width': Config.BROWSER_VIEWPORT_WIDTH,
                'height': Config.BROWSER_VIEWPORT_HEIGHT
            },
            user_agent=ua,
            java_script_enabled=True,
            ignore_https_errors=True,
            bypass_csp=True
        )

        # Block unnecessary resources for faster loading
        await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                           lambda route: route.abort())

        return context

    async def _recycle_context(self, context: BrowserContext, idx: int = 0) -> BrowserContext:
        """Recycle a context: close old, create new, update tracking."""
        ctx_id = id(context)
        uses = self._context_use_count.get(ctx_id, 0)
        age = time.monotonic() - self._context_created_at.get(ctx_id, time.monotonic())

        logger.debug(f"Recycling context (uses={uses}, age={age:.0f}s)")

        try:
            await context.close()
        except Exception as e:
            logger.warning(f"Error closing recycled context: {e}")

        new_context = await self._create_context(idx)

        # Update tracking
        if context in self.contexts:
            self.contexts[self.contexts.index(context)] = new_context
        self._context_use_count.pop(ctx_id, None)
        self._context_created_at.pop(ctx_id, None)
        self._context_use_count[id(new_context)] = 0
        self._context_created_at[id(new_context)] = time.monotonic()
        self._total_recycled += 1

        return new_context

    async def _check_memory_pressure(self) -> bool:
        """Check if memory usage is too high and pool should be reset.
        Returns True if pool was reset."""
        now = time.monotonic()
        if now - self._last_memory_check < self._memory_check_interval:
            return False
        self._last_memory_check = now

        current_mem = _get_process_memory_mb()
        mem_growth = current_mem - self._memory_at_init

        if mem_growth > self._memory_reset_threshold_mb:
            logger.warning(
                f"Memory pressure detected: {current_mem:.0f}MB RSS "
                f"(+{mem_growth:.0f}MB since init), forcing pool reset"
            )
            await self._reset_pool()
            gc.collect()
            after_mem = _get_process_memory_mb()
            logger.info(f"Pool reset complete. Memory: {after_mem:.0f}MB RSS (freed {current_mem - after_mem:.0f}MB)")
            return True

        return False

    async def _reset_pool(self):
        """Reset all contexts in the pool (emergency cleanup)."""
        logger.info("Resetting all browser pool contexts...")

        # Drain the available queue
        drained = []
        while not self.available_contexts.empty():
            try:
                ctx = self.available_contexts.get_nowait()
                drained.append(ctx)
            except asyncio.QueueEmpty:
                break

        # Close and recreate all drained contexts
        for i, context in enumerate(drained):
            try:
                new_ctx = await self._recycle_context(context, i)
                await self.available_contexts.put(new_ctx)
            except Exception as e:
                logger.error(f"Failed to recycle context {i} during reset: {e}")
                try:
                    new_ctx = await self._create_context(i)
                    self.contexts.append(new_ctx)
                    self._context_use_count[id(new_ctx)] = 0
                    self._context_created_at[id(new_ctx)] = time.monotonic()
                    await self.available_contexts.put(new_ctx)
                except Exception as e2:
                    logger.error(f"Failed to create replacement context: {e2}")

    def _detect_context_leaks(self):
        """Log warnings for contexts that have been checked out too long."""
        now = time.monotonic()
        # Contexts in self.contexts but NOT in available_contexts are checked out
        available_ids = set()
        # Peek into the queue (approximation)
        checked_out = len(self.contexts) - self.available_contexts.qsize()
        if checked_out > 0:
            for ctx_id, created_at in self._context_created_at.items():
                age = now - created_at
                uses = self._context_use_count.get(ctx_id, 0)
                if age > 300 and uses > 0:  # >5 min with uses
                    logger.warning(
                        f"Potential context leak: ctx {ctx_id} alive for {age:.0f}s with {uses} uses"
                    )

    async def acquire(self, timeout: float = 30.0) -> BrowserContext:
        """Acquire a browser context from the pool."""
        if not self._initialized:
            await self.initialize()

        # Periodic memory check
        await self._check_memory_pressure()

        try:
            context = await asyncio.wait_for(
                self.available_contexts.get(),
                timeout=timeout
            )

            ctx_id = id(context)
            if ctx_id in self._context_use_count:
                self._context_use_count[ctx_id] += 1

                # Recycle if over use limit OR context is very old (>30 min)
                needs_recycle = self._context_use_count[ctx_id] >= self._max_context_uses
                age = time.monotonic() - self._context_created_at.get(ctx_id, time.monotonic())
                if not needs_recycle and age > 1800:
                    needs_recycle = True
                    logger.debug(f"Context age-based recycle ({age:.0f}s old)")

                if needs_recycle:
                    idx = self.contexts.index(context) if context in self.contexts else 0
                    context = await self._recycle_context(context, idx)

            return context

        except asyncio.TimeoutError:
            self._detect_context_leaks()
            logger.warning("Timeout waiting for browser context")
            if self.browser and not self.browser.is_connected():
                await self.initialize()
            raise

    async def release(self, context: BrowserContext):
        """Release a browser context back to the pool."""
        try:
            await context.clear_cookies()

            for page in context.pages:
                try:
                    await page.close()
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"Error clearing context, attempting recovery: {e}")
            try:
                idx = self.contexts.index(context) if context in self.contexts else 0
                context = await self._recycle_context(context, idx)
            except Exception as e2:
                logger.error(f"Context recovery failed: {e2}")

        await self.available_contexts.put(context)

    @property
    def stats(self) -> dict:
        """Get pool statistics for monitoring."""
        return {
            'pool_size': self.pool_size,
            'available': self.available_contexts.qsize(),
            'checked_out': len(self.contexts) - self.available_contexts.qsize(),
            'total_recycled': self._total_recycled,
            'max_context_uses': self._max_context_uses,
            'memory_mb': _get_process_memory_mb(),
            'memory_at_init_mb': self._memory_at_init,
        }

    async def close(self):
        """Close all browser contexts and the browser"""
        if not self._initialized:
            return

        logger.info("Closing browser pool...")

        for context in self.contexts:
            try:
                await context.close()
            except Exception:
                pass

        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass

        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass

        self.contexts.clear()
        self._context_use_count.clear()
        self._context_created_at.clear()
        self._initialized = False

        gc.collect()
        logger.info("Browser pool closed")

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


class PooledPageContext:
    """
    Context manager for acquiring and releasing pages from pool.

    Features:
    - Automatic context acquisition/release
    - Configurable timeout
    - Error recovery
    """

    def __init__(self, pool: BrowserPool, timeout: int = None):
        self.pool = pool
        self.timeout = timeout or pool.default_timeout
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def __aenter__(self) -> Page:
        self.context = await self.pool.acquire()

        try:
            self.page = await asyncio.wait_for(
                self.context.new_page(),
                timeout=30.0
            )
            self.page.set_default_timeout(self.timeout)
            self.page.set_default_navigation_timeout(self.timeout)
            return self.page

        except Exception:
            if self.context:
                await self.pool.release(self.context)
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass

        if self.context:
            await self.pool.release(self.context)


class SingleBrowserScraper:
    """
    Lightweight scraper for single-page operations.
    More efficient than pool for one-off scrapes.
    """

    def __init__(self, headless: bool = True, timeout: int = 15000):
        self.headless = headless
        self.timeout = timeout

    async def scrape_page(self, url: str, extract_fn: str) -> dict:
        """
        Scrape a single page with a JavaScript extraction function.

        Args:
            url: Page URL to scrape
            extract_fn: JavaScript function body to execute (must return data)

        Returns:
            Extracted data from the page
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=BROWSER_ARGS
            )

            try:
                context = await browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent=USER_AGENTS[0]
                )

                await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                                   lambda route: route.abort())

                page = await context.new_page()
                page.set_default_timeout(self.timeout)

                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)

                result = await page.evaluate(extract_fn)
                return result

            finally:
                await browser.close()
