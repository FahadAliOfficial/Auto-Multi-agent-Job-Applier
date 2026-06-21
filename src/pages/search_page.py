"""
Indeed Search Page Object.

Handles:
- Building search URLs from config
- Parsing job listings from search results
- Extracting job metadata (title, company, location, salary, Easy Apply badge)
- Pagination (next page navigation)
- Filtering out non-Easy-Apply and already-applied jobs
"""
from __future__ import annotations

import re
import asyncio
from dataclasses import dataclass
from urllib.parse import urlencode, quote_plus
from playwright.async_api import Page, Locator, TimeoutError as PlaywrightTimeout

from src.database import Job
from src.utils.logger import logger, console
from src.utils.delay import between_actions, between_pages


# Supported Indeed country domains
COUNTRY_DOMAINS: dict[str, str] = {
    "us": "https://www.indeed.com",
    "pk": "https://pk.indeed.com",
    "uk": "https://uk.indeed.com",
    "ca": "https://ca.indeed.com",
    "au": "https://au.indeed.com",
    "in": "https://www.indeed.co.in",
    "ae": "https://www.indeed.com/jobs",  # UAE uses .com with location filter
}

DEFAULT_DOMAIN = "https://www.indeed.com"

# Mapping config values to Indeed URL parameters
JOB_TYPE_MAP = {
    "fulltime": "jt=fulltime",
    "parttime": "jt=parttime",
    "contract": "jt=contract",
    "temporary": "jt=temporary",
    "internship": "jt=internship",
}

EXPERIENCE_MAP = {
    "entry_level": "explvl=entry_level",
    "mid_level": "explvl=mid_level",
    "senior_level": "explvl=senior_level",
}

DATE_POSTED_MAP = {
    "1": "fromage=1",
    "3": "fromage=3",
    "7": "fromage=7",
    "14": "fromage=14",
}


@dataclass
class JobListing:
    """A job listing parsed from Indeed search results."""
    indeed_job_id: str
    title: str
    company: str
    location: str
    salary: str
    url: str
    is_easy_apply: bool
    snippet: str = ""


