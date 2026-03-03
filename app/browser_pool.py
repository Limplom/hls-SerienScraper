"""
Browser Pool Manager
Efficient browser context pooling for parallel scraping operations

Optimized for:
- Fast startup with minimal browser args
- Robust error recovery
- Memory-efficient context reuse
- Configurable timeouts
"""

import asyncio
import logging
from typing import List, Optional
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


class BrowserPool:
    """
    Manages a pool of browser contexts for parallel scraping.

    Features:
    - Lazy initialization
    - Automatic context recovery on errors
    - Configurable pool size and timeouts
    - User agent rotation
    """

    def __init__(self, pool_size: int = 3, headless: bool = True, default_timeout: int = 15000):
        """
        Initialize browser pool

        Args:
            pool_size: Number of browser contexts to maintain
            headless: Run browsers in headless mode
            default_timeout: Default page timeout in milliseconds
        """
        self.pool_size = pool_size
        self.headless = headless
        self.default_timeout = default_timeout
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.contexts: List[BrowserContext] = []
        self.available_contexts: asyncio.Queue = asyncio.Queue()
        self._initialized = False
        self._context_use_count: dict = {}  # Track usage for recycling
        self._max_context_uses = Config.BROWSER_MAX_CONTEXT_USES  # Recycle context after N uses

    async def initialize(self):
        """Initialize the browser pool with optimized settings"""
        if self._initialized:
            return

        logger.info(f"Initializing browser pool ({self.pool_size} contexts)...")

        try:
            self.playwright = await async_playwright().start()

            # Launch browser with optimized args
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=BROWSER_ARGS
            )

            # Create contexts with rotating user agents
            for i in range(self.pool_size):
                context = await self._create_context(i)
                self.contexts.append(context)
                await self.available_contexts.put(context)
                self._context_use_count[id(context)] = 0

            self._initialized = True
            logger.info(f"Browser pool ready ({self.pool_size} contexts)")

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
            # Block unnecessary resources for faster loading
            bypass_csp=True
        )

        # Set up resource blocking for faster page loads
        await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                           lambda route: route.abort())

        return context

    async def acquire(self, timeout: float = 30.0) -> BrowserContext:
        """
        Acquire a browser context from the pool

        Args:
            timeout: Max time to wait for available context

        Returns:
            Available browser context
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Wait for available context with timeout
            context = await asyncio.wait_for(
                self.available_contexts.get(),
                timeout=timeout
            )

            # Check if context needs recycling
            ctx_id = id(context)
            if ctx_id in self._context_use_count:
                self._context_use_count[ctx_id] += 1

                if self._context_use_count[ctx_id] >= self._max_context_uses:
                    # Recycle old context
                    logger.debug(f"Recycling browser context after {self._max_context_uses} uses")
                    try:
                        await context.close()
                    except:
                        pass

                    # Create fresh context
                    idx = self.contexts.index(context) if context in self.contexts else 0
                    new_context = await self._create_context(idx)

                    # Update tracking
                    if context in self.contexts:
                        self.contexts[self.contexts.index(context)] = new_context
                    del self._context_use_count[ctx_id]
                    self._context_use_count[id(new_context)] = 0

                    return new_context

            return context

        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for browser context")
            # Try to create emergency context
            if self.browser and not self.browser.is_connected():
                await self.initialize()
            raise

    async def release(self, context: BrowserContext):
        """
        Release a browser context back to the pool

        Args:
            context: Browser context to release
        """
        try:
            # Clear cookies and storage for clean state
            await context.clear_cookies()

            # Close all pages except keeping the context
            for page in context.pages:
                try:
                    await page.close()
                except:
                    pass

        except Exception as e:
            # Context might be corrupted, try to recover
            logger.warning(f"Error clearing context, attempting recovery: {e}")
            try:
                idx = self.contexts.index(context) if context in self.contexts else 0
                await context.close()
                new_context = await self._create_context(idx)

                if context in self.contexts:
                    self.contexts[self.contexts.index(context)] = new_context
                    del self._context_use_count[id(context)]
                    self._context_use_count[id(new_context)] = 0

                context = new_context
            except:
                pass

        # Put back in queue
        await self.available_contexts.put(context)

    async def close(self):
        """Close all browser contexts and the browser"""
        if not self._initialized:
            return

        logger.info("Closing browser pool...")

        # Close all contexts
        for context in self.contexts:
            try:
                await context.close()
            except:
                pass

        # Close browser
        if self.browser:
            try:
                await self.browser.close()
            except:
                pass

        # Stop playwright
        if self.playwright:
            try:
                await self.playwright.stop()
            except:
                pass

        self.contexts.clear()
        self._context_use_count.clear()
        self._initialized = False
        logger.info("Browser pool closed")

    async def __aenter__(self):
        """Context manager entry"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
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
        """
        Args:
            pool: BrowserPool instance
            timeout: Page timeout in ms (default: pool's default_timeout)
        """
        self.pool = pool
        self.timeout = timeout or pool.default_timeout
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def __aenter__(self) -> Page:
        """Acquire context and create page"""
        self.context = await self.pool.acquire()

        try:
            self.page = await self.context.new_page()
            self.page.set_default_timeout(self.timeout)
            self.page.set_default_navigation_timeout(self.timeout)

            return self.page

        except Exception as e:
            # Release context on page creation failure
            if self.context:
                await self.pool.release(self.context)
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close page and release context"""
        if self.page:
            try:
                await self.page.close()
            except:
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

                # Block images/fonts for speed
                await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                                   lambda route: route.abort())

                page = await context.new_page()
                page.set_default_timeout(self.timeout)

                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)

                # Execute extraction function
                result = await page.evaluate(extract_fn)

                return result

            finally:
                await browser.close()
