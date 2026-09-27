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
import random
import re
from urllib.parse import parse_qs, urlparse
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from src.control_center import REGISTRY
from src.extension_browser import ExtensionBridgeError
from src.utils.logger import cc_print, console, logger
from src.utils.delay import between_actions


JOB_CONTENT_SELECTORS = (
    'h1[data-testid="jobTitle"], '
    'h1[data-testid="jobsearch-JobInfoHeader-title"], '
    '[data-testid="jobsearch-JobInfoHeader-title"], '
    '.jobsearch-JobInfoHeader-title, '
    'h2.jobsearch-JobInfoHeader-title, '
    '.jobTitle, '
    '#jobDescriptionText, '
    '[data-testid="jobDescriptionText"], '
    '[data-testid="jobsearch-JobComponent"], '
    '[data-testid="jobsearch-ViewJobLayout"], '
    '[data-testid="jobsearch-JobInfoHeader"], '
    '[class*="jobsearch-JobComponent"], '
    '[class*="JobInfoHeader"], '
    '[class*="jobDescription"], '
    '#indeedApplyButton, '
    '[data-testid="indeedApplyButton"], '
    'button:has-text("Apply with Indeed"), '
    'button:has-text("Apply now")'
)


class JobPage:
    """Page object for an individual Indeed job listing."""

    def __init__(self, page: Page, config: dict | None = None):
        self.page = page
        self.config = config or {}
        self._context: BrowserContext | None = None
        self._expected_title = ""
        self._extension_mode = bool(getattr(page, "is_extension", False))
        self.last_navigation_timeout = False
        self._throttle_signal: str | None = None

    def set_context(self, context: BrowserContext) -> None:
        """Provide the browser context so we can watch for new tabs."""
        self._context = context

    async def open(
        self,
        url: str,
        agent_id: str = "",
        expected_title: str = "",
    ) -> bool:
        """Open a job, cooling down and retrying the same job after a timeout."""
        delays = self.config.get("bot", {}).get("delays", {})
        retry_attempts = max(0, int(delays.get("navigation_retry_attempts", 0)))
        for attempt in range(retry_attempts + 1):
            self.last_navigation_timeout = False
            if await self._open_once(url, agent_id, expected_title):
                return True
            if not self.last_navigation_timeout or attempt >= retry_attempts:
                return False

            minimum = float(delays.get("navigation_retry_delay_min", 5))
            maximum = float(delays.get("navigation_retry_delay_max", 10))
            if maximum < minimum:
                minimum, maximum = maximum, minimum
            seconds = random.uniform(max(0, minimum), max(0, maximum))
            logger.warning(
                "Navigation timed out; preserving this job and cooling down "
                "for %.0f seconds before retry %s/%s",
                seconds,
                attempt + 1,
                retry_attempts,
            )
            if not await self._interruptible_wait(seconds, agent_id):
                return False
        return False

    async def _open_once(
        self,
        url: str,
        agent_id: str = "",
        expected_title: str = "",
    ) -> bool:
        """Navigate to a job listing page.

        Args:
            url: Full Indeed job URL

        Returns:
            True if page loaded successfully
        """
        self._expected_title = (expected_title or "").strip()
        try:
            navigation_url = self._navigation_url(url)
            try:
                await self.page.goto(
                    navigation_url,
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
            except (PlaywrightTimeout, ExtensionBridgeError) as exc:
                if isinstance(exc, ExtensionBridgeError) and "timed out" not in str(exc).lower():
                    raise
                # Indeed can continue loading trackers/background resources after
                # useful job content has rendered. Validate the DOM before treating
                # a navigation timeout as a failed job page.
                if not await self._job_content_present(self._expected_title):
                    self.last_navigation_timeout = True
                    self._throttle_signal = "timeout"
                    logger.warning(f"Job page navigation timed out: {url}")
                    return False
                logger.debug("Job content rendered despite navigation timeout")

            if self._extension_mode:
                await self.page.wait_for_timeout(250)
            else:
                await between_actions()

            captcha_was_present = await self._captcha_present()
            if captcha_was_present:
                if self._throttle_signal != "timeout":
                    self._throttle_signal = "captcha"
                solved = await self._wait_for_captcha(agent_id)
                if not solved:
                    return False
                # Verification can clear back to an intermediate/search page
                # instead of completing the original tracking redirect. Never
                # sacrifice that job: reopen the same requested URL once.
                if not (
                    self._canonical_job_navigation_present(url)
                    or await self._job_content_present(self._expected_title)
                ):
                    logger.info(
                        "Captcha cleared without restoring the requested job; reopening it"
                    )
                    await self.page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
                    if self._extension_mode:
                        await self.page.wait_for_timeout(250)
                    else:
                        await between_actions()
                    if await self._captcha_present():
                        solved = await self._wait_for_captcha(agent_id)
                        if not solved:
                            return False

            # The extension can probe the rendered DOM directly. Avoid the
            # Playwright-style 10-second selector wait that every missing frame
            # would otherwise pay.
            if self._extension_mode:
                for _ in range(12):
                    if (
                        self._canonical_job_navigation_present(url)
                        or await self._job_content_present(self._expected_title)
                    ):
                        return True
                    await self.page.wait_for_timeout(250)
                current_url = self.page.url
                logger.warning(
                    "Job detail content did not appear within 3 seconds: "
                    f"{url} (current URL: {current_url})"
                )
                return False

            # Indeed regularly changes its title markup. A description or Apply
            # control is also sufficient evidence that the detail page is usable.
            try:
                await self.page.wait_for_selector(JOB_CONTENT_SELECTORS, timeout=10000)
            except PlaywrightTimeout:
                if await self._job_content_present(self._expected_title):
                    logger.debug("Job page recognized from its visible listing title")
                    return True
                raise
            return True

        except PlaywrightTimeout:
            current_url = self.page.url
            logger.warning(
                "Job detail content did not appear within 10 seconds: "
                f"{url} (current URL: {current_url})"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to open job page: {e}")
            return False

    def _navigation_url(self, requested_url: str) -> str:
        """Leave Smart Apply via a direct job URL instead of a tracking redirect."""
        try:
            current = urlparse(str(getattr(self.page, "url", "")))
            requested = urlparse(requested_url)
            current_host = (current.hostname or "").lower()
            requested_host = (requested.hostname or "").lower()
            job_key = parse_qs(requested.query).get("jk", [""])[0]
            on_smart_apply = current_host == "smartapply.indeed.com"
            valid_indeed_host = (
                requested_host == "indeed.com"
                or requested_host.endswith(".indeed.com")
            )
            if on_smart_apply and valid_indeed_host and len(job_key) >= 8:
                direct_url = (
                    f"{requested.scheme or 'https'}://{requested.netloc}"
                    f"/viewjob?jk={job_key}"
                )
                logger.info(
                    "Leaving the completed application page via direct job navigation"
                )
                return direct_url
        except Exception:
            pass
        return requested_url

    def mark_throttle_signal(self, reason: str = "captcha") -> None:
        """Record the strongest current anti-abuse pressure signal."""
        if self._throttle_signal != "timeout" or reason == "timeout":
            self._throttle_signal = reason

    def consume_throttle_signal(self) -> str | None:
        """Return and clear the current anti-abuse pressure signal."""
        signaled = self._throttle_signal
        self._throttle_signal = None
        return signaled

    async def _interruptible_wait(self, seconds: float, agent_id: str) -> bool:
        """Wait without making Stop, Skip, or Pause controls unresponsive."""
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if agent_id:
                if REGISTRY.is_stopped(agent_id) or REGISTRY.is_skip_requested(agent_id):
                    return False
                if not await REGISTRY.wait_if_paused(agent_id):
                    return False
            remaining = deadline - asyncio.get_running_loop().time()
            await asyncio.sleep(min(0.5, max(0, remaining)))
        return True

    async def _job_content_present(self, expected_title: str = "") -> bool:
        """Return whether recognizable, visible job-detail content is rendered."""
        try:
            elements = self.page.locator(JOB_CONTENT_SELECTORS)
            count = await elements.count()
            for index in range(count):
                if await elements.nth(index).is_visible():
                    return True
        except Exception:
            pass

        # Some Indeed layouts render a complete job page without stable title,
        # description, or Apply attributes. The exact title came from the search
        # card, so seeing it in the visible body is strong page-specific evidence.
        expected_title = self._normalize_title(expected_title)
        if expected_title:
            try:
                body_text = await self.page.locator("body").inner_text(timeout=2000)
                visible_text = self._normalize_title(body_text)
                if (
                    expected_title in visible_text
                    or self._title_tokens_present(expected_title, visible_text)
                ):
                    return True
            except Exception:
                pass

        # Current Indeed layouts sometimes render the detail page without any
        # stable title/description test IDs. A valid viewjob URL plus a
        # substantial, job-like visible body is sufficient after CAPTCHA checks.
        try:
            parsed = urlparse(self.page.url)
            job_key = parse_qs(parsed.query).get("jk", [""])[0]
            if "/viewjob" in parsed.path.lower() and len(job_key) >= 8:
                body_text = await self.page.locator("body").inner_text(timeout=2000)
                normalized_body = " ".join(body_text.lower().split())
                markers = (
                    "job details",
                    "full job description",
                    "apply now",
                    "apply with indeed",
                    "save job",
                    "company",
                )
                if len(normalized_body) >= 200 and any(
                    marker in normalized_body for marker in markers
                ):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _normalize_title(value: str) -> str:
        return re.sub(r"[^a-z0-9+#.]+", " ", value.lower()).strip()

    @staticmethod
    def _title_tokens_present(expected_title: str, visible_text: str) -> bool:
        """Allow small search-card/detail-title wording differences."""
        tokens = [
            token
            for token in expected_title.split()
            if len(token) > 2 and token not in {"the", "and", "with", "for"}
        ]
        if not tokens:
            return False
        matched = sum(
            1 for token in tokens if re.search(rf"\b{re.escape(token)}\b", visible_text)
        )
        return matched >= min(3, len(tokens)) and matched / len(tokens) >= 0.6

    def _canonical_job_navigation_present(self, requested_url: str) -> bool:
        """Recognize Indeed's canonical redirect for the requested job.

        Indeed's tracking URL redirects to /viewjob and may render job details
        without stable selectors or title/company query metadata. Matching the
        immutable job key across a known Indeed tracking redirect prevents a
        different or unrelated page from being accepted.
        """
        try:
            requested = urlparse(requested_url)
            current = urlparse(self.page.url)
            requested_query = parse_qs(requested.query)
            current_query = parse_qs(current.query)
            requested_key = requested_query.get("jk", [""])[0]
            current_key = current_query.get("jk", [""])[0]
            has_canonical_metadata = bool(
                current_query.get("t", [""])[0]
                or current_query.get("cmp", [""])[0]
            )
            requested_path = requested.path.lower().rstrip("/")
            is_tracking_redirect = requested_path in {
                "/rc/clk",
                "/pagead/clk",
            }
            requested_host = (requested.hostname or "").lower()
            current_host = (current.hostname or "").lower()
            def is_indeed_host(host: str) -> bool:
                return host.startswith("indeed.") or ".indeed." in host

            both_indeed_hosts = (
                is_indeed_host(requested_host)
                and is_indeed_host(current_host)
            )
            return bool(
                current.path.lower().rstrip("/") == "/viewjob"
                and len(requested_key) >= 8
                and current_key == requested_key
                and both_indeed_hosts
                and (has_canonical_metadata or is_tracking_redirect)
            )
        except Exception:
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

        # Auto-resume as soon as captcha clears. The extension can probe the
        # page cheaply, so use a 250 ms trigger instead of adding one-second
        # latency after an automatic verification.
        poll_interval = 0.25 if self._extension_mode else 1.0
        auto_checks = max(1, int(8 / poll_interval))
        for _ in range(auto_checks):
            if not await self._captcha_present():
                if agent_id:
                    REGISTRY.clear_prompt(agent_id)
                    REGISTRY.append_log(
                        agent_id,
                        "captcha cleared automatically; resuming the same job",
                    )
                break
            await asyncio.sleep(poll_interval)

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
        poll_interval = 0.25 if self._extension_mode else 1.0
        while asyncio.get_running_loop().time() < deadline:
            if not await self._captcha_present():
                return True
            await asyncio.sleep(poll_interval)
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
            'h1[data-testid="jobsearch-JobInfoHeader-title"]',
            '[data-testid="jobsearch-JobInfoHeader-title"]',
            '.jobsearch-JobInfoHeader-title',
            'h2.jobsearch-JobInfoHeader-title',
            '.jobTitle',
        ]
        for sel in selectors:
            el = self.page.locator(sel)
            if await el.count() > 0:
                return (await el.first.inner_text()).strip()
        return self._expected_title

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

    async def has_easy_apply(self, allow_generic_apply: bool = False) -> bool:
        """Check if the job has an Indeed Easy Apply button (not a company-site redirect)."""
        selectors = (
            '#indeedApplyButton, '
            'button[id*="indeedApply"], '
            'button:has-text("Apply with Indeed"), '
            'button:has-text("Easy Apply"), '
            '[data-testid="indeedApplyButton"], '
            '.indeed-apply-button'
        )
        if allow_generic_apply:
            # Indeed currently labels some filtered Easy Apply controls simply
            # "Apply now". Only enable this ambiguous label when the search-card
            # context already established that the result is Easy Apply.
            selectors += (
                ', button:has-text("Apply now")'
                ', a:has-text("Apply now")'
                ', [role="button"]:has-text("Apply now")'
                ', [aria-label*="Apply now" i]'
                ', [data-testid="job-apply-button"]'
            )
        apply_button = self.page.locator(selectors)
        return await apply_button.count() > 0

    async def has_company_site_apply(self) -> bool:
        """Return True if the job has an 'Apply on company site' / external apply button."""
        external_selectors = [
            # Indeed's standard external-redirect button
            'button:has-text("Apply on company site")',
            'a:has-text("Apply on company site")',
            '[data-testid="job-apply-button"]:not([id*="indeedApply"])',
            # Generic fallback — a visible "Apply now" that is NOT the Indeed modal button
            'button:has-text("Apply now")',
            'a:has-text("Apply now")',
        ]
        for sel in external_selectors:
            try:
                el = self.page.locator(sel)
                if await el.count() > 0 and await el.first.is_visible():
                    # Exclude Indeed Easy Apply buttons
                    el_id = await el.first.get_attribute("id") or ""
                    if "indeedApply" in el_id or "indeedapply" in el_id.lower():
                        continue
                    return True
            except Exception:
                continue
        return False

    async def click_company_site_apply(self) -> "Page | None":
        """Click the 'Apply on company site' button and return the new tab that opens.

        Returns:
            The new Playwright Page for the external site, or None on failure.
        """
        if self._context is None:
            logger.warning("No browser context set — cannot intercept new tab")
            return None

        external_selectors = [
            'button:has-text("Apply on company site")',
            'a:has-text("Apply on company site")',
            'button:has-text("Apply now")',
            'a:has-text("Apply now")',
            '[data-testid="job-apply-button"]:not([id*="indeedApply"])',
        ]

        for sel in external_selectors:
            try:
                el = self.page.locator(sel)
                if await el.count() == 0:
                    continue
                btn = el.first
                if not await btn.is_visible():
                    continue
                # Skip if it's an Indeed Easy Apply button
                el_id = await btn.get_attribute("id") or ""
                if "indeedApply" in el_id or "indeedapply" in el_id.lower():
                    continue

                logger.info(f"🖱️ Clicking company-site apply button: '{sel}'")
                async with self._context.expect_page() as new_page_info:
                    await btn.click()
                try:
                    import asyncio
                    new_tab = await asyncio.wait_for(
                        asyncio.ensure_future(new_page_info.value),
                        timeout=15,
                    )
                    await between_actions()
                    return new_tab
                except asyncio.TimeoutError:
                    logger.warning("Company-site button click did not open a new tab")
                    return None
            except Exception as exc:
                logger.debug(f"company-site click failed for '{sel}': {exc}")
                continue

        logger.warning("Could not find or click a company-site apply button")
        return None

    async def click_apply(self, agent_id: str = "") -> bool:
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
            'a:has-text("Apply now")',
            '[role="button"]:has-text("Apply now")',
            '[aria-label*="Apply now" i]',
            '[data-testid="job-apply-button"]',
            'button:has-text("Easy Apply")',
            '.indeed-apply-button',
        ]

        attempted = 0
        for sel in apply_selectors:
            buttons = self.page.locator(sel)
            for index in range(await buttons.count()):
                btn = buttons.nth(index)
                try:
                    if not await btn.is_visible() or not await btn.is_enabled():
                        continue
                    await btn.click()
                    attempted += 1
                    if not self._extension_mode:
                        logger.info("Clicked Apply button")
                        await between_actions()
                        return True
                    if await self._wait_for_application_started(timeout=4000):
                        logger.info("Clicked Apply button; application opened")
                        return True
                    logger.warning(
                        "Apply control was clicked, but no application form opened; "
                        "trying another visible match"
                    )
                    if attempted >= 2:
                        break
                except Exception as exc:
                    logger.debug("Apply click failed for '%s': %s", sel, exc)
                    continue
            if attempted >= 2:
                break

        if attempted and agent_id:
            return await self._request_manual_apply_start(agent_id)

        logger.warning("Could not find a usable Apply button")
        return False

    async def _wait_for_application_started(self, timeout: int = 4000) -> bool:
        """Return once an Indeed application URL, frame, or control is visible."""
        selectors = [
            'iframe[src*="indeedapply" i]',
            'iframe[src*="smartapply" i]',
            '[data-testid^="ia-"]',
            '[class*="ia-BasePage"]',
            '[class*="ia-FormPage"]',
            '#ia-container',
            '.indeed-apply-widget',
            'input[type="file"]',
            'button:has-text("Continue")',
            'button:has-text("Review your application")',
            'button:has-text("Submit your application")',
        ]
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while asyncio.get_running_loop().time() < deadline:
            try:
                await self.page.locator("body").count()
            except Exception:
                pass
            current_url = str(getattr(self.page, "url", "")).lower()
            if any(marker in current_url for marker in ("indeedapply", "smartapply", "applystart")):
                return True
            for selector in selectors:
                try:
                    matches = self.page.locator(selector)
                    count = await matches.count()
                    for index in range(count):
                        if await matches.nth(index).is_visible():
                            return True
                except Exception:
                    continue
            await asyncio.sleep(0.1 if self._extension_mode else 0.25)
        return False

    async def _request_manual_apply_start(self, agent_id: str) -> bool:
        """Keep this job open while the user manually starts its application."""
        message = (
            "The Apply button was clicked, but Indeed did not open the application. "
            "Click Apply now in the browser yourself. Once the application form "
            "is visible, click Continue here."
        )
        REGISTRY.set_state(agent_id, "manual_wait")
        REGISTRY.set_prompt(agent_id, message, options=["Continue", "Skip job"])
        REGISTRY.append_log(agent_id, "waiting for user to open the application form")
        try:
            await self.page.bring_to_front()
        except Exception:
            pass

        while True:
            if REGISTRY.is_stopped(agent_id):
                REGISTRY.clear_prompt(agent_id)
                return False
            if REGISTRY.is_skip_requested(agent_id):
                REGISTRY.clear_prompt(agent_id)
                return False
            if await self._wait_for_application_started(timeout=500):
                REGISTRY.clear_prompt(agent_id)
                REGISTRY.set_state(agent_id, "applying")
                REGISTRY.append_log(agent_id, "application form opened; continuing")
                return True
            answer = REGISTRY.consume_answer(agent_id)
            if answer is not None:
                normalized = str(answer).strip().lower()
                if normalized in {"__skip_job__", "skip", "skip job"}:
                    REGISTRY.clear_prompt(agent_id)
                    return False
                if await self._wait_for_application_started(timeout=4000):
                    REGISTRY.clear_prompt(agent_id)
                    REGISTRY.set_state(agent_id, "applying")
                    return True
                REGISTRY.set_prompt(agent_id, message, options=["Continue", "Skip job"])
            await asyncio.sleep(0.25)

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
