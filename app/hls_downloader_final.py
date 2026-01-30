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
from concurrent.futures import ThreadPoolExecutor

# Fix Windows console encoding for special characters
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

# Import config for browser settings
try:
    from app import config as app_config
except ImportError:
    app_config = None  # Fallback when running standalone

# Setup logging
logger = logging.getLogger(__name__)

# Global ThreadPoolExecutor for download operations (reused to avoid overhead)
_DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="HLSDownload")

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
        self._m3u8_event = None  # Event for instant m3u8 detection (created per extraction)
    
    def load_brave_filters(self):
        """Lädt Brave-kompatible Filterlisten"""
        filter_cache = Path("./filter_cache")
        filter_cache.mkdir(exist_ok=True)

        # Multiple URLs for redundancy (primary and fallback)
        filter_lists = {
            'easylist': [
                'https://easylist-downloads.adblockplus.org/easylist.txt',
                'https://easylist.to/easylist/easylist.txt'
            ],
            'easyprivacy': [
                'https://easylist-downloads.adblockplus.org/easyprivacy.txt',
                'https://easylist.to/easylist/easyprivacy.txt'
            ],
        }

        for name, urls in filter_lists.items():
            cache_file = filter_cache / f"{name}.txt"

            should_download = False
            if not cache_file.exists():
                should_download = True
            else:
                file_age = time.time() - cache_file.stat().st_mtime
                if file_age > 604800:  # 7 days
                    should_download = True

            if should_download:
                downloaded = False
                for url in urls:
                    try:
                        urllib.request.urlretrieve(url, cache_file)
                        downloaded = True
                        break
                    except Exception:
                        continue

                if not downloaded:
                    # Silently skip if all downloads failed and no cache exists
                    if not cache_file.exists():
                        continue
            
            try:
                with open(cache_file, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('!') or line.startswith('['):
                            continue
                        
                        if '||' in line:
                            domain = line.replace('||', '').split('^')[0].split('/')[0].split('$')[0]
                            if domain and '.' in domain:
                                self.ad_filters.add(domain.lower())
                        elif line.startswith('|https://') or line.startswith('|http://'):
                            try:
                                domain = line.split('://')[1].split('/')[0].split('$')[0]
                                if domain and '.' in domain:
                                    self.ad_filters.add(domain.lower())
                            except (IndexError, ValueError):
                                # Malformed filter line, skip silently
                                pass
            except Exception as e:
                print(f"⚠ Error parsing {name}: {e}")
        
        print(f"✓ Loaded {len(self.ad_filters)} filter rules")
        return len(self.ad_filters) > 0
    
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
    
    async def click_18_plus_popups(self, page, context, phase="initial"):
        """Klickt alle 18+ OK Buttons weg - NUR ECHTE POPUPS!"""
        print(f"\n🔞 Checking for 18+ popups ({phase})...")
        
        clicked_count = 0
        max_attempts = 10
        
        button_selectors = [
            'button:has-text("OK")',
            'button:has-text("Ok")',
            'button:has-text("Confirm")',
            '.swal2-confirm',
            '.swal2-styled',
            'button[class*="confirm"]',
        ]
        
        for attempt in range(max_attempts):
            try:
                await asyncio.sleep(1)
                
                # Schließe neue Tabs (Werbung)
                await self.close_new_tabs(context, page)
                
                found_button = False
                
                # Check ALLE Frames ABER ignoriere about:blank (sind Werbe-iframes)
                for frame in page.frames:
                    frame_url = frame.url
                    
                    # WICHTIG: Ignoriere about:blank und leere iframes!
                    if not frame_url or frame_url == 'about:blank' or frame_url == '':
                        continue
                    
                    frame_name = "main" if frame == page.main_frame else f"iframe:{frame_url[:50]}"
                    
                    for selector in button_selectors:
                        try:
                            buttons = await frame.query_selector_all(selector)
                            if buttons:
                                for button in buttons:
                                    try:
                                        is_visible = await button.is_visible()
                                        if is_visible:
                                            text = await button.inner_text()
                                            text_lower = text.lower()
                                            
                                            # Nur echte Confirm-Buttons
                                            if 'ok' in text_lower or 'confirm' in text_lower:
                                                print(f"  ✓ Found popup in {frame_name}")
                                                print(f"    Button text: '{text}'")
                                                
                                                # Click mit extra Vorsicht
                                                try:
                                                    await button.click(timeout=2000, force=True)
                                                    clicked_count += 1
                                                    found_button = True
                                                    print(f"  ✓ Clicked popup #{clicked_count}")
                                                    await asyncio.sleep(2)
                                                    
                                                    # Schließe neue Tabs die durch Click entstanden
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
            print(f"✓ Closed {clicked_count} popup(s) in {phase} phase")
            await asyncio.sleep(2)
        else:
            print(f"  No popups found in {phase} phase")
        
        return clicked_count
    
    async def click_play_button(self, page, context):
        """Klickt den Play-Button - OPTIMIERT mit wait_for_selector"""
        print("\n▶️  Looking for Play button...")

        # Scroll um iframe im Viewport zu haben
        try:
            await page.evaluate("window.scrollBy(0, 200)")
        except Exception as e:
            logger.debug(f"Scroll failed: {e}")

        # OPTIMIERT: Warte auf Video-Frame mit wait_for_selector statt fester 5s
        print("  Waiting for video iframe to load (smart wait)...")
        video_frame = None
        max_frame_wait = 15000  # 15s timeout (statt feste 5s)

        try:
            # Warte bis ein iframe mit Video erscheint
            start_wait = asyncio.get_event_loop().time()

            while (asyncio.get_event_loop().time() - start_wait) < (max_frame_wait / 1000):
                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    if not frame.url or frame.url == 'about:blank':
                        continue

                    try:
                        # wait_for_selector ist viel effizienter als query_selector + sleep
                        video = await frame.wait_for_selector('video', timeout=2000)
                        if video:
                            elapsed = asyncio.get_event_loop().time() - start_wait
                            print(f"  ✓ Video iframe found in {elapsed:.1f}s!")
                            logger.info(f"Found video iframe: {frame.url[:80]}")
                            video_frame = frame
                            break
                    except Exception:
                        continue

                if video_frame:
                    break
                await asyncio.sleep(0.5)  # Kurzes Polling zwischen Frame-Checks

        except Exception as e:
            logger.debug(f"Frame wait error: {e}")

        if not video_frame:
            print("  ⚠ No video iframe found after 15s")
            return False
        
        print("  Trying optimized playback methods...")

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
                    print("  ✓ Video already playing! Skipping play clicks.")
                    return True
                print(f"  Video state: paused={video_state.get('paused')}, readyState={video_state.get('readyState')}")
        except Exception as e:
            logger.debug(f"Video state check failed: {e}")

        # ============================================================
        # Methode 1: Smart Play-Button Click mit Video-Start-Check
        # ============================================================
        try:
            print("  → Method 1: Smart play button detection")

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
                        print(f"  ✓ Found play button: {selector}")

                        # SMART CLICK: Max 4 Clicks mit Video-Start-Check
                        for click_num in range(1, 5):
                            try:
                                await play_button.click(timeout=2000, force=True)
                                print(f"    Click #{click_num}")

                                # Schließe Werbe-Tabs
                                closed = await self.close_new_tabs(context, page)
                                if closed > 0:
                                    print(f"      → Closed {closed} ad tab(s)")

                                # OPTIMIERT: Prüfe ob Video gestartet hat
                                try:
                                    # wait_for_function statt fester Sleep!
                                    await video_frame.wait_for_function(
                                        '() => { const v = document.querySelector("video"); return v && !v.paused && v.currentTime > 0; }',
                                        timeout=3000
                                    )
                                    print(f"  ✓ Video started playing after {click_num} click(s)!")
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
            print("  → Method 2: JavaScript play() with readiness check")

            # Warte bis Video bereit ist (readyState >= 2)
            try:
                await video_frame.wait_for_function(
                    '() => { const v = document.querySelector("video"); return v && v.readyState >= 2; }',
                    timeout=5000
                )
                print("    Video is ready (readyState >= 2)")
            except Exception:
                print("    Video not fully ready, trying play() anyway...")

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
                print("  ✓ Video.play() executed!")
                await self.close_new_tabs(context, page)

                # Kurze Wartezeit für Play-Start
                try:
                    await video_frame.wait_for_function(
                        '() => { const v = document.querySelector("video"); return v && !v.paused; }',
                        timeout=3000
                    )
                    print("  ✓ Video is now playing!")
                    return True
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"JS play failed: {e}")

        # ============================================================
        # Methode 3: Klicke direkt auf Video-Element
        # ============================================================
        try:
            print("  → Method 3: Direct video click")
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
                    print("  ✓ Video started via direct click!")
                    return True
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Video click failed: {e}")

        print("  ⚠ All play methods tried - video might need manual start")
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
        def log(msg, prefix=""):
            if self.browser_id:
                print(f"{self.browser_id} {prefix}{msg}")
            else:
                print(f"{prefix}{msg}")

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

                log(f"✓ Ad-blocking enabled ({len(self.ad_filters)} rules)")

                blocked_count = [0]

                async def block_ads_brave(route, request):
                    url_lower = request.url.lower()
                    try:
                        parsed = urlparse(request.url)
                        domain = parsed.netloc.lower()

                        if domain in self.ad_filters:
                            blocked_count[0] += 1
                            if blocked_count[0] <= 10:
                                log(f"🚫 {domain}", "  ")
                            await route.abort()
                            return
                        
                        domain_parts = domain.split('.')
                        if len(domain_parts) > 2:
                            parent_domain = '.'.join(domain_parts[-2:])
                            if parent_domain in self.ad_filters:
                                blocked_count[0] += 1
                                await route.abort()
                                return
                        
                        blocked_patterns = [
                            '/ads/', '/ad/', '/advert',
                            'doubleclick', 'googlesyndication',
                            'facebook.com/tr', 'facebook.net',
                            '/analytics.', '/tracking.',
                            '/banner', '/popup',
                        ]
                        
                        if any(pattern in url_lower for pattern in blocked_patterns):
                            blocked_count[0] += 1
                            await route.abort()
                            return
                        
                        if request.resource_type in ['font']:
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
                    log(f"🎯 Found m3u8: {url[:100]}...", "\n")
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
                        log(f"✓ Series (from h1): {self.metadata.series_name}")
            except Exception as e:
                log(f"Could not extract series name from h1: {e}")

            # Priorität 2: Fallback auf .hosterSeriesTitle strong
            if not self.metadata.series_name:
                try:
                    series_element = await page.query_selector('.hosterSeriesTitle strong')
                    if series_element:
                        self.metadata.series_name = await series_element.inner_text()
                        self.metadata.series_name_display = self.metadata.series_name
                        log(f"✓ Series (from hosterSeriesTitle): {self.metadata.series_name}")
                except Exception as e:
                    logger.debug(f"Could not extract series from hosterSeriesTitle: {e}")

            try:
                season_meta = await page.query_selector('meta[itemprop="seasonNumber"]')
                if season_meta:
                    self.metadata.season = await season_meta.get_attribute('content')
                    log(f"✓ Season: {self.metadata.season}")
            except Exception as e:
                logger.debug(f"Could not extract season metadata: {e}")

            try:
                episode_meta = await page.query_selector('meta[itemprop="episode"]')
                if episode_meta:
                    self.metadata.episode = await episode_meta.get_attribute('content')
                    log(f"✓ Episode: {self.metadata.episode}")
            except Exception as e:
                logger.debug(f"Could not extract episode metadata: {e}")

            try:
                german_title = await page.query_selector('.episodeGermanTitle')
                if german_title:
                    self.metadata.episode_title_german = await german_title.inner_text()
                    log(f"✓ German Title: {self.metadata.episode_title_german}")
            except Exception as e:
                logger.debug(f"Could not extract German title: {e}")

            try:
                english_title = await page.query_selector('small.episodeEnglishTitle')
                if english_title:
                    self.metadata.episode_title_english = await english_title.inner_text()
                    log(f"✓ English Title: {self.metadata.episode_title_english}")
            except Exception as e:
                logger.debug(f"Could not extract English title: {e}")

            if not self.metadata.series_name:
                self._parse_url_fallback(url)

            # ============================================================
            # WICHTIG: SCROLL NACH UNTEN damit iframe sichtbar wird
            # ============================================================

            log("📜 Scrolling page to load iframe properly...", "\n")
            try:
                # Scroll mehrmals nach unten
                for i in range(3):
                    await page.evaluate("window.scrollBy(0, 300)")
                    await asyncio.sleep(0.5)
                log("✓ Scrolled down", "  ")
            except Exception as e:
                log(f"⚠ Scroll failed: {e}", "  ")

            # ============================================================
            # LANGUAGE SELECTION (if specified)
            # ============================================================

            log(f"🌐 Language parameter received: '{language}'", "\n")

            if language:
                log(f"🌐 Selecting language: {language}", "\n")
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
                    log(f"  Available languages: {available_langs}", "  ")

                    # Find the language image with the specified data-lang-key
                    lang_selector = f'.changeLanguageBox img[data-lang-key="{language}"]'

                    # First check if language is already selected
                    is_already_selected = await page.evaluate(f'''() => {{
                        const img = document.querySelector('.changeLanguageBox img[data-lang-key="{language}"]');
                        return img ? img.classList.contains("selectedLanguage") : false;
                    }}''')

                    if is_already_selected:
                        log(f"✓ Language {language} already selected", "  ")
                    else:
                        # FIRST: Remove ad overlays that block clicks
                        log(f"  Removing ad overlays...", "  ")
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
                        log(f"  Current iframe: {iframe_before[:60] if iframe_before else 'none'}...", "  ")

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
                                log(f"  Language element not found on attempt {attempt + 1}", "  ")
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
                                log(f"  Clicked language flag via JS (attempt {attempt + 1})", "  ")
                            except Exception as click_err:
                                log(f"  Click failed: {click_err}", "  ")
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
                                    log(f"  Closed {closed_count} popup tab(s)", "  ")

                            # Check if language flag is now selected
                            is_now_selected = await page.evaluate(f'''() => {{
                                const img = document.querySelector('.changeLanguageBox img[data-lang-key="{language}"]');
                                return img ? img.classList.contains("selectedLanguage") : false;
                            }}''')

                            if is_now_selected:
                                log(f"✓ Language flag {language} now selected", "  ")
                                language_flag_changed = True
                                break

                            if attempt < max_attempts - 1:
                                log(f"  Language flag not yet selected, retrying...", "  ")
                                await asyncio.sleep(1.0)

                        if not language_flag_changed:
                            log(f"⚠ Could not select language flag after {max_attempts} attempts", "  ")

                        # After clicking language flag, the iframe automatically loads the new video
                        # Wait for the new iframe content to load
                        log(f"⏳ Waiting for new language content to load...", "  ")
                        await asyncio.sleep(3.0)

                        # Verify iframe changed
                        iframe_after = await page.evaluate('''() => {
                            const iframe = document.querySelector('.inSiteWebStream iframe');
                            return iframe ? iframe.src : null;
                        }''')
                        if iframe_after and iframe_before and iframe_after != iframe_before:
                            log(f"✓ Iframe updated to: {iframe_after[:60]}...", "  ")
                        else:
                            log(f"⚠ Iframe may not have changed yet", "  ")

                except Exception as e:
                    log(f"⚠ Language selection failed: {e}", "  ")
                    import traceback
                    traceback.print_exc()
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
                            log(f"  Available languages: {available}", "  ")
                    except Exception as e:
                        logger.debug(f"Could not list available languages: {e}")

            # ============================================================
            # 18+ POPUPS WEGKLICKEN (falls vorhanden)
            # Note: Button Debugger zeigt keine Popups auf dieser Seite!
            # ============================================================

            await asyncio.sleep(2)
            popup_count = await self.click_18_plus_popups(page, context, "initial")
            
            if popup_count == 0:
                print("  → No popups found (as expected for this site)")
            
            # ============================================================
            # PLAY BUTTON KLICKEN (role='button' mit "Spielen" Text!)
            # ============================================================
            
            play_clicked = await self.click_play_button(page, context)
            
            if not play_clicked:
                print("  ⚠ Play button not clicked - video might auto-play or need manual start")
            
            # ============================================================
            # WARTE AUF M3U8 (Event-basiert - SOFORTIGE Erkennung!)
            # ============================================================

            log(f"⏳ Waiting up to {wait_time}s for m3u8 (event-based)...", "\n")

            start_time = asyncio.get_event_loop().time()

            try:
                # Event-basiertes Warten - reagiert SOFORT wenn m3u8 gefunden!
                # Kein 2-Sekunden-Polling mehr nötig!
                await asyncio.wait_for(self._m3u8_event.wait(), timeout=wait_time)
                elapsed = asyncio.get_event_loop().time() - start_time
                log(f"✅ SUCCESS! m3u8 found after {elapsed:.1f} seconds! (event-based)", "\n")
                log("🔒 Closing browser to free resources...")
            except asyncio.TimeoutError:
                log(f"❌ No m3u8 found after {wait_time}s", "\n")
                log("Try:", "   ")
                log("1. Increase --wait to 70 or 80", "   ")
                log("2. Check if video started playing in browser", "   ")
                log("3. Try without --no-adblock", "   ")

            if use_adblock and blocked_count[0] > 10:
                log(f"🚫 Total blocked: {blocked_count[0]} requests", "\n")

            # Browser schließen (jetzt auch wenn m3u8 gefunden wurde)
            await browser.close()
            if self.m3u8_urls:
                log("✓ Browser closed, resources freed")
            
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
            print(f"✓ Fallback URL parsing successful")

