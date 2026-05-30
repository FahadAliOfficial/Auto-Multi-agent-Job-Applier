"""
Indeed Login Page Object.

Handles:
- Navigating to Indeed login
- Filling email/password
- Detecting and waiting for manual CAPTCHA/2FA
- Saving session state after successful login
- Detecting existing logged-in sessions
"""
from __future__ import annotations

import asyncio
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from src.utils.logger import logger, console
from src.utils.delay import between_actions, type_like_human


INDEED_BASE = "https://www.indeed.com"
INDEED_LOGIN_URL = "https://secure.indeed.com/account/login"
INDEED_HOME_URL = "https://www.indeed.com/"


class LoginPage:
    """Page object for Indeed login flow."""

    def __init__(self, page: Page):
        self.page = page

    async def is_logged_in(self) -> bool:
        """Check if user is already logged in by looking for account indicators."""
        try:
            await self.page.goto(INDEED_HOME_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

            # Indeed shows a user menu or account icon when logged in
            # Check multiple selectors that indicate a logged-in state
            logged_in_selectors = [
                '[data-gnav-element-name="AccountMenu"]',
                '#AccountMenu',
                'a[href*="/account"]',
                '[data-testid="gnav-AccountMenu"]',
                '#userOptionsLabel',
            ]
            for selector in logged_in_selectors:
                try:
                    el = self.page.locator(selector)
                    if await el.count() > 0:
                        logger.info("✅ Already logged in to Indeed")
                        return True
                except Exception:
                    continue

            return False

        except Exception as e:
            logger.warning(f"Could not check login status: {e}")
            return False

    async def login(self, email: str, password: str = "") -> bool:
        """Log in to Indeed.

        If password is empty, opens login page and waits for manual login.
        This is the recommended approach to avoid storing passwords and handle
        Google/Apple SSO, CAPTCHA, and 2FA.

        Args:
            email: Indeed account email
            password: Password (optional — if empty, waits for manual login)

        Returns:
            True if login was successful
        """
        logger.info("🔑 Starting Indeed login...")

        # Navigate to login page
        await self.page.goto(INDEED_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
        await between_actions()

        if not password:
            # Manual login mode — recommended
            return await self._manual_login(email)
        else:
            return await self._auto_login(email, password)

    async def _manual_login(self, email: str) -> bool:
        """Wait for the user to manually complete login.

        Pre-fills the email field, then waits for the user to complete
        password/2FA/CAPTCHA manually in the browser.
        """
        console.print(
            "\n[bold yellow]📋 Manual Login Required[/bold yellow]\n"
            "The browser has opened the Indeed login page.\n"
            "Please complete the login process manually:\n"
            "  1. Enter your password\n"
            "  2. Complete any CAPTCHA or 2FA\n"
            "  3. Wait until you see the Indeed homepage\n",
        )

        # Try to pre-fill the email field
        try:
            email_input = self.page.locator('input[type="email"], input[name="__email"]')
            if await email_input.count() > 0:
                await email_input.first.fill("")
                await type_like_human(self.page, email_input.first, email)
                logger.info(f"Pre-filled email: {email}")
        except Exception:
            pass

        # Wait for the user to complete login (up to 5 minutes)
        console.print("[dim]Waiting for you to complete login (5 min timeout)...[/dim]")
        return await self._wait_for_login_complete(timeout_seconds=300)

    async def _auto_login(self, email: str, password: str) -> bool:
        """Attempt automatic login with email and password.

        Falls back to manual login if CAPTCHA or other challenges appear.
        """
        try:
            # Fill email
            email_input = self.page.locator('input[type="email"], input[name="__email"]')
            await email_input.first.wait_for(state="visible", timeout=10000)
            await type_like_human(self.page, email_input.first, email)
            await between_actions()

            # Look for continue/next button after email
            continue_btn = self.page.locator(
                'button:has-text("Continue"), button:has-text("Next"), '
                'button[type="submit"]'
            )
            if await continue_btn.count() > 0:
                await continue_btn.first.click()
                await between_actions()

            # Fill password
            password_input = self.page.locator('input[type="password"]')
            await password_input.first.wait_for(state="visible", timeout=10000)
            await type_like_human(self.page, password_input.first, password)
            await between_actions()

            # Click sign in button
            sign_in_btn = self.page.locator(
                'button:has-text("Sign in"), button:has-text("Log in"), '
                'button[type="submit"]'
            )
            if await sign_in_btn.count() > 0:
                await sign_in_btn.first.click()

            # Wait for login to complete
            return await self._wait_for_login_complete(timeout_seconds=60)

        except (PlaywrightTimeout, Exception) as e:
            logger.warning(f"Auto-login encountered issue: {e}")
            console.print(
                "[yellow]Auto-login needs help. Please complete login manually.[/yellow]"
            )
            return await self._wait_for_login_complete(timeout_seconds=300)

    async def _wait_for_login_complete(self, timeout_seconds: int = 300) -> bool:
        """Poll until the user is logged in or timeout is reached."""
        start = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start) < timeout_seconds:
            current_url = self.page.url

            # Successfully landed on homepage or job search
            if any(
                indicator in current_url
                for indicator in [
                    "indeed.com/?",
                    "indeed.com/jobs",
                    "indeed.com/my",
                    "indeed.com/#",
                ]
            ) or current_url.rstrip("/") == "https://www.indeed.com":
                # Double-check with page content
                try:
                    account_menu = self.page.locator(
                        '[data-gnav-element-name="AccountMenu"], #AccountMenu, '
                        '#userOptionsLabel'
                    )
                    if await account_menu.count() > 0:
                        logger.info("✅ Login successful!")
                        return True
                except Exception:
                    pass

                # Even without the menu, if we're on the homepage, likely logged in
                if "secure.indeed.com" not in current_url:
                    logger.info("✅ Login appears successful (on homepage)")
                    return True

            await asyncio.sleep(2)

        logger.error("❌ Login timed out")
        return False
