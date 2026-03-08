#!/usr/bin/env python3
"""
HLS Video Downloader - FINAL FIXED VERSION
- Handles 18+ popups
- Multiple play button click methods
- Longer default wait (60s)
"""

import asyncio
from playwright.async_api import async_playwright, Page, BrowserContext
import subprocess
import sys
import argparse
import re
from pathlib import Path
import urllib.request
from urllib.parse import urlparse
import time
import logging
from typing import Optional, List, Dict, Any, Tuple, Set

# Fix Windows console encoding for special characters
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Import config for browser settings
try:
    from app import config as app_config
except ImportError:
    app_config = None  # Fallback when running standalone

# Setup logging
logger = logging.getLogger(__name__)

class VideoMetadata:
    def __init__(self):
        self.series_name = None
        self.series_name_display = None
        self.season = None
        self.episode = None
        self.episode_title_german = None
        self.episode_title_english = None
    
    def get_filename(self, use_english_title=False, quality="1080p", format_ext="mkv"):
        season_ep = f"S{int(self.season):02d}E{int(self.episode):02d}"
        
        if use_english_title and self.episode_title_english:
            title = self.episode_title_english.upper()
        elif self.episode_title_german:
            title = self.episode_title_german.upper()
        else:
            title = self.episode_title_english.upper() if self.episode_title_english else "EPISODE"
        
        clean_title = title.replace(":", "").replace("?", "").replace("/", "-").replace("\\", "-")
        series = self.series_name_display or self.series_name
        
        return f"{series}- {season_ep}- {clean_title} WEBRip-{quality}.{format_ext}"
    
    def get_directory(self, base_path="."):
        series_dir = self.series_name_display or self.series_name
        season_dir = f"Season {int(self.season)}"
        return Path(base_path) / series_dir / season_dir
    
    def get_full_path(self, base_path=".", use_english_title=False, quality="1080p", format_ext="mkv"):
        directory = self.get_directory(base_path)
        filename = self.get_filename(use_english_title, quality, format_ext)
        return directory / filename