def parse_episode_range(episodes_str: str) -> List[int]:
    """
    Parse episode range string into list of episode numbers.

    Args:
        episodes_str: String like "1-5,8,10-12"

    Returns:
        Sorted list of episode numbers
    """
    episodes = set()

    for part in episodes_str.split(','):
        part = part.strip()
        if '-' in part:
            start, end = map(int, part.split('-'))
            episodes.update(range(start, end + 1))
        else:
            episodes.add(int(part))

    return sorted(episodes)

async def detect_series_info(url: str) -> Tuple[Optional[int], Optional[List[int]]]:
    """
    Detect number of seasons and episodes from series page.

    Args:
        url: URL of the series page

    Returns:
        Tuple of (total_seasons, list_of_episodes) or (None, None) on error
    """
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--mute-audio']
            )
            page = await browser.new_page()

            await page.goto(url, wait_until="domcontentloaded")

            # Get number of seasons
            total_seasons = None
            try:
                seasons_meta = await page.query_selector('meta[itemprop="numberOfSeasons"]')
                if seasons_meta:
                    total_seasons = int(await seasons_meta.get_attribute('content'))
            except Exception as e:
                logger.debug(f"Could not extract number of seasons: {e}")

            # Get episodes for current season (if on season page)
            episodes = []
            try:
                # Find all episode links
                episode_links = await page.query_selector_all('a[href*="/episode-"]')
                for link in episode_links:
                    href = await link.get_attribute('href')
                    if href:
                        match = re.search(r'/episode-(\d+)', href)
                        if match:
                            episodes.append(int(match.group(1)))
            except Exception as e:
                logger.debug(f"Could not extract episode list: {e}")

            await browser.close()

            return total_seasons, sorted(set(episodes)) if episodes else None

    except Exception as e:
        print(f"⚠️  Could not auto-detect series info: {e}")
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

    print(f"\nDownloading...")
    print(f"  From: {m3u8_url[:80]}...")
    print(f"  To: {output_path.name}")
    print(f"  Quality: {quality}")

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
        print(f"\n✓ Download complete: {output_path.name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ yt-dlp failed: {e}")
        print("\nTrying with ffmpeg...")
        try:
            ffmpeg_command = [
                "ffmpeg",
                "-i", m3u8_url,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                str(output_path),
                "-loglevel", "warning",
                "-stats"
            ]
            subprocess.run(ffmpeg_command, check=True)
            print(f"\n✓ Download complete: {output_path.name}")
            return True
        except subprocess.CalledProcessError:
            print("✗ ffmpeg also failed")
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
        print(f"\n{browser_id} {'='*60}")
        print(f"{browser_id} Processing: {url}")
        print(f"{browser_id} {'='*60}\n")
    else:
        print(f"\n{'='*70}")
        print(f"Processing: {url}")
        print(f"{'='*70}\n")

    m3u8_urls = await extractor.extract_metadata_and_m3u8(
        url,
        wait_time=args.wait,
        use_adblock=not args.no_adblock,
        browser_id=browser_id
    )
    
    if not m3u8_urls:
        print(f"\n✗ No m3u8 found for {url}")
        return False
    
    print(f"\nExtracted: S{extractor.metadata.season}E{extractor.metadata.episode}", end="")
    if extractor.metadata.episode_title_english:
        print(f" - {extractor.metadata.episode_title_english}")
    elif extractor.metadata.episode_title_german:
        print(f" - {extractor.metadata.episode_title_german}")
    else:
        print()
    
    if args.series_display:
        extractor.metadata.series_name_display = args.series_display
    
    output_path = extractor.metadata.get_full_path(
        base_path=args.base_path,
        use_english_title=args.english_title,
        quality=args.quality_tag,
        format_ext=args.format
    )
    
    if output_path.exists() and not args.force:
        print(f"⚠ File already exists: {output_path.name}")
        print("  Skipping... (use --force to overwrite)")
        return True
    
    print(f"\nFull path: {output_path}")
    
    download_url = extractor.master_playlist if extractor.master_playlist else m3u8_urls[0]

    if not args.no_download:
        success = await download_video(download_url, output_path, args.quality)
        return success
    else:
        print("\n✓ Metadata extracted (download skipped)")
        print(f"  Manual: yt-dlp -o '{output_path}' {download_url}")
        return True

