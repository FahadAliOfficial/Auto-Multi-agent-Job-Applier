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

from src.utils.logger import logger, console
from src.utils.delay import between_actions


class JobPage:
    """Page object for an individual Indeed job listing."""

    def __init__(self, page: Page):
        self.page = page

    async def open(self, url: str) -> bool:
        """Navigate to a job listing page.

        Args:
            url: Full Indeed job URL

        Returns:
            True if page loaded successfully
        """
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await between_actions()

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

        console.print("\n" + "=" * 60)
        console.print(f"[bold cyan]📌 {title}[/bold cyan]")
        console.print(f"[bold]{company}[/bold] — {location}")
        if salary:
            console.print(f"[green]💰 {salary}[/green]")
        console.print("-" * 60)
        console.print(f"[dim]{desc_preview}[/dim]")
        console.print("=" * 60)

        return {
            "title": title,
            "company": company,
            "location": location,
            "salary": salary,
            "description": description,
        }