class HLSExtractor:
    def __init__(self):
        self.m3u8_urls = []
        self.master_playlist = None
        self.metadata = VideoMetadata()
        self.ad_filters = set()
        self._ad_filters_frozen = None  # frozenset for O(1) lookups
        self._blocked_patterns_tuple = None  # tuple for faster iteration
        self._m3u8_event = None  # Event for instant m3u8 detection (created per extraction)
    
    # Filter update interval: 24 hours
    FILTER_UPDATE_INTERVAL = 86400

    def load_brave_filters(self):
        """Lädt erweiterte Filterlisten mit automatischem 24h-Update"""
        filter_cache = app_config.PROJECT_ROOT / "filter_cache"
        filter_cache.mkdir(exist_ok=True)

        # Extended filter lists for better ad/tracker blocking
        filter_lists = {
            'easylist': [
                'https://easylist-downloads.adblockplus.org/easylist.txt',
                'https://easylist.to/easylist/easylist.txt'
            ],
            'easyprivacy': [
                'https://easylist-downloads.adblockplus.org/easyprivacy.txt',
                'https://easylist.to/easylist/easyprivacy.txt'
            ],
            'easylist_germany': [
                'https://easylist-downloads.adblockplus.org/easylistgermany.txt',
            ],
            'adguard_base': [
                'https://filters.adtidy.org/extension/chromium/filters/2.txt',
            ],
            'adguard_tracking': [
                'https://filters.adtidy.org/extension/chromium/filters/3.txt',
            ],
            'adguard_annoyances': [
                'https://filters.adtidy.org/extension/chromium/filters/14.txt',
            ],
        }

        updated_count = 0
        for name, urls in filter_lists.items():
            cache_file = filter_cache / f"{name}.txt"

            should_download = not cache_file.exists()
            if not should_download:
                file_age = time.time() - cache_file.stat().st_mtime
                should_download = file_age > self.FILTER_UPDATE_INTERVAL

            if should_download:
                downloaded = False
                for url in urls:
                    try:
                        urllib.request.urlretrieve(url, cache_file)
                        downloaded = True
                        updated_count += 1
                        break
                    except Exception:
                        continue

                if not downloaded and not cache_file.exists():
                    continue

            self._parse_filter_file(cache_file, name)

        if updated_count > 0:
            logger.info(f"Updated {updated_count} filter list(s)")
        logger.info(f"Loaded {len(self.ad_filters)} filter rules from {len(filter_lists)} lists")
        # Build frozenset for O(1) lookups during request interception
        self._ad_filters_frozen = frozenset(self.ad_filters)
        # Pre-build blocked patterns tuple for faster iteration
        self._blocked_patterns_tuple = (
            '/ads/', '/ad/', '/advert',
            'doubleclick', 'googlesyndication',
            'facebook.com/tr', 'facebook.net',
            '/analytics.', '/tracking.',
            '/banner', '/popup',
        )
        return len(self.ad_filters) > 0

    def _parse_filter_file(self, cache_file: Path, name: str):
        """Parse a single adblock filter file and extract domain rules."""
        try:
            filters = self.ad_filters
            with open(cache_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    # Quick prefix checks avoid strip() on most lines
                    if not line or line[0] in ('!', '[', '\n', '\r'):
                        continue

                    line = line.rstrip()
                    if not line:
                        continue

                    if '||' in line:
                        # Extract domain: ||domain.com^ or ||domain.com/path$options
                        start = line.index('||') + 2
                        end = len(line)
                        for ch_idx in range(start, end):
                            ch = line[ch_idx]
                            if ch in ('^', '/', '$'):
                                end = ch_idx
                                break
                        domain = line[start:end]
                        if '.' in domain:
                            filters.add(domain.lower())
                    elif line[:9] == '|https://' or line[:8] == '|http://':
                        try:
                            start = line.index('://') + 3
                            end = len(line)
                            for ch_idx in range(start, end):
                                ch = line[ch_idx]
                                if ch in ('/', '$'):
                                    end = ch_idx
                                    break
                            domain = line[start:end]
                            if '.' in domain:
                                filters.add(domain.lower())
                        except (IndexError, ValueError):
                            pass
        except Exception as e:
            logger.warning(f"Error parsing filter list {name}: {e}")
    
    async def close_new_tabs(self, context: BrowserContext, main_page: Page) -> int:
        """Schließt alle neuen Tabs (Werbung) außer der Hauptseite"""
        try:
            pages = context.pages
            closed = 0
            for page in pages:
                if page != main_page:
                    try:
                        await page.close()
                        closed += 1
                    except Exception as e:
                        logger.debug(f"Could not close ad tab: {e}")
            if closed > 0:
                logger.info(f"Closed {closed} ad tab(s)")
            return closed
        except Exception as e:
            logger.debug(f"Error in close_new_tabs: {e}")
            return 0
    
    async def _remove_overlays(self, page):
        """Entfernt Ad-Overlays und blockierende Elemente von der Seite"""
        try:
            await page.evaluate('''() => {
                // Remove known ad overlay elements
                const adSelectors = [
                    '[id*="ad-"]', '[class*="ad-overlay"]',
                    '[style*="z-index: 999"]', '[style*="z-index:999"]',
                    '[style*="z-index: 9999"]', '[style*="z-index:9999"]',
                ];
                adSelectors.forEach(sel => {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            if (el.tagName === 'A' || el.id.match(/^[a-z]{5,}$/i)) {
                                el.remove();
                            }
                        });
                    } catch(e) {}
                });
                // Remove full-screen fixed overlays (likely ads)
                document.querySelectorAll('div').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' &&
                        parseInt(style.zIndex) > 100 &&
                        el.offsetWidth > window.innerWidth * 0.8 &&
                        el.offsetHeight > window.innerHeight * 0.8 &&
                        !el.querySelector('video')) {
                        el.remove();
                    }
                });
            }''')
        except Exception as e:
            logger.debug(f"Overlay removal error: {e}")

    async def _detect_modal_popup(self, frame):
        """Erkennt modale Dialoge (SweetAlert, Bootstrap, custom modals)"""
        try:
            return await frame.evaluate('''() => {
                // SweetAlert2
                const swal = document.querySelector('.swal2-container, .swal2-popup');
                if (swal && swal.offsetParent !== null) return 'swal2';
                // Bootstrap modal
                const bsModal = document.querySelector('.modal.show, .modal[style*="display: block"]');
                if (bsModal) return 'bootstrap';
                // Generic modal/dialog overlays
                const genericModal = document.querySelector(
                    '[role="dialog"]:not([aria-hidden="true"]), ' +
                    '[class*="modal"]:not([style*="display: none"]):not([aria-hidden="true"]), ' +
                    '[class*="popup"]:not([style*="display: none"]):not([aria-hidden="true"]), ' +
                    '[class*="age-verify"], [class*="age_verify"], [id*="age-verify"], [id*="age_verify"]'
                );
                if (genericModal && genericModal.offsetParent !== null) return 'generic';
                return null;
            }''')
        except Exception:
            return None

    async def click_18_plus_popups(self, page, context, phase="initial"):
        """Klickt alle 18+ OK Buttons weg - erkennt iframes, modals und overlays"""
        logger.info(f"Checking for 18+ popups ({phase})...")

        clicked_count = 0
        max_attempts = 8

        # Erweiterte Selektoren für verschiedene Popup-Typen
        button_selectors = [
            # SweetAlert
            '.swal2-confirm',
            '.swal2-styled',
            # Standard buttons
            'button:has-text("OK")',
            'button:has-text("Ok")',
            'button:has-text("Confirm")',
            'button:has-text("Bestätigen")',
            'button:has-text("Ja")',
            'button:has-text("Yes")',
            'button:has-text("Ich bin 18")',
            'button:has-text("I am 18")',
            'button:has-text("Enter")',
            'button:has-text("Eintreten")',
            # Generic confirm patterns
            'button[class*="confirm"]',
            'button[class*="accept"]',
            'button[class*="agree"]',
            'a[class*="confirm"]',
            'a[class*="accept"]',
            # Age-verification specific
            '[class*="age"] button',
            '[id*="age"] button',
            '[class*="age-gate"] button',
            '[class*="verify"] button',
        ]

        # Confirm-Texte die auf echte Popups hindeuten (nicht Werbung)
        confirm_texts = {'ok', 'confirm', 'bestätigen', 'ja', 'yes', 'enter',
                         'eintreten', 'ich bin 18', 'i am 18', 'accept', 'akzeptieren',
                         'agree', 'zustimmen', 'weiter', 'continue'}

        for attempt in range(max_attempts):
            try:
                await asyncio.sleep(1)

                # Schließe Werbe-Tabs
                await self.close_new_tabs(context, page)

                # Entferne Ad-Overlays die Clicks blockieren
                await self._remove_overlays(page)

                found_button = False

                # Scroll um verzögerte Popups zu triggern
                if attempt == 2:
                    try:
                        await page.evaluate("window.scrollBy(0, 300)")
                        await asyncio.sleep(0.5)
                        await page.evaluate("window.scrollTo(0, 0)")
                    except Exception:
                        pass

                # Check alle Frames (main + iframes mit content)
                for frame in page.frames:
                    frame_url = frame.url
                    if not frame_url or frame_url == 'about:blank' or frame_url == '':
                        continue

                    frame_name = "main" if frame == page.main_frame else f"iframe:{frame_url[:50]}"

                    # Prüfe auf modale Dialoge
                    modal_type = await self._detect_modal_popup(frame)
                    if modal_type:
                        logger.info(f"Detected {modal_type} modal in {frame_name}")

                    for selector in button_selectors:
                        try:
                            buttons = await frame.query_selector_all(selector)
                            if not buttons:
                                continue
                            for button in buttons:
                                try:
                                    is_visible = await button.is_visible()
                                    if not is_visible:
                                        continue
                                    text = (await button.inner_text()).strip()
                                    text_lower = text.lower()

                                    # Nur echte Confirm-Buttons (nicht Werbung)
                                    if any(ct in text_lower for ct in confirm_texts):
                                        logger.info(f"Found popup button in {frame_name}: '{text}'")
                                        try:
                                            # JavaScript-Click als Fallback falls normaler Click blockiert
                                            try:
                                                await button.click(timeout=2000, force=True)
                                            except Exception:
                                                await frame.evaluate('(el) => el.click()', button)
                                            clicked_count += 1
                                            found_button = True
                                            logger.info(f"Clicked popup #{clicked_count}")
                                            await asyncio.sleep(1.5)
                                            await self.close_new_tabs(context, page)
                                        except Exception as e:
                                            logger.debug(f"Click failed: {e}")
                                except Exception as e:
                                    logger.debug(f"Button interaction error: {e}")
                        except Exception as e:
                            logger.debug(f"Frame interaction error: {e}")

                if not found_button and attempt > 3:
                    break

            except Exception as e:
                logger.debug(f"Popup check attempt error: {e}")

        if clicked_count > 0:
            logger.info(f"Closed {clicked_count} popup(s) in {phase} phase")
            await asyncio.sleep(1.5)
        else:
            logger.debug(f"No popups found in {phase} phase")

        return clicked_count
    
    async def click_play_button(self, page, context):
        """Klickt den Play-Button - OPTIMIERT mit wait_for_selector"""
        logger.info("Looking for Play button...")

        # Scroll um iframe im Viewport zu haben
        try:
            await page.evaluate("window.scrollBy(0, 200)")
        except Exception as e:
            logger.debug(f"Scroll failed: {e}")

        # OPTIMIERT: Warte auf Video-Frame mit wait_for_selector statt fester 5s
        logger.info("Waiting for video iframe to load (smart wait)...")
        video_frame = None
        max_frame_wait = 15  # 15s timeout

        try:
            # Warte bis ein iframe mit Video erscheint
            loop = asyncio.get_event_loop()
            start_wait = loop.time()

            while (loop.time() - start_wait) < max_frame_wait:
                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    if not frame.url or frame.url == 'about:blank':
                        continue

                    try:
                        video = await frame.wait_for_selector('video', timeout=2000)
                        if video:
                            elapsed = loop.time() - start_wait
                            logger.info(f"Video iframe found in {elapsed:.1f}s!")
                            logger.info(f"Found video iframe: {frame.url[:80]}")
                            video_frame = frame
                            break
                    except Exception:
                        continue

                if video_frame:
                    break
                await asyncio.sleep(0.5)

        except Exception as e:
            logger.debug(f"Frame wait error: {e}")

        if not video_frame:
            logger.warning("No video iframe found after 15s")
            return False
        
        logger.info("Trying optimized playback methods...")

        # ============================================================
        # OPTIMIERT: Prüfe zuerst ob Video bereits spielt (readyState)
        # ============================================================
        try:
            video_state = await video_frame.evaluate("""
                () => {
                    const video = document.querySelector('video');
                    if (!video) return null;
                    return {
                        paused: video.paused,
                        readyState: video.readyState,
                        currentTime: video.currentTime
                    };
                }
            """)
            if video_state:
                if not video_state.get('paused') and video_state.get('currentTime', 0) > 0:
                    logger.info("Video already playing! Skipping play clicks.")
                    return True
                logger.debug(f"Video state: paused={video_state.get('paused')}, readyState={video_state.get('readyState')}")
        except Exception as e:
            logger.debug(f"Video state check failed: {e}")

        # ============================================================
        # Methode 1: Smart Play-Button Click mit Video-Start-Check
        # ============================================================
        try:
            logger.info("Method 1: Smart play button detection")

            # Kombinierter Selector für alle Play-Button Varianten
            play_selectors = [
                '[role="button"]:has-text("Spielen")',
                '[role="button"]:has-text("Play")',
                '.jw-icon-playback',
                '.jw-display-icon-container',
                'button[aria-label*="Play" i]',
            ]

            for selector in play_selectors:
                try:
                    # wait_for_selector ist effizienter als query_selector
                    play_button = await video_frame.wait_for_selector(
                        selector,
                        state='visible',
                        timeout=3000
                    )
                    if play_button:
                        logger.info(f"Found play button: {selector}")

                        # SMART CLICK: Max 4 Clicks mit Video-Start-Check
                        for click_num in range(1, 5):
                            try:
                                await play_button.click(timeout=2000, force=True)
                                logger.debug(f"Click #{click_num}")

                                # Schließe Werbe-Tabs
                                closed = await self.close_new_tabs(context, page)
                                if closed > 0:
                                    logger.debug(f"Closed {closed} ad tab(s)")

                                # OPTIMIERT: Prüfe ob Video gestartet hat
                                try:
                                    # wait_for_function statt fester Sleep!
                                    await video_frame.wait_for_function(
                                        '() => { const v = document.querySelector("video"); return v && !v.paused && v.currentTime > 0; }',
                                        timeout=3000
                                    )
                                    logger.info(f"Video started playing after {click_num} click(s)!")
                                    return True
                                except Exception:
                                    # Video noch nicht gestartet, nächster Click
                                    pass

                                # Re-query button (kann verschwunden sein)
                                play_button = await video_frame.query_selector(selector)
                                if not play_button:
                                    break

                            except Exception as e:
                                logger.debug(f"Click #{click_num} error: {e}")
                                break

                except Exception:
                    continue  # Nächster Selector

        except Exception as e:
            logger.debug(f"Smart play button failed: {e}")

        # ============================================================
        # Methode 2: JavaScript video.play() mit readyState check
        # ============================================================
        try:
            logger.info("Method 2: JavaScript play() with readiness check")

            # Warte bis Video bereit ist (readyState >= 2)
            try:
                await video_frame.wait_for_function(
                    '() => { const v = document.querySelector("video"); return v && v.readyState >= 2; }',
                    timeout=5000
                )
                logger.debug("Video is ready (readyState >= 2)")
            except Exception:
                logger.debug("Video not fully ready, trying play() anyway...")

            result = await video_frame.evaluate("""
                () => {
                    const videos = document.querySelectorAll('video');
                    let played = false;
                    videos.forEach(v => {
                        try {
                            v.play().then(() => { played = true; }).catch(() => {});
                        } catch(e) {}
                    });
                    return played;
                }
            """)
            if result:
                logger.info("Video.play() executed!")
                await self.close_new_tabs(context, page)

                # Kurze Wartezeit für Play-Start
                try:
                    await video_frame.wait_for_function(
                        '() => { const v = document.querySelector("video"); return v && !v.paused; }',
                        timeout=3000
                    )
                    logger.info("Video is now playing!")
                    return True
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"JS play failed: {e}")

        # ============================================================
        # Methode 3: Klicke direkt auf Video-Element
        # ============================================================
        try:
            logger.info("Method 3: Direct video click")
            video = await video_frame.query_selector('video')
            if video:
                await video.click(timeout=3000, force=True)
                await self.close_new_tabs(context, page)

                # Check ob Video gestartet
                try:
                    await video_frame.wait_for_function(
                        '() => { const v = document.querySelector("video"); return v && !v.paused; }',
                        timeout=3000
                    )
                    logger.info("Video started via direct click!")
                    return True
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Video click failed: {e}")

        logger.warning("All play methods tried - video might need manual start")
        return False
    
    async def extract_metadata_and_m3u8(self, url, wait_time=60, use_adblock=True, browser_id="", language=""):
        """
        FINAL VERSION:
        1. Load page + extract metadata
        2. Select language if specified
        3. Click 18+ popups
        4. Try multiple methods to start video
        5. Wait for m3u8

        browser_id: Optional identifier for logging in parallel mode (e.g. "[B1]")
        language: Language key (data-lang-key) to select before starting video
        """
        self.m3u8_urls = []
        self.master_playlist = None
        self.metadata = VideoMetadata()
        self.browser_id = browser_id  # For logging
        self._m3u8_event = asyncio.Event()  # Event-based m3u8 detection (instant!)

        # Helper function for logging with browser ID
        def log(msg, level="info", prefix=""):
            formatted = f"{self.browser_id} {msg}" if self.browser_id else msg
            getattr(logger, level)(formatted)

        async with async_playwright() as p:
            # Browser arguments
            browser_args = [
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--mute-audio',  # Browser-Level Audio-Mute
                '--autoplay-policy=no-user-gesture-required'
            ]

            # Get browser settings from config (with fallbacks for standalone mode)
            headless = False
            if app_config:
                headless = getattr(app_config.Config, 'BROWSER_HEADLESS', False)

            # Start browser off-screen when not headless (--start-minimized doesn't work with Playwright)
            if not headless:
                browser_args.extend([
                    '--window-position=-2400,-2400',  # Off-screen position
                    '--window-size=1280,720'
                ])

            browser = await p.chromium.launch(
                headless=headless,
                args=browser_args
            )

            # Context mit MUTED Audio (kein Ton nötig!)
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                is_mobile=False,
                has_touch=False,
                permissions=[],
                extra_http_headers={
                    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7'
                }
            )
            page = await context.new_page()

            # Stumm schalten via JavaScript - mehrere Methoden kombiniert
            await page.add_init_script("""
                // Methode 1: HTMLMediaElement muted erzwingen
                Object.defineProperty(HTMLMediaElement.prototype, 'muted', {
                    get: () => true,
                    set: () => {}
                });

                // Methode 2: Volume auf 0 setzen
                Object.defineProperty(HTMLMediaElement.prototype, 'volume', {
                    get: () => 0,
                    set: () => {}
                });

                // Methode 3: AudioContext blockieren
                window.AudioContext = undefined;
                window.webkitAudioContext = undefined;
            """)
            
            # ============================================================
            # AD-BLOCKING
            # ============================================================
            
            if use_adblock:
                if not self.ad_filters:
                    self.load_brave_filters()

                log(f"Ad-blocking enabled ({len(self.ad_filters)} rules)")

                blocked_count = [0]

                # Cache references for faster access in hot path
                _filters = self._ad_filters_frozen or frozenset(self.ad_filters)
                _patterns = self._blocked_patterns_tuple or ()

                async def block_ads_brave(route, request):
                    try:
                        url_lower = request.url.lower()
                        parsed = urlparse(request.url)
                        domain = parsed.netloc.lower()

                        # O(1) frozenset lookup instead of set
                        if domain in _filters:
                            blocked_count[0] += 1
                            if blocked_count[0] <= 10:
                                log(f"Blocked: {domain}", "debug")
                            await route.abort()
                            return

                        # Check parent domain
                        dot_idx = domain.find('.')
                        if dot_idx >= 0:
                            parent = domain[dot_idx + 1:]
                            if parent in _filters:
                                blocked_count[0] += 1
                                await route.abort()
                                return

                        # Pre-built tuple for faster pattern matching
                        if _patterns:
                            for pattern in _patterns:
                                if pattern in url_lower:
                                    blocked_count[0] += 1
                                    await route.abort()
                                    return

                        if request.resource_type == 'font':
                            await route.abort()
                            return

                    except Exception as e:
                        logger.debug(f"Ad filter check error: {e}")

                    await route.continue_()
                
                await page.route("**/*", block_ads_brave)
            
            # ============================================================
            # M3U8 REQUEST HANDLER
            # ============================================================
            
            async def handle_request(request):
                url = request.url
                if '.m3u8' in url:
                    log(f"Found m3u8: {url[:100]}...")
                    self.m3u8_urls.append(url)
                    if not self.master_playlist and ('master' in url.lower() or len(self.m3u8_urls) == 1):
                        self.master_playlist = url
                    # Trigger event immediately! (no more 2s polling delay)
                    self._m3u8_event.set()

            page.on("request", handle_request)

            # ============================================================
            # SEITE LADEN
            # ============================================================

            log(f"Loading page: {url}")
            await page.goto(url, wait_until="domcontentloaded")

            log("Extracting metadata from page...")

            # Metadaten-Extraktion - Serienname
            # Priorität 1: h1[itemprop="name"] - enthält den echten Seriennamen
            try:
                h1_element = await page.query_selector('h1[itemprop="name"]')
                if h1_element:
                    # Versuche erst den span innerhalb des h1
                    span_element = await h1_element.query_selector('span')
                    if span_element:
                        self.metadata.series_name = await span_element.inner_text()
                    else:
                        self.metadata.series_name = await h1_element.inner_text()

                    # Bereinige den Namen (entferne "Serien Stream:" falls vorhanden)
                    if self.metadata.series_name:
                        self.metadata.series_name = self.metadata.series_name.strip()
                        self.metadata.series_name_display = self.metadata.series_name
                        log(f"Series (from h1): {self.metadata.series_name}")
            except Exception as e:
                log(f"Could not extract series name from h1: {e}")

            # Priorität 2: Fallback auf .hosterSeriesTitle strong
            if not self.metadata.series_name:
                try:
                    series_element = await page.query_selector('.hosterSeriesTitle strong')
                    if series_element:
                        self.metadata.series_name = await series_element.inner_text()
                        self.metadata.series_name_display = self.metadata.series_name
                        log(f"Series (from hosterSeriesTitle): {self.metadata.series_name}")
                except Exception as e:
                    logger.debug(f"Could not extract series from hosterSeriesTitle: {e}")

            try:
                season_meta = await page.query_selector('meta[itemprop="seasonNumber"]')
                if season_meta:
                    self.metadata.season = await season_meta.get_attribute('content')
                    log(f"Season: {self.metadata.season}")
            except Exception as e:
                logger.debug(f"Could not extract season metadata: {e}")

            try:
                episode_meta = await page.query_selector('meta[itemprop="episode"]')
                if episode_meta:
                    self.metadata.episode = await episode_meta.get_attribute('content')
                    log(f"Episode: {self.metadata.episode}")
            except Exception as e:
                logger.debug(f"Could not extract episode metadata: {e}")

            try:
                german_title = await page.query_selector('.episodeGermanTitle')
                if german_title:
                    self.metadata.episode_title_german = await german_title.inner_text()
                    log(f"German Title: {self.metadata.episode_title_german}")
            except Exception as e:
                logger.debug(f"Could not extract German title: {e}")

            try:
                english_title = await page.query_selector('small.episodeEnglishTitle')
                if english_title:
                    self.metadata.episode_title_english = await english_title.inner_text()
                    log(f"English Title: {self.metadata.episode_title_english}")
            except Exception as e:
                logger.debug(f"Could not extract English title: {e}")

            if not self.metadata.series_name:
                self._parse_url_fallback(url)

            # ============================================================
            # WICHTIG: SCROLL NACH UNTEN damit iframe sichtbar wird
            # ============================================================

            log("Scrolling page to load iframe properly...")
            try:
                # Scroll mehrmals nach unten
                for i in range(3):
                    await page.evaluate("window.scrollBy(0, 300)")
                    await asyncio.sleep(0.5)
                log("Scrolled down", "debug")
            except Exception as e:
                log(f"Scroll failed: {e}", "warning")

            # ============================================================
            # LANGUAGE SELECTION (if specified)
            # ============================================================

            log(f"Language parameter received: '{language}'", "debug")

            if language:
                log(f"Selecting language: {language}")
                try:
                    # DEBUG: List all available languages first
                    available_langs = await page.evaluate('''() => {
                        const imgs = document.querySelectorAll('.changeLanguageBox img[data-lang-key]');
                        return Array.from(imgs).map(img => ({
                            key: img.getAttribute('data-lang-key'),
                            title: img.getAttribute('title'),
                            selected: img.classList.contains('selectedLanguage'),
                            src: img.src
                        }));
                    }''')
                    log(f"Available languages: {available_langs}", "debug")

                    # Find the language image with the specified data-lang-key
                    lang_selector = f'.changeLanguageBox img[data-lang-key="{language}"]'

                    # First check if language is already selected
                    is_already_selected = await page.evaluate(f'''() => {{
                        const img = document.querySelector('.changeLanguageBox img[data-lang-key="{language}"]');
                        return img ? img.classList.contains("selectedLanguage") : false;
                    }}''')

                    if is_already_selected:
                        log(f"Language {language} already selected")
                    else:
                        # FIRST: Remove ad overlays that block clicks
                        log(f"Removing ad overlays...", "debug")
                        await page.evaluate('''() => {
                            // Remove common ad overlay elements
                            const adSelectors = [
                                '#zfj1cbm', '#lkk9s',  // Known ad IDs from error
                                '[id*="ad"]', '[class*="overlay"]',
                                '[style*="z-index: 999"]', '[style*="z-index:999"]',
                                '[style*="position: fixed"]', '[style*="position:fixed"]'
                            ];
                            adSelectors.forEach(sel => {
                                try {
                                    document.querySelectorAll(sel).forEach(el => {
                                        // Only remove if it looks like an ad (not the actual content)
                                        if (el.tagName === 'A' || el.id.match(/^[a-z]{5,}$/i) ||
                                            el.innerHTML.includes('coleastrehabilitation') ||
                                            el.innerHTML.includes('protrafficinspector')) {
                                            el.remove();
                                        }
                                    });
                                } catch(e) {}
                            });
                            // Also remove any full-screen overlays
                            document.querySelectorAll('div').forEach(el => {
                                const style = window.getComputedStyle(el);
                                if (style.position === 'fixed' &&
                                    style.zIndex > 100 &&
                                    el.offsetWidth > window.innerWidth * 0.8 &&
                                    el.offsetHeight > window.innerHeight * 0.8) {
                                    el.remove();
                                }
                            });
                        }''')
                        await asyncio.sleep(0.5)

                        # Get current iframe src before clicking
                        iframe_before = await page.evaluate('''() => {
                            const iframe = document.querySelector('.inSiteWebStream iframe');
                            return iframe ? iframe.src : null;
                        }''')
                        log(f"Current iframe: {iframe_before[:60] if iframe_before else 'none'}...", "debug")

                        # STEP 1: Click the language FLAG to make hosters for that language visible
                        max_attempts = 5
                        language_flag_changed = False

                        for attempt in range(max_attempts):
                            if language_flag_changed:
                                break

                            # Remove overlays again before each attempt
                            await page.evaluate('''() => {
                                ['#zfj1cbm', '#lkk9s'].forEach(sel => {
                                    const el = document.querySelector(sel);
                                    if (el) el.remove();
                                });
                            }''')

                            # Re-query the element each time (DOM might have changed)
                            lang_element = await page.query_selector(lang_selector)
                            if not lang_element:
                                log(f"Language element not found on attempt {attempt + 1}", "debug")
                                await asyncio.sleep(1.0)
                                continue

                            # Count current pages/tabs before click
                            pages_before = len(context.pages)

                            # Click the language flag using JavaScript to bypass overlays
                            try:
                                # Use JavaScript click to bypass any remaining overlays
                                await page.evaluate(f'''() => {{
                                    const img = document.querySelector('.changeLanguageBox img[data-lang-key="{language}"]');
                                    if (img) img.click();
                                }}''')
                                log(f"Clicked language flag via JS (attempt {attempt + 1})", "debug")
                            except Exception as click_err:
                                log(f"Click failed: {click_err}", "warning")
                                continue

                            # Wait a moment for page to react
                            await asyncio.sleep(1.5)

                            # Close any new popup tabs that opened
                            pages_after = context.pages
                            if len(pages_after) > pages_before:
                                closed_count = 0
                                for p in pages_after[pages_before:]:
                                    try:
                                        await p.close()
                                        closed_count += 1
                                    except Exception as e:
                                        logger.debug(f"Failed to close popup tab: {e}")
                                if closed_count > 0:
                                    log(f"Closed {closed_count} popup tab(s)", "debug")

                            # Check if language flag is now selected
                            is_now_selected = await page.evaluate(f'''() => {{
                                const img = document.querySelector('.changeLanguageBox img[data-lang-key="{language}"]');
                                return img ? img.classList.contains("selectedLanguage") : false;
                            }}''')

                            if is_now_selected:
                                log(f"Language flag {language} now selected")
                                language_flag_changed = True
                                break

                            if attempt < max_attempts - 1:
                                log(f"Language flag not yet selected, retrying...", "debug")
                                await asyncio.sleep(1.0)

                        if not language_flag_changed:
                            log(f"Could not select language flag after {max_attempts} attempts", "warning")

                        # After clicking language flag, the iframe automatically loads the new video
                        # Wait for the new iframe content to load
                        log(f"Waiting for new language content to load...")
                        await asyncio.sleep(2.0)

                        # Verify iframe changed
                        iframe_after = await page.evaluate('''() => {
                            const iframe = document.querySelector('.inSiteWebStream iframe');
                            return iframe ? iframe.src : null;
                        }''')
                        if iframe_after and iframe_before and iframe_after != iframe_before:
                            log(f"Iframe updated to: {iframe_after[:60]}...")
                        else:
                            log(f"Iframe may not have changed yet", "warning")

                except Exception as e:
                    log(f"Language selection failed: {e}", "warning")
                    logger.warning("Language selection error details", exc_info=True)
                    # List available languages for debugging
                    try:
                        available = await page.evaluate('''() => {
                            const imgs = document.querySelectorAll('.changeLanguageBox img[data-lang-key]');
                            return Array.from(imgs).map(img => ({
                                key: img.getAttribute('data-lang-key'),
                                title: img.getAttribute('title'),
                                selected: img.classList.contains('selectedLanguage')
                            }));
                        }''')
                        if available:
                            log(f"Available languages: {available}", "debug")
                    except Exception as e:
                        logger.debug(f"Could not list available languages: {e}")

            # ============================================================
            # 18+ POPUPS WEGKLICKEN (falls vorhanden)
            # Note: Button Debugger zeigt keine Popups auf dieser Seite!
            # ============================================================

            await asyncio.sleep(2)
            popup_count = await self.click_18_plus_popups(page, context, "initial")
            
            if popup_count == 0:
                logger.debug("No popups found (as expected for this site)")
            
            # ============================================================
            # PLAY BUTTON KLICKEN (role='button' mit "Spielen" Text!)
            # ============================================================
            
            play_clicked = await self.click_play_button(page, context)
            
            if not play_clicked:
                logger.warning("Play button not clicked - video might auto-play or need manual start")
            
            # ============================================================
            # WARTE AUF M3U8 (Event-basiert - SOFORTIGE Erkennung!)
            # ============================================================

            log(f"Waiting up to {wait_time}s for m3u8 (event-based)...")

            start_time = asyncio.get_event_loop().time()

            try:
                # Event-basiertes Warten - reagiert SOFORT wenn m3u8 gefunden!
                # Kein 2-Sekunden-Polling mehr nötig!
                await asyncio.wait_for(self._m3u8_event.wait(), timeout=wait_time)
                elapsed = asyncio.get_event_loop().time() - start_time
                log(f"SUCCESS! m3u8 found after {elapsed:.1f} seconds! (event-based)")
                log("Closing browser to free resources...")
            except asyncio.TimeoutError:
                log(f"No m3u8 found after {wait_time}s", "error")
                log("Try: 1. Increase --wait to 70 or 80, 2. Check if video started playing in browser, 3. Try without --no-adblock", "error")

            if use_adblock and blocked_count[0] > 10:
                log(f"Total blocked: {blocked_count[0]} requests")

            # Browser schließen (jetzt auch wenn m3u8 gefunden wurde)
            await browser.close()
            if self.m3u8_urls:
                log("Browser closed, resources freed")
            
        return self.m3u8_urls
    
    def _parse_url_fallback(self, url: str) -> None:
        """Fallback: Parse URL to extract metadata from URL pattern."""
        pattern = r'/serie/stream/([^/]+)/staffel-(\d+)/episode-(\d+)'
        match = re.search(pattern, url)
        
        if match:
            if not self.metadata.series_name:
                self.metadata.series_name = match.group(1)
            if not self.metadata.season:
                self.metadata.season = match.group(2)
            if not self.metadata.episode:
                self.metadata.episode = match.group(3)
            logger.info(f"Fallback URL parsing successful")

