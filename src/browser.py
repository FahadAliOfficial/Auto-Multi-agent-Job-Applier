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
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
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

        # Launch arguments for stealth
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-extensions",
            f"--window-size={self.viewport['width']},{self.viewport['height']}",
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

        Overrides common detection vectors:
        - navigator.webdriver property
        - chrome.runtime presence
        - permissions query
        - plugins and languages
        """
        stealth_js = """
        () => {
            // Override navigator.webdriver
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });

            // Override chrome.runtime (absent in real browsers when no extension)
            if (!window.chrome) {
                window.chrome = {};
            }
            window.chrome.runtime = undefined;

            // Override permissions query
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );

            // Override plugins to appear non-empty
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                    { name: 'Native Client', filename: 'internal-nacl-plugin' },
                ],
            });

            // Override languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });

            // Mask the headless signature in WebGL renderer
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                // UNMASKED_VENDOR_WEBGL = 37445
                if (parameter === 37445) return 'Intel Inc.';
                // UNMASKED_RENDERER_WEBGL = 37446
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.call(this, parameter);
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