async def process_episode_with_semaphore(semaphore, ep_num, base_url, season, args, episode_idx, total_episodes, browser_num):
    """Verarbeitet eine Episode mit Semaphore (für Parallelisierung)"""
    browser_id = f"[B{browser_num}]"

    async with semaphore:
        url = f"{base_url}/staffel-{season}/episode-{ep_num}"

        print(f"\n{browser_id} ▶️  Starting S{season:02d}E{ep_num:02d} ({episode_idx}/{total_episodes})")

        try:
            # Jede Episode bekommt ihren eigenen Extractor
            extractor = HLSExtractor()
            result = await process_single_episode(extractor, url, args, browser_id)

            if result:
                print(f"\n{browser_id} ✅ S{season:02d}E{ep_num:02d} - SUCCESS! ({episode_idx}/{total_episodes})")
            else:
                print(f"\n{browser_id} ❌ S{season:02d}E{ep_num:02d} - FAILED! ({episode_idx}/{total_episodes})")

            return result
        except Exception as e:
            print(f"\n{browser_id} ❌ S{season:02d}E{ep_num:02d} - ERROR: {e} ({episode_idx}/{total_episodes})")
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
    
    print("="*70)
    print("HLS Video Downloader - FINAL VERSION")
    print("Handles 18+ popups and play button automatically")
    if args.parallel > 1:
        print(f"⚡ PARALLEL MODE: {args.parallel} concurrent windows")
    print("="*70 + "\n")

    # Parse URL with flexible format support
    base_url, series_slug, season, start_episode, url_type = parse_flexible_url(args.url)

    if not base_url:
        print("✗ Invalid URL format!")
        print("Supported formats:")
        print("  1. http://site/serie/stream/NAME/staffel-N/episode-N")
        print("  2. http://site/serie/stream/NAME/staffel-N")
        print("  3. http://site/serie/stream/NAME")
        sys.exit(1)

    print(f"📝 Detected URL type: {url_type}")
    print(f"   Series: {series_slug}")

    # Handle different URL types
    if url_type == 'series':
        # No season specified - need to detect or ask
        print("\n⚠️  No season specified in URL")

        if args.season:
            season = args.season
            print(f"✓ Using --season argument: Season {season}")
        else:
            # Auto-detect available seasons
            print("🔍 Auto-detecting available seasons...")
            total_seasons, _ = await detect_series_info(args.url)

            if total_seasons:
                print(f"✓ Found {total_seasons} season(s)")
                if total_seasons == 1:
                    season = 1
                    print(f"✓ Auto-selected: Season 1")
                else:
                    print(f"\nAvailable seasons: 1-{total_seasons}")
                    print("Please specify season with --season N")
                    sys.exit(1)
            else:
                print("❌ Could not auto-detect seasons")
                print("Please specify season with --season N")
                sys.exit(1)

    elif url_type == 'season':
        # Season specified, but no episode
        print(f"   Season: {season}")

        if not args.episodes:
            # Auto-detect available episodes
            print(f"\n🔍 Auto-detecting episodes for Season {season}...")
            season_url = f"{base_url}/staffel-{season}"
            _, available_episodes = await detect_series_info(season_url)

            if available_episodes:
                print(f"✓ Found {len(available_episodes)} episode(s): {available_episodes[0]}-{available_episodes[-1]}")
                print(f"💡 Tip: Use --episodes {available_episodes[0]}-{available_episodes[-1]} to download all")

                # Use first episode as start
                start_episode = available_episodes[0]
                print(f"✓ Using first episode: E{start_episode:02d}")
            else:
                print("❌ Could not auto-detect episodes")
                start_episode = 1
                print("⚠️  Defaulting to Episode 1")

    else:  # url_type == 'full'
        print(f"   Season: {season}")
        print(f"   Episode: {start_episode}")

    # Override season if specified
    if args.season:
        season = args.season
        print(f"✓ Season overridden to: {season}")
    
    if args.episodes:
        episodes = parse_episode_range(args.episodes)
        print(f"Batch mode: {len(episodes)} episode(s) from Season {season}")
        print(f"Episodes: {episodes}")
        if args.parallel > 1:
            print(f"Processing {args.parallel} episodes in parallel\n")
        else:
            print()
    else:
        episodes = [start_episode]
        print(f"Single mode: S{season:02d}E{start_episode:02d}\n")
    
    successful = 0
    failed = 0
    total = len(episodes)
    
    # ============================================================
    # PARALLEL vs SEQUENTIAL PROCESSING
    # ============================================================
    
    if args.parallel > 1 and len(episodes) > 1:
        # PARALLEL MODE (2-4 gleichzeitige Browser)
        print(f"🚀 Starting parallel processing with {args.parallel} windows...")
        print(f"⏱️  This will be ~{args.parallel}x faster!\n")
        
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
            print("📝 Sequential processing (use --parallel 2-4 for faster processing)\n")
        
        extractor = HLSExtractor()
        
        for i, ep_num in enumerate(episodes, 1):
            url = f"{base_url}/staffel-{season}/episode-{ep_num}"
            
            print(f"\n[Episode {i}/{total}]")
            
            try:
                result = await process_single_episode(extractor, url, args)
                if result:
                    successful += 1
                else:
                    failed += 1
            except KeyboardInterrupt:
                print("\n\n✗ Aborted")
                break
            except Exception as e:
                print(f"\n✗ Error: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
            
            # Pause zwischen Episoden (nur im Sequential Mode)
            if len(episodes) > 1 and ep_num != episodes[-1]:
                print(f"\n⏳ Waiting 5s before next episode...")
                await asyncio.sleep(5)
    
    print(f"\n{'='*70}")
    print("Summary:")
    print(f"  ✓ Successful: {successful}")
    print(f"  ✗ Failed: {failed}")
    print(f"  📊 Total: {total}")
    print(f"{'='*70}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n✗ Aborted")
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        sys.exit(1)