def parse_episode_range(episodes_str: str, max_episodes: int = 500) -> List[int]:
    """
    Parse episode range string into list of episode numbers.

    Args:
        episodes_str: String like "1-5,8,10-12"
        max_episodes: Maximum number of episodes allowed (prevents DoS)

    Returns:
        Sorted list of episode numbers

    Raises:
        ValueError: If range is invalid or exceeds max_episodes
    """
    episodes = set()

    for part in episodes_str.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            start, end = map(int, part.split('-', 1))
            if start < 0 or end < 0 or start > end:
                raise ValueError(f"Invalid range: {part}")
            if end - start + 1 > max_episodes:
                raise ValueError(f"Range {part} exceeds maximum of {max_episodes} episodes")
            episodes.update(range(start, end + 1))
        else:
            episodes.add(int(part))
        if len(episodes) > max_episodes:
            raise ValueError(f"Total episodes exceed maximum of {max_episodes}")

    return sorted(episodes)

async def detect_series_info(url: str) -> Tuple[Optional[int], Optional[List[int]]]:
    """
    Detect number of seasons and episodes from series page.
    Uses SingleBrowserScraper for efficient single-page extraction.

    Args:
        url: URL of the series page

    Returns:
        Tuple of (total_seasons, list_of_episodes) or (None, None) on error
    """
    try:
        from app.browser_pool import SingleBrowserScraper
        scraper = SingleBrowserScraper(headless=True)
        data = await scraper.scrape_page(url, '''
            () => {
                let totalSeasons = null;
                const seasonsMeta = document.querySelector('meta[itemprop="numberOfSeasons"]');
                if (seasonsMeta) totalSeasons = parseInt(seasonsMeta.getAttribute('content'));

                const episodes = new Set();
                document.querySelectorAll('a[href*="/episode-"]').forEach(link => {
                    const href = link.getAttribute('href');
                    if (href) {
                        const match = href.match(/\\/episode-(\\d+)/);
                        if (match) episodes.add(parseInt(match[1]));
                    }
                });

                return {
                    totalSeasons: totalSeasons,
                    episodes: Array.from(episodes).sort((a, b) => a - b)
                };
            }
        ''')
        return data.get('totalSeasons'), data.get('episodes') or None
    except Exception as e:
        logger.warning(f"Could not auto-detect series info: {e}")
        return None, None

