"""
Browser stealth helpers — patches the most common headless detection vectors.

These do NOT bypass Turnstile by themselves. Cloudflare's reputation system
weighs IP, behaviour, and fingerprint together; a clean fingerprint just
keeps that pillar from dragging the score down. Pair with VPN rotation
(SurfsharkVPN) and persistent browser sessions for full coverage.

Usage:
    from app.stealth import STEALTH_INIT_JS
    await context.add_init_script(STEALTH_INIT_JS)
"""

# Patches applied on every page in the context BEFORE any site JS runs:
#  1. navigator.webdriver — already covered by --disable-blink-features=AutomationControlled
#     but we set it explicitly as belt-and-suspenders
#  2. navigator.plugins — vanilla headless reports 0 (huge red flag for fingerprinters)
#  3. navigator.languages — keep it consistent with Accept-Language
#  4. window.chrome — vanilla headless lacks the runtime sub-object
#  5. WebGL vendor strings — leak "Google SwiftShader" in headless; fake Intel
#  6. Permissions API — keep notification-permission consistent with state
STEALTH_INIT_JS = r"""
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });

    const fakePlugin = (name) => {
      const p = { name, filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 };
      p[0] = { type: 'application/pdf', suffixes: 'pdf', description: '', enabledPlugin: p };
      return p;
    };
    const plugins = [
      fakePlugin('PDF Viewer'),
      fakePlugin('Chrome PDF Viewer'),
      fakePlugin('Chromium PDF Viewer'),
      fakePlugin('Microsoft Edge PDF Viewer'),
      fakePlugin('WebKit built-in PDF'),
    ];
    Object.defineProperty(navigator, 'plugins', { get: () => plugins, configurable: true });
    Object.defineProperty(navigator, 'mimeTypes', {
      get: () => [{ type: 'application/pdf', suffixes: 'pdf', description: '', enabledPlugin: plugins[0] }],
      configurable: true,
    });

    Object.defineProperty(navigator, 'languages', {
      get: () => ['de-DE', 'de', 'en-US', 'en'],
      configurable: true,
    });

    if (typeof window.chrome === 'undefined' || !window.chrome.runtime) {
      window.chrome = {
        runtime: { OnInstalledReason: {}, OnRestartRequiredReason: {}, PlatformArch: {}, PlatformOs: {} },
        loadTimes: () => ({ firstPaintTime: 0, firstPaintAfterLoadTime: 0 }),
        csi: () => ({ pageT: 0, startE: 0, tran: 0 }),
        app: { isInstalled: false },
      };
    }

    if (navigator.permissions && navigator.permissions.query) {
      const orig = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = (params) => (
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : orig(params)
      );
    }

    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (name) {
      if (name === 37445) return 'Intel Inc.';
      if (name === 37446) return 'Intel Iris OpenGL Engine';
      return getParam.call(this, name);
    };
  } catch (_) { /* never break the page */ }
})();
"""


def is_turnstile_active(page) -> bool:
    """True if a Cloudflare Turnstile challenge frame is present in the page.

    Detection signal: any frame whose URL contains 'challenges.cloudflare.com'.
    Caller can use this to trigger VPN rotation via SurfsharkVPN.rotate().
    """
    try:
        return any("challenges.cloudflare.com" in (f.url or "") for f in page.frames)
    except Exception:
        return False