class SearchPage:
    """Page object for Indeed job search."""

    def __init__(self, page: Page, config: dict, country: str = "us"):
        self.page = page
        self.config = config
        self.search_config = config.get("search", {})
        base = COUNTRY_DOMAINS.get(country.lower(), DEFAULT_DOMAIN)
        self._search_url = f"{base}/jobs"
        self._viewjob_url = f"{base}/viewjob"
        self._country = country.lower()

    def build_search_url(self, query: str, page_num: int = 0) -> str:
        """Build an Indeed search URL from the query and config filters.

        Args:
            query: Job title or keyword search
            page_num: Page number (0-indexed, each page has 10 results)

        Returns:
            Full Indeed search URL with all filters applied
        """
        params = {
            "q": query,
            "l": self.search_config.get("location", ""),
            "radius": str(self.search_config.get("radius", 25)),
        }

        # Page offset (Indeed uses &start=10, 20, 30...)
        if page_num > 0:
            params["start"] = str(page_num * 10)

        # Build URL
        url = f"{self._search_url}?{urlencode(params)}"

        # Add optional filters
        job_type = self.search_config.get("job_type", "")
        if job_type and job_type in JOB_TYPE_MAP:
            url += f"&{JOB_TYPE_MAP[job_type]}"

        experience = self.search_config.get("experience_level", "")
        if experience and experience in EXPERIENCE_MAP:
            url += f"&{EXPERIENCE_MAP[experience]}"

        date_posted = str(self.search_config.get("date_posted", ""))
        if date_posted and date_posted in DATE_POSTED_MAP:
            url += f"&{DATE_POSTED_MAP[date_posted]}"

        # Salary filter
        salary_min = self.search_config.get("salary_min", 0)
        if salary_min and salary_min > 0:
            url += f"&salary={salary_min}"

        # Easy Apply filter
        if self.search_config.get("easy_apply_only", True):
            url += "&sc=0kf%3Aattr(DSQF7)%3B"  # Indeed's "Easily apply" filter param

        return url

    async def search(self, query: str, page_num: int = 0) -> list[JobListing]:
        """Navigate to search results and parse job listings.

        Args:
            query: Search query string
            page_num: Page number to navigate to

        Returns:
            List of JobListing objects found on the page
        """
        url = self.build_search_url(query, page_num)
        logger.info(f"🔍 Searching: '{query}' — Page {page_num + 1}")

        await self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await between_pages()

        # Wait for job cards to load
        try:
            await self.page.wait_for_selector(
                '.job_seen_beacon, .jobsearch-ResultsList > li, [data-testid="jobListing"]',
                timeout=10000,
            )
        except PlaywrightTimeout:
            logger.warning("No job listings found on page — may be empty or blocked")
            return []

        return await self._parse_listings()

    async def _parse_listings(self) -> list[JobListing]:
        """Parse all job listings from the current search results page."""
        listings: list[JobListing] = []

        # Indeed uses multiple possible selectors for job cards
        job_cards = self.page.locator(
            '.job_seen_beacon, '
            '.jobsearch-ResultsList > li[data-resulttype="job"], '
            '[data-testid="jobListing"]'
        )

        count = await job_cards.count()
        logger.info(f"📋 Found {count} job cards on page")

        for i in range(count):
            try:
                card = job_cards.nth(i)
                listing = await self._parse_single_card(card)
                if listing:
                    listings.append(listing)
            except Exception as e:
                logger.debug(f"Failed to parse job card {i}: {e}")
                continue

        return listings

    async def _parse_single_card(self, card: Locator) -> JobListing | None:
        """Extract job information from a single job card element."""
        try:
            # --- Job Title & URL ---
            title_el = card.locator(
                'h2.jobTitle a, '
                'a[data-jk], '
                '.jobTitle > a, '
                'h2 a[id^="job_"], '
                'a.jcs-JobTitle'
            )
            if await title_el.count() == 0:
                return None

            title_link = title_el.first
            title = (await title_link.inner_text()).strip()

            # Extract Indeed job ID from data-jk attribute or href
            job_id = ""
            href = await title_link.get_attribute("href") or ""
            jk_attr = await title_link.get_attribute("data-jk") or ""

            if jk_attr:
                job_id = jk_attr
            else:
                # Try to extract from href: /rc/clk?jk=XXXXX or /viewjob?jk=XXXXX
                jk_match = re.search(r'jk=([a-f0-9]+)', href)
                if jk_match:
                    job_id = jk_match.group(1)

            if not job_id:
                # Try from the card's parent or data attributes
                card_jk = await card.get_attribute("data-jk") or ""
                if card_jk:
                    job_id = card_jk

            if not job_id:
                return None  # Can't track without an ID

            # Build full URL
            url = f"{self._viewjob_url}?jk={job_id}"

            # --- Company Name ---
            company = ""
            company_el = card.locator(
                '[data-testid="company-name"], '
                '.companyName, '
                'span.css-1x7z1ps, '  # Common Indeed class
                '.company_location .companyName'
            )
            if await company_el.count() > 0:
                company = (await company_el.first.inner_text()).strip()

            # --- Location ---
            location = ""
            location_el = card.locator(
                '[data-testid="text-location"], '
                '.companyLocation, '
                '.company_location .companyLocation'
            )
            if await location_el.count() > 0:
                location = (await location_el.first.inner_text()).strip()

            # --- Salary ---
            salary = ""
            salary_el = card.locator(
                '.salary-snippet-container, '
                '[data-testid="attribute_snippet_testid"], '
                '.salaryOnly, '
                '.estimated-salary, '
                '.metadata.salary-snippet-container'
            )
            if await salary_el.count() > 0:
                salary = (await salary_el.first.inner_text()).strip()

            # --- Easy Apply badge ---
            is_easy_apply = False
            easy_apply_el = card.locator(
                '.iaLabel, '
                'button:has-text("Apply with Indeed"), '
                'span:has-text("Apply with Indeed"), '
                'a:has-text("Apply with Indeed"), '
                'span:has-text("Easily apply"), '
                'span:has-text("Easy Apply"), '
                'button:has-text("Easily apply"), '
                'button:has-text("Easy Apply"), '
                '[data-testid="indeedApply"]'
            )
            if await easy_apply_el.count() > 0:
                is_easy_apply = True
            elif self.search_config.get("easy_apply_only", True):
                # Indeed often hides the badge text on result cards even when the
                # DSQF7 filter is active. Treat these as candidates and verify the
                # real Apply with Indeed button on the detail page before applying.
                is_easy_apply = True

            # --- Snippet ---
            snippet = ""
            snippet_el = card.locator('.job-snippet, .underShelfFooter, [data-testid="jobDescriptionText"]')
            if await snippet_el.count() > 0:
                snippet = (await snippet_el.first.inner_text()).strip()

            return JobListing(
                indeed_job_id=job_id,
                title=title,
                company=company,
                location=location,
                salary=salary,
                url=url,
                is_easy_apply=is_easy_apply,
                snippet=snippet,
            )

        except Exception as e:
            logger.debug(f"Error parsing job card: {e}")
            return None

    async def has_next_page(self) -> bool:
        """Check if there's a 'Next' pagination link."""
        next_btn = self.page.locator(
            'a[data-testid="pagination-page-next"], '
            'a[aria-label="Next Page"], '
            'nav a:has-text("Next")'
        )
        return await next_btn.count() > 0

    async def go_next_page(self) -> bool:
        """Click the next page button. Returns True if navigated successfully."""
        next_btn = self.page.locator(
            'a[data-testid="pagination-page-next"], '
            'a[aria-label="Next Page"], '
            'nav a:has-text("Next")'
        )
        if await next_btn.count() == 0:
            return False

        await next_btn.first.click()
        await between_pages()

        try:
            await self.page.wait_for_selector(
                '.job_seen_beacon, [data-testid="jobListing"]',
                timeout=10000,
            )
            return True
        except PlaywrightTimeout:
            return False

    async def get_total_results(self) -> str:
        """Try to extract the total results count text from the page."""
        try:
            count_el = self.page.locator(
                '.jobsearch-JobCountAndSortPane-jobCount, '
                '[data-testid="jobCount"]'
            )
            if await count_el.count() > 0:
                return (await count_el.first.inner_text()).strip()
        except Exception:
            pass
        return "unknown"