def parse_flexible_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int], Optional[str]]:
    """
    Parse URL with flexible format support for both series and anime.

    Series patterns:
    1. http://site/serie/stream/NAME
    2. http://site/serie/stream/NAME/staffel-N
    3. http://site/serie/stream/NAME/staffel-N/episode-N

    Anime patterns:
    4. http://site/anime/stream/NAME
    5. http://site/anime/stream/NAME/staffel-N
    6. http://site/anime/stream/NAME/staffel-N/episode-N

    Args:
        url: The URL to parse

    Returns:
        Tuple of (base_url, series_slug, season, episode, url_type)
        where url_type is 'anime' or 'series'
    """
    # Try ANIME patterns first (more specific)
    # Pattern A1: Full URL with season and episode (anime)
    pattern_anime_full = r'(https?://[^/]+/anime/stream/([^/]+))/staffel-(\d+)/episode-(\d+)'
    match = re.match(pattern_anime_full, url)
    if match:
        return (
            match.group(1),  # base_url
            match.group(2),  # series_slug
            int(match.group(3)),  # season
            int(match.group(4)),  # episode
            'full'
        )

    # Pattern A2: URL with season only (anime)
    pattern_anime_season = r'(https?://[^/]+/anime/stream/([^/]+))/staffel-(\d+)/?'
    match = re.match(pattern_anime_season, url)
    if match:
        return (
            match.group(1),  # base_url
            match.group(2),  # series_slug
            int(match.group(3)),  # season
            None,  # episode (to be determined)
            'season'
        )

    # Pattern A2b: URL with extra tab (filme, specials, ova, movies) - anime
    pattern_anime_extra = r'(https?://[^/]+/anime/stream/([^/]+))/(filme|specials|ova|movies)/?'
    match = re.match(pattern_anime_extra, url)
    if match:
        return (
            match.group(1),  # base_url
            match.group(2),  # series_slug
            match.group(3),  # extra_type (filme, specials, etc.)
            None,  # episode (to be determined)
            'extra'  # New type to indicate it's an extra tab
        )

    # Pattern A3: URL with anime only
    pattern_anime_series = r'(https?://[^/]+/anime/stream/([^/]+))/?'
    match = re.match(pattern_anime_series, url)
    if match:
        return (
            match.group(1),  # base_url
            match.group(2),  # series_slug
            None,  # season (to be determined)
            None,  # episode (to be determined)
            'series'
        )

    # Try SERIES patterns (original)
    # Pattern 1: Full URL with season and episode
    pattern_full = r'(https?://[^/]+/serie/stream/([^/]+))/staffel-(\d+)/episode-(\d+)'
    match = re.match(pattern_full, url)
    if match:
        return (
            match.group(1),  # base_url
            match.group(2),  # series_slug
            int(match.group(3)),  # season
            int(match.group(4)),  # episode
            'full'
        )

    # Pattern 2: URL with season only
    pattern_season = r'(https?://[^/]+/serie/stream/([^/]+))/staffel-(\d+)/?'
    match = re.match(pattern_season, url)
    if match:
        return (
            match.group(1),  # base_url
            match.group(2),  # series_slug
            int(match.group(3)),  # season
            None,  # episode (to be determined)
            'season'
        )

    # Pattern 2b: URL with extra tab (filme, specials, ova, movies)
    pattern_extra = r'(https?://[^/]+/serie/stream/([^/]+))/(filme|specials|ova|movies)/?'
    match = re.match(pattern_extra, url)
    if match:
        return (
            match.group(1),  # base_url
            match.group(2),  # series_slug
            match.group(3),  # extra_type (filme, specials, etc.) - stored as "season"
            None,  # episode (to be determined)
            'extra'  # New type to indicate it's an extra tab
        )

    # Pattern 3: URL with series only
    pattern_series = r'(https?://[^/]+/serie/stream/([^/]+))/?'
    match = re.match(pattern_series, url)
    if match:
        return (
            match.group(1),  # base_url
            match.group(2),  # series_slug
            None,  # season (to be determined)
            None,  # episode (to be determined)
            'series'
        )

    return None, None, None, None, None

