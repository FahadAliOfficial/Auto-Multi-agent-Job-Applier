"""
Browser module for Indeed Easy Apply Bot.

Handles Playwright browser lifecycle:
- Launch with anti-detection settings
- Persistent context with session reuse
- Screenshot-on-error middleware
- Stealth configuration to minimize bot detection
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)


# ---------------------------------------------------------------------------
# Default browser settings
# ---------------------------------------------------------------------------

DEFAULT_VIEWPORT = {"width": 1366, "height": 768}

# Keep the UA up-to-date with a real, current Chrome version.
# Tip: update this every few months to match the latest stable Chrome.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SESSION_DIR = Path("data/sessions")
STORAGE_STATE_PATH = SESSION_DIR / "indeed_session.json"


class BrowserManager:
    """Manages Playwright browser with anti-detection and session persistence."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        browser_cfg = self.config.get("bot", {}).get("browser", {})
        self.headless: bool = browser_cfg.get("headless", False)
        self.slow_mo: int = browser_cfg.get("slow_mo", 50)
        self.viewport: dict = {
            "width": browser_cfg.get("viewport_width", DEFAULT_VIEWPORT["width"]),
            "height": browser_cfg.get("viewport_height", DEFAULT_VIEWPORT["height"]),
        }
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        # Ensure directories exist
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> Page:
        """Launch browser and return main page.

        Uses persistent context to preserve cookies/localStorage across runs.
        If a saved session exists, it will be loaded automatically.
        """
        self._playwright = await async_playwright().start()

        # Launch arguments for stealth — cover every major detection vector
        launch_args = [
            # Core anti-automation flags
            "--disable-blink-features=AutomationControlled",
            "--disable-automation",
            # Process / sandbox flags (required in some environments)
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            # Suppress infobars / extensions
            "--disable-infobars",
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            # Window / rendering
            f"--window-size={self.viewport['width']},{self.viewport['height']}",
            "--start-maximized",
            # GPU / graphics — prevent headless signatures
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-accelerated-2d-canvas",
            # Misc hardening
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--use-mock-keychain",
            "--lang=en-US",
            "--disable-background-networking",
            "--disable-client-side-phishing-detection",
            "--disable-sync",
            "--metrics-recording-only",
            "--mute-audio",
            "--safebrowsing-disable-auto-update",
        ]

        # Use persistent context to keep session across runs
        user_data_dir = str(SESSION_DIR / "browser_profile")
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=self.headless,
            slow_mo=self.slow_mo,
            viewport=self.viewport,
            user_agent=DEFAULT_USER_AGENT,
            args=launch_args,
            ignore_https_errors=True,
            java_script_enabled=True,
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
        )

        # Apply stealth scripts to evade detection
        await self._apply_stealth(self._context)

        # Get or create the main page
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        return self._page

    async def stop(self) -> None:
        """Close browser and cleanup."""
        if self._context:
            with suppress(Exception):
                await asyncio.wait_for(self._context.close(), timeout=5)
            self._context = None
        if self._playwright:
            with suppress(Exception):
                await asyncio.wait_for(self._playwright.stop(), timeout=5)
            self._playwright = None
        self._page = None

    @property
    def page(self) -> Page:
        """Get the active page."""
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        """Get the browser context."""
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    # ------------------------------------------------------------------
    # Session Management
    # ------------------------------------------------------------------

    async def save_session(self) -> None:
        """Save current session (cookies, localStorage) to disk."""
        if self._context:
            await self._context.storage_state(path=str(STORAGE_STATE_PATH))

    async def has_saved_session(self) -> bool:
        """Check if a saved session file exists."""
        return STORAGE_STATE_PATH.exists()

    # ------------------------------------------------------------------
    # Navigation Helpers
    # ------------------------------------------------------------------

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        """Navigate to URL with auto-waiting."""
        await self.page.goto(url, wait_until=wait_until, timeout=30000)

    async def wait_for_navigation(self, timeout: int = 15000) -> None:
        """Wait for navigation to complete."""
        await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)

    # ------------------------------------------------------------------
    # Anti-Detection (Stealth)
    # ------------------------------------------------------------------

    async def _apply_stealth(self, context: BrowserContext) -> None:
        """Inject stealth scripts to make automation less detectable.

        Covers the major fingerprinting vectors checked by Cloudflare,
        Imperva, DataDome, and similar bot-detection services:
          - navigator.webdriver
          - chrome.runtime / chrome.app
          - permissions API
          - plugins / mimeTypes
          - languages
          - WebGL vendor / renderer
          - Canvas 2D fingerprint noise
          - AudioContext fingerprint noise
          - screen / window dimensions
          - battery / connection APIs (stub)
          - WebRTC IP leak prevention
          - Object.getOwnPropertyDescriptor hardening
        """
        stealth_js = """
        () => {
            // ── 1. navigator.webdriver ──────────────────────────────────────
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true,
            });

            // ── 2. chrome.runtime ───────────────────────────────────────────
            if (!window.chrome) {
                Object.defineProperty(window, 'chrome', {
                    value: {
                        app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
                        runtime: {},
                        loadTimes: function() {},
                        csi: function() {},
                    },
                    writable: false,
                    configurable: true,
                });
            } else {
                window.chrome.runtime = window.chrome.runtime || {};
            }

            // ── 3. Permissions API ──────────────────────────────────────────
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(parameters)
            );

            // ── 4. plugins / mimeTypes ──────────────────────────────────────
            const pluginData = [
                { name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer',          description: 'Portable Document Format', mimeTypes: [{ type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' }] },
                { name: 'Chrome PDF Viewer',  filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: 'Portable Document Format', mimeTypes: [{ type: 'application/pdf',               suffixes: 'pdf', description: 'Portable Document Format' }] },
                { name: 'Native Client',      filename: 'internal-nacl-plugin',          description: 'Native Client Executable', mimeTypes: [{ type: 'application/x-nacl',           suffixes: '',    description: 'Native Client Executable' }, { type: 'application/x-pnacl', suffixes: '', description: 'Portable Native Client Executable' }] },
            ];
            const plugins = pluginData.map(p => {
                const plugin = { name: p.name, filename: p.filename, description: p.description, length: p.mimeTypes.length };
                p.mimeTypes.forEach((mt, idx) => { plugin[idx] = mt; });
                return plugin;
            });
            Object.defineProperty(navigator, 'plugins', { get: () => plugins });

            const allMimeTypes = pluginData.flatMap(p => p.mimeTypes);
            Object.defineProperty(navigator, 'mimeTypes', { get: () => allMimeTypes });

            // ── 5. languages ────────────────────────────────────────────────
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'language',  { get: () => 'en-US' });

            // ── 6. platform ─────────────────────────────────────────────────
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

            // ── 7. hardwareConcurrency / deviceMemory ───────────────────────
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });

            // ── 8. maxTouchPoints ───────────────────────────────────────────
            Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

            // ── 9. WebGL fingerprint ─────────────────────────────────────────
            const getParam = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';                  // UNMASKED_VENDOR_WEBGL
                if (parameter === 37446) return 'Intel(R) Iris(TM) Graphics 6100'; // UNMASKED_RENDERER_WEBGL
                return getParam.call(this, parameter);
            };
            // Also cover WebGL2
            if (typeof WebGL2RenderingContext !== 'undefined') {
                const getParam2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel(R) Iris(TM) Graphics 6100';
                    return getParam2.call(this, parameter);
                };
            }

            // ── 10. Canvas 2D noise ──────────────────────────────────────────
            // Add imperceptible random noise to toDataURL / getImageData outputs
            // so each browser session has a unique canvas fingerprint.
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type, ...args) {
                const ctx = this.getContext('2d');
                if (ctx) {
                    const imgData = ctx.getImageData(0, 0, this.width || 1, this.height || 1);
                    for (let i = 0; i < imgData.data.length; i += 4) {
                        imgData.data[i]   = Math.min(255, imgData.data[i]   + (Math.random() > 0.5 ? 1 : 0));
                        imgData.data[i+1] = Math.min(255, imgData.data[i+1] + (Math.random() > 0.5 ? 1 : 0));
                    }
                    ctx.putImageData(imgData, 0, 0);
                }
                return origToDataURL.call(this, type, ...args);
            };

            // ── 11. AudioContext fingerprint noise ───────────────────────────
            const origGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(...args) {
                const data = origGetChannelData.apply(this, args);
                for (let i = 0; i < data.length; i += 100) {
                    data[i] += (Math.random() - 0.5) * 0.0001;
                }
                return data;
            };

            // ── 12. Screen dimensions ────────────────────────────────────────
            Object.defineProperty(screen, 'width',      { get: () => 1920 });
            Object.defineProperty(screen, 'height',     { get: () => 1080 });
            Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
            Object.defineProperty(screen, 'availHeight',{ get: () => 1040 });
            Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
            Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });

            // ── 13. window.outerWidth / outerHeight ──────────────────────────
            if (window.outerWidth === 0)  Object.defineProperty(window, 'outerWidth',  { get: () => 1366 });
            if (window.outerHeight === 0) Object.defineProperty(window, 'outerHeight', { get: () => 768  });

            // ── 14. Battery API stub (prevent absence-based detection) ────────
            if ('getBattery' in navigator) {
                navigator.getBattery = () => Promise.resolve({
                    charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1.0,
                    addEventListener: () => {}, removeEventListener: () => {},
                });
            }

            // ── 15. Network Information API stub ─────────────────────────────
            Object.defineProperty(navigator, 'connection', {
                get: () => ({ effectiveType: '4g', downlink: 10, rtt: 50, saveData: false }),
            });

            // ── 16. WebRTC – prevent local IP leakage ───────────────────────
            if (window.RTCPeerConnection) {
                const origRTC = window.RTCPeerConnection;
                window.RTCPeerConnection = function(config, ...rest) {
                    if (config && config.iceServers) {
                        config.iceServers = [];
                    }
                    return new origRTC(config, ...rest);
                };
                Object.assign(window.RTCPeerConnection, origRTC);
            }

            // ── 17. Prevent Function.prototype.toString leakage ──────────────
            // Detectors check if overridden functions look 'native'
            const nativeToString = Function.prototype.toString;
            Function.prototype.toString = function() {
                const s = nativeToString.call(this);
                if (s.includes('native code')) return s;
                // For our patched functions, return a believable native signature
                if (['toDataURL', 'getChannelData', 'getParameter', 'getBattery'].includes(this.name)) {
                    return `function ${this.name}() { [native code] }`;
                }
                return s;
            };
        }
        """
        await context.add_init_script(stealth_js)

    # ------------------------------------------------------------------
    # Context Manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> BrowserManager:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()
