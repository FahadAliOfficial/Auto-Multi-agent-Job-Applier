"""
Indeed Job Detail Page Object.

Handles:
- Navigating to individual job listings
- Extracting full job description
- Detecting "Easy Apply" / "Apply now" button
- Displaying job summary for user review (semi-auto mode)
"""
from __future__ import annotations

import asyncio
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from src.control_center import REGISTRY
from src.utils.logger import cc_print, console, logger
from src.utils.delay import between_actions


class JobPage:
    """Page object for an individual Indeed job listing."""

    def __init__(self, page: Page):
        self.page = page

    async def open(self, url: str, agent_id: str = "") -> bool:
        """Navigate to a job listing page.

        Args:
            url: Full Indeed job URL

        Returns:
            True if page loaded successfully
        """
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await between_actions()

            if await self._captcha_present():
                solved = await self._wait_for_captcha(agent_id)
                if not solved:
                    return False

            # Wait for the job title to appear
            await self.page.wait_for_selector(
                '.jobsearch-JobInfoHeader-title, '
                'h1[data-testid="jobTitle"], '
                'h2.jobsearch-JobInfoHeader-title, '
                '.jobTitle',
                timeout=10000,
            )
            return True

        except PlaywrightTimeout:
            logger.warning(f"Job page took too long to load: {url}")
            return False
        except Exception as e:
            logger.error(f"Failed to open job page: {e}")
            return False

    async def _wait_for_captcha(self, agent_id: str) -> bool:
        """Notify user about CAPTCHA and wait until it clears."""
        agent_id = (agent_id or "").strip()
        if agent_id:
            REGISTRY.set_state(agent_id, "captcha_wait")
            REGISTRY.set_captcha_wait_count()
            REGISTRY.set_prompt(
                agent_id,
                "Captcha/verification detected. Solve in the browser, then type ok to continue.",
                options=["ok"],
            )
            REGISTRY.append_log(agent_id, "captcha detected; waiting for manual solve")
        cc_print(
            "\n[bold yellow]CAPTCHA detected.[/bold yellow] "
            "Solve it in the browser, then confirm to continue."
        )

        # Auto-resume if captcha clears by itself; otherwise wait for manual confirmation.
        for _ in range(8):
            if not await self._captcha_present():
                if agent_id:
                    REGISTRY.clear_prompt(agent_id)
                break
            await asyncio.sleep(1)

        if await self._captcha_present():
            if agent_id:
                while True:
                    if REGISTRY.is_stopped(agent_id):
                        REGISTRY.clear_prompt(agent_id)
                        REGISTRY.set_captcha_wait_count()
                        return False
                    if REGISTRY.is_skip_requested(agent_id):
                        REGISTRY.clear_prompt(agent_id)
                        REGISTRY.set_captcha_wait_count()
                        return False
                    answer = REGISTRY.consume_answer(agent_id)
                    if answer is not None:
                        REGISTRY.clear_prompt(agent_id)
                        if answer.strip().lower() in {"__skip_job__", "skip"}:
                            REGISTRY.set_captcha_wait_count()
                            return False
                        break
                    await asyncio.sleep(0.5)
            else:
                await asyncio.to_thread(input, "Press Enter after solving CAPTCHA...")

        cleared = await self._wait_for_captcha_clear()
        if agent_id:
            REGISTRY.set_state(agent_id, "applying")
            REGISTRY.set_captcha_wait_count()
        return cleared

    async def _wait_for_captcha_clear(self, timeout: int = 120) -> bool:
        """Wait until the verification page is gone or a timeout occurs."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not await self._captcha_present():
                return True
            await asyncio.sleep(1)
        return False

    async def _captcha_present(self) -> bool:
        """Detect Cloudflare/recaptcha verification pages."""
        # If core job content is visible, this is not a captcha wall.
        job_content_selectors = [
            'h1[data-testid="jobTitle"]',
            '.jobsearch-JobInfoHeader-title',
            'button:has-text("Apply with Indeed")',
            'button:has-text("Apply now")',
            '#indeedApplyButton',
        ]
        for selector in job_content_selectors:
            try:
                el = self.page.locator(selector)
                if await el.count() > 0 and await el.first.is_visible():
                    return False
            except Exception:
                continue

        text_markers = [
            "additional verification required",
            "checking your browser",
            "cloudflare",
            "verify you are human",
            "i'm not a robot",
            "i’m not a robot",
        ]

        try:
            body_text = await self.page.locator("body").inner_text(timeout=2000)
            lower_text = body_text.lower()
            if any(marker in lower_text for marker in text_markers):
                return True
        except Exception:
            pass

        selectors = [
            'iframe[src*="recaptcha"]',
            '.g-recaptcha',
            '[title*="reCAPTCHA"]',
            'text="Additional Verification Required"',
            'text="Checking your browser"',
        ]

        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
                for idx in range(count):
                    if await locator.nth(idx).is_visible():
                        return True
            except Exception:
                continue

        return False

    async def get_title(self) -> str:
        """Extract job title."""
        selectors = [
            'h1[data-testid="jobTitle"]',
            '.jobsearch-JobInfoHeader-title',
            'h2.jobsearch-JobInfoHeader-title',
            '.jobTitle',
        ]
        for sel in selectors:
            el = self.page.locator(sel)
            if await el.count() > 0:
                return (await el.first.inner_text()).strip()
        return ""

    async def get_company(self) -> str:
        """Extract company name."""
        selectors = [
            '[data-testid="inlineHeader-companyName"] a',
            '[data-testid="inlineHeader-companyName"]',
            '.jobsearch-CompanyInfoContainer a',
            '.jobsearch-InlineCompanyRating a',
            '.css-1saizt3',
        ]
        for sel in selectors:
            el = self.page.locator(sel)
            if await el.count() > 0:
                return (await el.first.inner_text()).strip()
        return ""

    async def get_location(self) -> str:
        """Extract job location."""
        selectors = [
            '[data-testid="inlineHeader-companyLocation"]',
            '[data-testid="job-location"]',
            '.jobsearch-CompanyInfoContainer div:last-child',
            '.css-waniwe',
        ]
        for sel in selectors:
            el = self.page.locator(sel)
            if await el.count() > 0:
                return (await el.first.inner_text()).strip()
        return ""

    async def get_salary(self) -> str:
        """Extract salary information if displayed."""
        selectors = [
            '#salaryInfoAndJobType',
            '[data-testid="attribute_snippet_testid"]',
            '.jobsearch-JobMetadataHeader-item',
            '.salary-snippet-container',
        ]
        for sel in selectors:
            el = self.page.locator(sel)
            if await el.count() > 0:
                text = (await el.first.inner_text()).strip()
                if "$" in text or "year" in text.lower() or "hour" in text.lower():
                    return text
        return ""

    async def get_job_type(self) -> str:
        """Extract job type if displayed (full-time, contract, etc.)."""
        selectors = [
            '#salaryInfoAndJobType',
            '.jobsearch-JobMetadataHeader-item',
            '[data-testid="attribute_snippet_testid"]',
        ]
        for sel in selectors:
            el = self.page.locator(sel)
            if await el.count() > 0:
                text = (await el.first.inner_text()).strip()
                if any(token in text.lower() for token in ["full-time", "part-time", "contract", "temporary", "internship"]):
                    return text
        return ""

    async def get_description(self) -> str:
        """Extract full job description text."""
        selectors = [
            '#jobDescriptionText',
            '[data-testid="jobDescriptionText"]',
            '.jobsearch-JobComponent-description',
            '.jobsearch-jobDescriptionText',
        ]
        for sel in selectors:
            el = self.page.locator(sel)
            if await el.count() > 0:
                return (await el.first.inner_text()).strip()
        return ""

    async def has_easy_apply(self) -> bool:
        """Check if the job has an Easy Apply / Indeed Apply button."""
        apply_button = self.page.locator(
            '#indeedApplyButton, '
            'button[id*="indeedApply"], '
            'button:has-text("Apply with Indeed"), '
            'button:has-text("Apply now"), '
            'button:has-text("Easy Apply"), '
            '[data-testid="indeedApplyButton"], '
            '.indeed-apply-button'
        )
        return await apply_button.count() > 0

    async def click_apply(self) -> bool:
        """Click the 'Apply now' / Easy Apply button.

        Returns:
            True if button was clicked successfully
        """
        apply_selectors = [
            '#indeedApplyButton',
            'button[id*="indeedApply"]',
            '[data-testid="indeedApplyButton"]',
            'button:has-text("Apply with Indeed")',
            'button:has-text("Apply now")',
            'button:has-text("Easy Apply")',
            '.indeed-apply-button',
        ]

        for sel in apply_selectors:
            btn = self.page.locator(sel)
            if await btn.count() > 0:
                await btn.first.click()
                logger.info("🖱️ Clicked Apply button")
                await between_actions()
                return True

        logger.warning("Could not find Apply button")
        return False

    async def should_skip(self, skip_keywords: list[str], skip_companies: list[str]) -> str | None:
        """Check if this job should be skipped based on config rules.

        Returns:
            Reason string if should skip, None if OK to apply
        """
        title = (await self.get_title()).lower()
        company = (await self.get_company()).lower()
        description = (await self.get_description()).lower()

        # Check skip keywords in title and description
        for keyword in skip_keywords:
            kw = keyword.lower()
            if kw in title or kw in description:
                return f"Contains skip keyword: '{keyword}'"

        # Check skip companies
        for skip_co in skip_companies:
            if skip_co.lower() in company:
                return f"Company on skip list: '{skip_co}'"

        return None

    async def display_summary(self) -> dict:
        """Display a rich summary of the job for user review.

        Returns:
            Dict with job details
        """
        title = await self.get_title()
        company = await self.get_company()
        location = await self.get_location()
        salary = await self.get_salary()
        description = await self.get_description()

        # Truncate description for display
        desc_preview = description[:500] + "..." if len(description) > 500 else description

        cc_print("\n" + "=" * 60)
        cc_print(f"[bold cyan]📌 {title}[/bold cyan]")
        cc_print(f"[bold]{company}[/bold] — {location}")
        if salary:
            cc_print(f"[green]💰 {salary}[/green]")
        cc_print("-" * 60)
        cc_print(f"[dim]{desc_preview}[/dim]")
        cc_print("=" * 60)

        return {
            "title": title,
            "company": company,
            "location": location,
            "salary": salary,
            "description": description,
        }