def download_video_sync(m3u8_url, output_path, quality="best"):
    """Synchronous download function (to be called in thread executor)"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Downloading...")
    logger.info(f"From: {m3u8_url[:80]}...")
    logger.info(f"To: {output_path.name}")
    logger.info(f"Quality: {quality}")

    # Use Python module to ensure it works from venv
    command = [
        sys.executable, "-m", "yt_dlp",
        "-o", str(output_path),
        "-f", quality,
        "--no-warnings",
        "--progress",
        m3u8_url
    ]

    try:
        subprocess.run(command, check=True)
        logger.info(f"Download complete: {output_path.name}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"yt-dlp failed: {e}")
        logger.info("Trying with ffmpeg...")
        from app.ffmpeg_setup import get_ffmpeg_executable
        try:
            ffmpeg_command = [
                get_ffmpeg_executable(),
                "-i", m3u8_url,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                str(output_path),
                "-loglevel", "warning",
                "-stats"
            ]
            subprocess.run(ffmpeg_command, check=True)
            logger.info(f"Download complete: {output_path.name}")
            return True
        except subprocess.CalledProcessError:
            logger.error("ffmpeg also failed")
            return False


async def download_video(m3u8_url, output_path, quality="best"):
    """Async wrapper for download_video - uses global executor to avoid overhead"""
    import asyncio

    loop = asyncio.get_event_loop()

    # Use global executor instead of creating a new one (saves ~50-100ms per download)
    result = await loop.run_in_executor(
        _DOWNLOAD_EXECUTOR,
        download_video_sync,
        m3u8_url,
        output_path,
        quality
    )

    return result

async def process_single_episode(extractor, url, args, browser_id=""):
    """Verarbeitet eine einzelne Episode"""
    if browser_id:
        logger.info(f"{browser_id} {'='*60}")
        logger.info(f"{browser_id} Processing: {url}")
        logger.info(f"{browser_id} {'='*60}")
    else:
        logger.info(f"{'='*70}")
        logger.info(f"Processing: {url}")
        logger.info(f"{'='*70}")

    m3u8_urls = await extractor.extract_metadata_and_m3u8(
        url,
        wait_time=args.wait,
        use_adblock=not args.no_adblock,
        browser_id=browser_id
    )
    
    if not m3u8_urls:
        logger.error(f"No m3u8 found for {url}")
        return False
    
    title_info = ""
    if extractor.metadata.episode_title_english:
        title_info = f" - {extractor.metadata.episode_title_english}"
    elif extractor.metadata.episode_title_german:
        title_info = f" - {extractor.metadata.episode_title_german}"
    logger.info(f"Extracted: S{extractor.metadata.season}E{extractor.metadata.episode}{title_info}")
    
    if args.series_display:
        extractor.metadata.series_name_display = args.series_display
    
    output_path = extractor.metadata.get_full_path(
        base_path=args.base_path,
        use_english_title=args.english_title,
        quality=args.quality_tag,
        format_ext=args.format
    )
    
    if output_path.exists() and not args.force:
        logger.warning(f"File already exists: {output_path.name}")
        logger.warning("Skipping... (use --force to overwrite)")
        return True
    
    logger.info(f"Full path: {output_path}")
    
    download_url = extractor.master_playlist if extractor.master_playlist else m3u8_urls[0]

    if not args.no_download:
        success = await download_video(download_url, output_path, args.quality)
        return success
    else:
        logger.info("Metadata extracted (download skipped)")
        logger.info(f"Manual: yt-dlp -o '{output_path}' {download_url}")
        return True

async def process_episode_with_semaphore(semaphore, ep_num, base_url, season, args, episode_idx, total_episodes, browser_num):
    """Verarbeitet eine Episode mit Semaphore (für Parallelisierung)"""
    browser_id = f"[B{browser_num}]"

    async with semaphore:
        url = f"{base_url}/staffel-{season}/episode-{ep_num}"

        logger.info(f"{browser_id} Starting S{season:02d}E{ep_num:02d} ({episode_idx}/{total_episodes})")

        try:
            # Jede Episode bekommt ihren eigenen Extractor
            extractor = HLSExtractor()
            result = await process_single_episode(extractor, url, args, browser_id)

            if result:
                logger.info(f"{browser_id} S{season:02d}E{ep_num:02d} - SUCCESS! ({episode_idx}/{total_episodes})")
            else:
                logger.error(f"{browser_id} S{season:02d}E{ep_num:02d} - FAILED! ({episode_idx}/{total_episodes})")

            return result
        except Exception as e:
            logger.error(f"{browser_id} S{season:02d}E{ep_num:02d} - ERROR: {e} ({episode_idx}/{total_episodes})")
            return False

async def main():
    parser = argparse.ArgumentParser(
        description='HLS Video Downloader - FINAL VERSION',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single episode (default 60s wait)
  %(prog)s "http://186.2.175.5/serie/stream/unbesiegbar/staffel-3/episode-7"
  
  # With longer wait time
  %(prog)s "URL" --wait 70
  
  # Full season (sequential)
  %(prog)s "URL" --episodes 1-8
  
  # Full season (4 parallel windows - FAST!)
  %(prog)s "URL" --episodes 1-8 --parallel 4
  
  # Test without download
  %(prog)s "URL" --no-download
        """
    )
    
    parser.add_argument('url', help='Episode URL')
    parser.add_argument('-b', '--base-path', default='.', help='Base path for series')
    parser.add_argument('-w', '--wait', type=int, default=60, help='Wait time for m3u8 in seconds (default: 60)')
    parser.add_argument('-q', '--quality', default='best', help='yt-dlp quality')
    parser.add_argument('--quality-tag', default='1080p', help='Quality tag in filename')
    parser.add_argument('--format', default='mkv', choices=['mkv', 'mp4', 'avi'], help='File format')
    parser.add_argument('--english-title', action='store_true', help='Use English title')
    parser.add_argument('--series-display', help='Override series name')
    parser.add_argument('--episodes', help='Episode range (e.g. "1-8" or "1,3,5-8")')
    parser.add_argument('--season', type=int, help='Override season number')
    parser.add_argument('--no-adblock', action='store_true', help='Disable ad-blocking')
    parser.add_argument('--no-download', action='store_true', help='Extract metadata only')
    parser.add_argument('--force', action='store_true', help='Overwrite existing files')
    parser.add_argument('--parallel', type=int, default=1, choices=[1, 2, 3, 4], 
                       help='Number of parallel browser windows (1-4, default: 1)')
    
    args = parser.parse_args()
    
    logger.info("="*70)
    logger.info("HLS Video Downloader - FINAL VERSION")
    logger.info("Handles 18+ popups and play button automatically")
    if args.parallel > 1:
        logger.info(f"PARALLEL MODE: {args.parallel} concurrent windows")
    logger.info("="*70)

    # Parse URL with flexible format support
    base_url, series_slug, season, start_episode, url_type = parse_flexible_url(args.url)

    if not base_url:
        logger.error("Invalid URL format!")
        logger.error("Supported formats: 1. http://site/serie/stream/NAME/staffel-N/episode-N, 2. http://site/serie/stream/NAME/staffel-N, 3. http://site/serie/stream/NAME")
        sys.exit(1)

    logger.info(f"Detected URL type: {url_type}")
    logger.info(f"Series: {series_slug}")

    # Handle different URL types
    if url_type == 'series':
        # No season specified - need to detect or ask
        logger.warning("No season specified in URL")

        if args.season:
            season = args.season
            logger.info(f"Using --season argument: Season {season}")
        else:
            # Auto-detect available seasons
            logger.info("Auto-detecting available seasons...")
            total_seasons, _ = await detect_series_info(args.url)

            if total_seasons:
                logger.info(f"Found {total_seasons} season(s)")
                if total_seasons == 1:
                    season = 1
                    logger.info(f"Auto-selected: Season 1")
                else:
                    logger.info(f"Available seasons: 1-{total_seasons}")
                    logger.info("Please specify season with --season N")
                    sys.exit(1)
            else:
                logger.error("Could not auto-detect seasons")
                logger.error("Please specify season with --season N")
                sys.exit(1)

    elif url_type == 'season':
        # Season specified, but no episode
        logger.info(f"Season: {season}")

        if not args.episodes:
            # Auto-detect available episodes
            logger.info(f"Auto-detecting episodes for Season {season}...")
            season_url = f"{base_url}/staffel-{season}"
            _, available_episodes = await detect_series_info(season_url)

            if available_episodes:
                logger.info(f"Found {len(available_episodes)} episode(s): {available_episodes[0]}-{available_episodes[-1]}")
                logger.info(f"Tip: Use --episodes {available_episodes[0]}-{available_episodes[-1]} to download all")

                # Use first episode as start
                start_episode = available_episodes[0]
                logger.info(f"Using first episode: E{start_episode:02d}")
            else:
                logger.error("Could not auto-detect episodes")
                start_episode = 1
                logger.warning("Defaulting to Episode 1")

    else:  # url_type == 'full'
        logger.info(f"Season: {season}")
        logger.info(f"Episode: {start_episode}")

    # Override season if specified
    if args.season:
        season = args.season
        logger.info(f"Season overridden to: {season}")
    
    if args.episodes:
        episodes = parse_episode_range(args.episodes)
        logger.info(f"Batch mode: {len(episodes)} episode(s) from Season {season}")
        logger.info(f"Episodes: {episodes}")
        if args.parallel > 1:
            logger.info(f"Processing {args.parallel} episodes in parallel")
    else:
        episodes = [start_episode]
        logger.info(f"Single mode: S{season:02d}E{start_episode:02d}")
    
    successful = 0
    failed = 0
    total = len(episodes)
    
    # ============================================================
    # PARALLEL vs SEQUENTIAL PROCESSING
    # ============================================================
    
    if args.parallel > 1 and len(episodes) > 1:
        # PARALLEL MODE (2-4 gleichzeitige Browser)
        logger.info(f"Starting parallel processing with {args.parallel} windows...")
        logger.info(f"This will be ~{args.parallel}x faster!")
        
        # Semaphore limitiert auf max. parallel Fenster
        semaphore = asyncio.Semaphore(args.parallel)
        
        # Erstelle Tasks für alle Episoden
        tasks = []
        for i, ep_num in enumerate(episodes, 1):
            # Browser-Nummer wird zyklisch vergeben (1-4 für 4 parallel)
            browser_num = ((i - 1) % args.parallel) + 1
            task = process_episode_with_semaphore(
                semaphore,
                ep_num,
                base_url,
                season,
                args,
                i,
                total,
                browser_num
            )
            tasks.append(task)
        
        # Führe alle Tasks parallel aus (aber max. parallel gleichzeitig)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Zähle Erfolge/Fehler
        for result in results:
            if isinstance(result, Exception):
                failed += 1
            elif result:
                successful += 1
            else:
                failed += 1
    
    else:
        # SEQUENTIAL MODE (1 Browser nach dem anderen)
        if len(episodes) > 1:
            logger.info("Sequential processing (use --parallel 2-4 for faster processing)")
        
        extractor = HLSExtractor()
        
        for i, ep_num in enumerate(episodes, 1):
            url = f"{base_url}/staffel-{season}/episode-{ep_num}"
            
            logger.info(f"[Episode {i}/{total}]")
            
            try:
                result = await process_single_episode(extractor, url, args)
                if result:
                    successful += 1
                else:
                    failed += 1
            except KeyboardInterrupt:
                logger.warning("Aborted")
                break
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                failed += 1
            
            # Pause zwischen Episoden (nur im Sequential Mode)
            if len(episodes) > 1 and ep_num != episodes[-1]:
                logger.info(f"Waiting 2s before next episode...")
                await asyncio.sleep(2)
    
    logger.info(f"{'='*70}")
    logger.info("Summary:")
    logger.info(f"Successful: {successful}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Total: {total}")
    logger.info(f"{'='*70}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("Aborted")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
