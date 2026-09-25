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
from urllib.parse import urlparse
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from src.control_center import REGISTRY
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

    async def login(self, email: str, password: str = "", agent_id: str = "") -> bool:
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
            return await self._manual_login(email, agent_id=agent_id)
        else:
            return await self._auto_login(email, password, agent_id=agent_id)

    async def _manual_login(self, email: str, agent_id: str = "") -> bool:
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
        if agent_id:
            console.print(
                "[bold cyan]After login, click Continue for this agent in the "
                "Control Center to resume immediately.[/bold cyan]"
            )
        console.print("[dim]Waiting for you to complete login (5 min timeout)...[/dim]")
        return await self._wait_for_login_complete(300, agent_id=agent_id)

    async def _auto_login(self, email: str, password: str, agent_id: str = "") -> bool:
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
            return await self._wait_for_login_complete(60, agent_id=agent_id)

        except (PlaywrightTimeout, Exception) as e:
            logger.warning(f"Auto-login encountered issue: {e}")
            console.print(
                "[yellow]Auto-login needs help. Please complete login manually.[/yellow]"
            )
            return await self._wait_for_login_complete(300, agent_id=agent_id)

    @staticmethod
    def _can_manually_resume(url: str) -> bool:
        """Return True when the tab has left Indeed authentication screens."""
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            location = f"{parsed.path}?{parsed.query}".lower()
        except Exception:
            return False
        if host != "indeed.com" and not host.endswith(".indeed.com"):
            return False
        blocked = ("login", "signin", "auth", "challenge", "captcha", "verify")
        return not any(marker in location for marker in blocked)

    async def _wait_for_login_complete(
        self,
        timeout_seconds: int = 300,
        agent_id: str = "",
    ) -> bool:
        """Poll until login is detected or the user manually resumes it."""
        start = asyncio.get_event_loop().time()
        if agent_id:
            REGISTRY.set_state(agent_id, "login_wait")
            REGISTRY.set_prompt(
                agent_id,
                "Complete the Indeed login/CAPTCHA in the automation tab, then click Continue.",
                options=["Continue", "Cancel"],
            )
            REGISTRY.append_log(agent_id, "waiting for manual Indeed login")

        try:
            while (asyncio.get_event_loop().time() - start) < timeout_seconds:
                # Extension pages cache their URL until a bridge operation
                # completes. This lightweight query refreshes it after manual
                # redirects without adding a fixed page-load delay.
                try:
                    await self.page.locator("body").count()
                except Exception:
                    pass
                current_url = self.page.url

                if any(
                    indicator in current_url
                    for indicator in [
                        "indeed.com/?",
                        "indeed.com/jobs",
                        "indeed.com/my",
                        "indeed.com/#",
                    ]
                ) or current_url.rstrip("/") == "https://www.indeed.com":
                    try:
                        account_menu = self.page.locator(
                            '[data-gnav-element-name="AccountMenu"], #AccountMenu, '
                            '#userOptionsLabel'
                        )
                        if await account_menu.count() > 0:
                            logger.info("Login successful")
                            if agent_id:
                                REGISTRY.append_log(agent_id, "login detected automatically")
                            return True
                    except Exception:
                        pass

                    if "secure.indeed.com" not in current_url:
                        logger.info("Login appears successful (on homepage)")
                        if agent_id:
                            REGISTRY.append_log(agent_id, "login detected automatically")
                        return True

                if agent_id:
                    if REGISTRY.is_stopped(agent_id):
                        logger.info("Login wait stopped from Control Center")
                        return False

                    if REGISTRY.consume_focus_flag(agent_id):
                        try:
                            await self.page.bring_to_front()
                            REGISTRY.append_log(agent_id, "brought login tab to front")
                        except Exception as exc:
                            REGISTRY.append_log(agent_id, f"focus failed during login: {exc}")

                    answer = REGISTRY.consume_answer(agent_id)
                    normalized = (answer or "").strip().lower()
                    if normalized in {"cancel", "__skip_job__"}:
                        logger.info("Login cancelled from Control Center")
                        REGISTRY.append_log(agent_id, "login cancelled")
                        return False
                    if normalized in {"continue", "resume", "done", "ok", "yes", "y"}:
                        if self._can_manually_resume(current_url):
                            logger.info("Login manually confirmed from Control Center")
                            REGISTRY.append_log(agent_id, "login manually confirmed; resuming bot")
                            return True
                        REGISTRY.append_log(
                            agent_id,
                            "Continue ignored: finish login and leave the login/verification page first",
                        )
                        REGISTRY.set_prompt(
                            agent_id,
                            "Login is still open. Finish login/CAPTCHA, then click Continue again.",
                            options=["Continue", "Cancel"],
                        )

                await asyncio.sleep(1)

            logger.error("Login timed out")
            if agent_id:
                REGISTRY.append_log(agent_id, "login timed out")
            return False
        finally:
            if agent_id:
                REGISTRY.clear_prompt(agent_id)
