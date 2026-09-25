"""
External (Company-Site) Apply Handler.

Handles the full flow for jobs that redirect to the employer's own website
instead of using Indeed Easy Apply.  Covers every scenario observed in the wild:

    1. Form already present at the bottom of the page → fill and submit.
    2. An Apply / Apply Now / Apply for Job button present →
           a. Login / account-creation wall   → mark as ACCOUNT_NEEDED
           b. Autofill popup (Resume / Manual) → pick Autofill, then handle result
           c. Email address shown              → mark as MANUAL_NEEDED + save lead
           d. New page opened                 → re-scan recursively (max 2 levels)
    3. Login / account wall immediately visible → mark as ACCOUNT_NEEDED
    4. Email address shown with no form         → mark as MANUAL_NEEDED + save lead

Usage::

    handler = ExternalApplyHandler(config, db, question_matcher)
    result  = await handler.attempt(context, job_info, resume_path, external_tab)
"""
from __future__ import annotations

import asyncio
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from src.handlers.form_detector import FormDetector
from src.handlers.form_filler import FormFiller
from src.handlers.question_matcher import QuestionMatcher
from src.database import Database
from src.utils.delay import between_actions
from src.utils.logger import logger

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class ExternalApplyResult(Enum):
    """Outcome of an external apply attempt."""
    SUCCESS = "external_applied"       # Submitted successfully on company site
    ACCOUNT_NEEDED = "account_needed"  # Login / registration wall — skip
    MANUAL_NEEDED = "manual_needed"    # Email-only — save for manual follow-up
    FAILED = "failed"                  # Unexpected error / could not complete


@dataclass
class ExternalApplyOutcome:
    """Detailed result returned by ExternalApplyHandler.attempt()."""
    result: ExternalApplyResult
    note: str = ""          # Human-readable explanation
    apply_email: str = ""   # Populated for MANUAL_NEEDED results


# ---------------------------------------------------------------------------
# ATS / Apply-button patterns (common across Greenhouse, Lever, Workday, etc.)
# ---------------------------------------------------------------------------

_APPLY_BUTTON_TEXTS = re.compile(
    r"^(apply(\s+now)?|apply\s+for\s+(this\s+)?(job|position|role)|"
    r"apply\s+online|apply\s+here|submit\s+application|apply\s+with\s+resume|"
    r"get\s+started|start\s+application)$",
    re.IGNORECASE,
)

_AUTOFILL_TEXTS = re.compile(
    r"autofill\s+with\s+resume|auto.?fill|fill\s+with\s+resume|"
    r"import\s+resume|use\s+resume",
    re.IGNORECASE,
)

# Text patterns that indicate a login / account-creation wall
_LOGIN_WALL_PATTERNS = [
    re.compile(r"sign\s+in\s+to\s+(apply|continue|your\s+account)", re.IGNORECASE),
    re.compile(r"log\s*in\s+to\s+(apply|continue|your\s+account)", re.IGNORECASE),
    re.compile(r"create\s+(an?\s+)?account\s+to\s+apply", re.IGNORECASE),
    re.compile(r"create\s+a\s+profile\s+to\s+apply", re.IGNORECASE),
    re.compile(r"register\s+to\s+apply", re.IGNORECASE),
    re.compile(r"you\s+must\s+(log\s*in|sign\s+in|register)", re.IGNORECASE),
]

# Structural selectors for login pages
_LOGIN_SELECTORS = [
    'h1:has-text("Sign in")',
    'h1:has-text("Log in")',
    'h1:has-text("Create account")',
    'h1:has-text("Create an account")',
    'h1:has-text("Register")',
    'h2:has-text("Sign in")',
    'h2:has-text("Log in")',
    '[data-test="login-form"]',
    '[data-testid="login-form"]',
    'form[action*="login"]',
    'form[action*="signin"]',
    'form[action*="register"]',
    'form[action*="signup"]',
]

# CSS selectors for apply buttons on common ATS platforms
_APPLY_CSS_SELECTORS = [
    'button:has-text("Apply Now")',
    'button:has-text("Apply now")',
    'button:has-text("Apply")',
    'a:has-text("Apply Now")',
    'a:has-text("Apply now")',
    'a:has-text("Apply")',
    '[data-automation="apply-button"]',
    '[data-qa="btn-apply"]',
    '[data-testid="apply-button"]',
    'button[class*="apply"]',
    'a[class*="apply-btn"]',
    'a[class*="applyButton"]',
    # Greenhouse
    '#apply_button',
    '.apply-button',
    # Lever
    '.template-btn-submit',
    # Workday
    'button[data-automation-id="applyButton"]',
    'button[data-automation-id="Apply"]',
    # iCIMS
    'a[title="Apply for Position"]',
    # BambooHR
    'button#apply',
    # SmartRecruiters
    '[data-qa="btn-apply-bottom"]',
    '[data-qa="btn-apply-top"]',
]

# CSV path for manual leads
_MANUAL_CSV = Path("data/manual_apply_needed.csv")
_MANUAL_CSV_HEADERS = [
    "found_at", "job_title", "company", "location",
    "job_url", "apply_email", "reason", "description_preview",
]


# ---------------------------------------------------------------------------
# CSV helper
# ---------------------------------------------------------------------------

def _append_manual_csv(row: dict) -> None:
    """Append a row to the manual-leads CSV, creating headers if needed."""
    _MANUAL_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not _MANUAL_CSV.exists() or _MANUAL_CSV.stat().st_size == 0
    with _MANUAL_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_MANUAL_CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in _MANUAL_CSV_HEADERS})


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

class ExternalApplyHandler:
    """Attempts to apply for a job on the employer's own website.

    Args:
        config:           Full bot configuration dict.
        db:               Connected Database instance.
        question_matcher: For answering screening questions.
    """

    def __init__(
        self,
        config: dict,
        db: Database,
        question_matcher: QuestionMatcher,
    ) -> None:
        self.config = config
        self.db = db
        self.question_matcher = question_matcher
        self._csa_cfg: dict = config.get("company_site_apply", {})
        self._max_wait_ms: int = int(self._csa_cfg.get("max_wait_seconds", 15)) * 1000
        self._skip_login: bool = bool(self._csa_cfg.get("skip_if_login_required", True))
        self._save_email: bool = bool(self._csa_cfg.get("save_email_leads", True))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def attempt(
        self,
        context: BrowserContext,
        job_info: dict,
        resume_path: str,
        external_tab: Page,
        agent_id: str | None = None,
    ) -> ExternalApplyOutcome:
        """Try to apply on the company's site.

        Args:
            context:      Playwright browser context (for watching new tabs).
            job_info:     Job metadata dict: title, company, location, url,
                          description, indeed_job_id, salary.
            resume_path:  Path to the resume PDF.
            external_tab: The already-opened company-site tab (from clicking
                          the "Apply on company site" button in job_page.py).
            agent_id:     Optional agent ID for Web UI prompts.

        Returns:
            ExternalApplyOutcome describing the result.
        """
        self._agent_id = agent_id
        page = external_tab
        try:
            await self._wait_for_page_ready(page)
            logger.info(f"🌐 External page loaded: {page.url}")
            outcome = await self._scan_and_act(page, context, job_info, resume_path, depth=0)
            return outcome
        except Exception as exc:
            logger.error(f"ExternalApplyHandler error: {exc}", exc_info=True)
            return ExternalApplyOutcome(ExternalApplyResult.FAILED, str(exc))
        finally:
            # Always close the external tab when done
            if page and not page.is_closed():
                try:
                    await page.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Core scan-and-act (recursive, max depth 2)
    # ------------------------------------------------------------------

    async def _scan_and_act(
        self,
        page: Page,
        context: BrowserContext,
        job_info: dict,
        resume_path: str,
        depth: int,
    ) -> ExternalApplyOutcome:
        """Classify the current page and take the appropriate action."""

        job_id      = job_info.get("indeed_job_id", "")
        job_title   = job_info.get("title", "")
        company     = job_info.get("company", "")
        location    = job_info.get("location", "")
        job_url     = job_info.get("url", "")
        description = job_info.get("description", "")

        # ── 1. Immediate login wall ───────────────────────────────────
        if self._skip_login and await self._detect_login_wall(page):
            logger.info(f"🔒 Login wall detected on: {page.url}")
            return self._make_account_needed("Login / account creation required")

        # ── 2. Email-only page ────────────────────────────────────────
        email = await self._detect_email_only(page)
        if email:
            logger.info(f"📧 Email-only application: {email}")
            return await self._make_manual_needed(
                job_id, job_title, company, location, job_url, description,
                apply_email=email, reason="Email-only application",
            )

        # ── 3. Autofill popup ─────────────────────────────────────────
        if await self._detect_autofill_popup(page):
            logger.info("🤖 Autofill popup detected — selecting 'Autofill with Resume'")
            popup_result = await self._handle_autofill_popup(
                page, context, job_info, resume_path, depth
            )
            if popup_result is not None:
                return popup_result
            # popup dismissed but form should now be visible — fall through

        # ── 4. Form directly on page ──────────────────────────────────
        if await self._detect_form_present(page):
            logger.info("📋 Form detected on company site — filling and submitting")
            return await self._fill_and_submit(page, job_info, resume_path)

        # ── 5. Apply button ───────────────────────────────────────────
        apply_btn = await self._find_apply_button(page)
        if apply_btn:
            btn_text = ""
            try:
                btn_text = (await apply_btn.inner_text()).strip()
            except Exception:
                pass
            logger.info(f"🖱️ Found Apply button: '{btn_text}'")

            if depth >= 2:
                logger.warning("Max navigation depth reached")
                return ExternalApplyOutcome(ExternalApplyResult.FAILED, "Max depth reached")

            # Click and watch for a new tab
            new_tab: Page | None = None
            try:
                async with context.expect_page() as new_page_event:
                    await apply_btn.click()
                try:
                    new_tab = await asyncio.wait_for(
                        asyncio.ensure_future(new_page_event.value),
                        timeout=self._max_wait_ms / 1000,
                    )
                except asyncio.TimeoutError:
                    new_tab = None
            except Exception:
                new_tab = None

            if new_tab and not new_tab.is_closed():
                await self._wait_for_page_ready(new_tab)
                logger.info(f"🌐 Apply button opened new tab: {new_tab.url}")
                outcome = await self._scan_and_act(
                    new_tab, context, job_info, resume_path, depth + 1
                )
                if not new_tab.is_closed():
                    await new_tab.close()
                return outcome

            # No new tab — the current page changed; re-scan
            await asyncio.sleep(2)
            await self._wait_for_page_ready(page)
            return await self._scan_and_act(page, context, job_info, resume_path, depth + 1)

        # ── 6. Nothing found ──────────────────────────────────────────
        logger.warning(f"No actionable element found on: {page.url}")
        return ExternalApplyOutcome(
            ExternalApplyResult.FAILED, "No apply button or form found"
        )

    # ------------------------------------------------------------------
    # Detection helpers
    # ------------------------------------------------------------------

    async def _detect_login_wall(self, page: Page) -> bool:
        """Return True if the page is a login / account-creation wall."""
        # Structural selectors (fast)
        for sel in _LOGIN_SELECTORS:
            try:
                el = page.locator(sel)
                if await el.count() > 0 and await el.first.is_visible():
                    return True
            except Exception:
                continue

        # Heuristic: password input + email/username input = login form
        try:
            has_password = await page.locator('input[type="password"]').count() > 0
            if has_password:
                has_identity = await page.locator(
                    'input[type="email"], input[name*="email"], input[name*="username"]'
                ).count() > 0
                if has_identity:
                    return True
        except Exception:
            pass

        # Text-based check (slowest — only if above checks pass)
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
            for pattern in _LOGIN_WALL_PATTERNS:
                if pattern.search(body_text):
                    return True
        except Exception:
            pass

        return False

    async def _detect_email_only(self, page: Page) -> str:
        """Return an email address if the page shows an email-only application flow.

        Returns:
            Email string if found, empty string otherwise.
        """
        try:
            # mailto: links are the most reliable signal
            mailto_links = page.locator('a[href^="mailto:"]')
            count = await mailto_links.count()
            for i in range(count):
                href = await mailto_links.nth(i).get_attribute("href") or ""
                email = href.replace("mailto:", "").split("?")[0].strip()
                if email and "@" in email:
                    return email

            # Free-text search: "send your resume to <email>"
            body_text = await page.locator("body").inner_text(timeout=3000)
            match = re.search(
                r"(?:send|email|mail|apply\s+by\s+email|send\s+.*?to)[^\n]*?"
                r"([\w.+-]+@[\w-]+\.[\w.]+)",
                body_text,
                re.IGNORECASE,
            )
            if match:
                return match.group(1)

        except Exception:
            pass

        return ""

    async def _detect_autofill_popup(self, page: Page) -> bool:
        """Return True if an Autofill-with-Resume popup/overlay is visible."""
        try:
            autofill_locators = [
                page.get_by_text(_AUTOFILL_TEXTS),
                page.locator(':has-text("Autofill with Resume")'),
                page.locator('[data-qa*="autofill"], [data-testid*="autofill"]'),
            ]
            for loc in autofill_locators:
                count = await loc.count()
                for i in range(count):
                    if await loc.nth(i).is_visible():
                        return True
        except Exception:
            pass
        return False

    async def _detect_form_present(self, page: Page) -> bool:
        """Return True if there's a meaningful fillable form (2+ visible inputs)."""
        try:
            inputs = page.locator(
                'input:not([type="hidden"]):not([type="submit"]):not([type="button"]),'
                'textarea, select'
            )
            count = await inputs.count()
            visible = 0
            for i in range(min(count, 20)):
                try:
                    if await inputs.nth(i).is_visible():
                        visible += 1
                        if visible >= 2:
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _find_apply_button(self, page: Page):
        """Find an Apply / Apply Now button. Returns locator element or None."""
        # Role-based (most reliable)
        for role in ("button", "link"):
            try:
                btn = page.get_by_role(role, name=_APPLY_BUTTON_TEXTS)
                count = await btn.count()
                for i in range(count):
                    candidate = btn.nth(i)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        return candidate
            except Exception:
                continue

        # CSS selector sweep
        for sel in _APPLY_CSS_SELECTORS:
            try:
                el = page.locator(sel)
                count = await el.count()
                for i in range(count):
                    candidate = el.nth(i)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        return candidate
            except Exception:
                continue

        return None

    # ------------------------------------------------------------------
    # Autofill popup handler
    # ------------------------------------------------------------------

    async def _handle_autofill_popup(
        self,
        page: Page,
        context: BrowserContext,
        job_info: dict,
        resume_path: str,
        depth: int,
    ) -> ExternalApplyOutcome | None:
        """Click 'Autofill with Resume' and handle whatever comes next.

        Returns an outcome if the popup flow ended definitively, or
        None to let the caller continue scanning the same page normally.
        """
        autofill_locators = [
            page.get_by_role("button", name=_AUTOFILL_TEXTS),
            page.get_by_text(_AUTOFILL_TEXTS),
            page.locator('[data-qa*="autofill"], [data-testid*="autofill"]'),
        ]

        clicked = False
        for loc in autofill_locators:
            try:
                count = await loc.count()
                for i in range(count):
                    candidate = loc.nth(i)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        await candidate.click()
                        logger.info("🤖 Clicked 'Autofill with Resume'")
                        clicked = True
                        break
            except Exception:
                continue
            if clicked:
                break

        if not clicked:
            logger.debug("Autofill option not clickable; falling through")
            return None

        await asyncio.sleep(2)
        await self._wait_for_page_ready(page)

        # After autofill click — check what happened
        if self._skip_login and await self._detect_login_wall(page):
            logger.info("🔒 Autofill requires login")
            return self._make_account_needed("Autofill requires login")

        if await self._detect_form_present(page):
            logger.info("📋 Form appeared after Autofill click")
            return await self._fill_and_submit(page, job_info, resume_path)

        # Popup dismissed but nothing obvious yet — let caller continue
        return None

    # ------------------------------------------------------------------
    # Form fill + submit
    # ------------------------------------------------------------------

    async def _fill_and_submit(
        self,
        page: Page,
        job_info: dict,
        resume_path: str,
    ) -> ExternalApplyOutcome:
        """Detect and fill all form fields, then submit."""
        job_context = {
            "salary": job_info.get("salary", ""),
            "description": job_info.get("description", ""),
            "agent_id": self._agent_id or "",
            "mode": self.config.get("bot", {}).get("mode", "auto"),
        }
        detector = FormDetector()
        filler = FormFiller(self.config, page, self.question_matcher, job_context)

        max_steps = 12
        last_sig = ""
        same_sig_count = 0

        for step in range(1, max_steps + 1):
            logger.info(f"  [External] Step {step}...")
            await asyncio.sleep(1)

            # Check success
            if await self._is_application_complete(page):
                logger.info("✅ External application submitted successfully!")
                return ExternalApplyOutcome(ExternalApplyResult.SUCCESS)

            # Check for login wall that appeared mid-form
            if self._skip_login and await self._detect_login_wall(page):
                return self._make_account_needed("Login required mid-form")

            # Try resume upload if file input is present
            if resume_path:
                await self._try_upload_resume(page, resume_path)

            # Detect and fill fields
            fields = await detector.detect_fields(page)
            if fields:
                sig = "|".join(
                    f"{f.field_type.name}:{f.label.strip().lower()}" for f in fields
                )
                if sig != last_sig:
                    logger.info(f"  [External] Filling {len(fields)} field(s)...")
                    await filler.fill_fields(fields)
                    last_sig = sig
                    same_sig_count = 0
                else:
                    same_sig_count += 1
                    logger.debug("  [External] Same fields as last step — skipping refill")
                    if same_sig_count >= 2:
                        logger.warning("  [External] Stuck on same form fields, unable to advance.")
                        break

            # Advance
            if not await self._advance_or_submit(page):
                if await self._is_application_complete(page):
                    return ExternalApplyOutcome(ExternalApplyResult.SUCCESS)
                logger.warning("  [External] Could not advance form")
                break

            await asyncio.sleep(1.5)

        # Final check
        if await self._is_application_complete(page):
            return ExternalApplyOutcome(ExternalApplyResult.SUCCESS)

        return ExternalApplyOutcome(ExternalApplyResult.FAILED, "Form could not be completed")

    async def _try_upload_resume(self, page: Page, resume_path: str) -> None:
        """Upload resume to any file input present on the current step."""
        try:
            path = Path(resume_path).expanduser().resolve()
            if not path.is_file():
                return
            file_inputs = page.locator('input[type="file"]')
            if await file_inputs.count() > 0:
                await file_inputs.first.set_input_files(str(path))
                logger.info(f"📎 Uploaded resume: {path.name}")
                await between_actions()
        except Exception as exc:
            logger.debug(f"Resume upload skipped: {exc}")

    async def _advance_or_submit(self, page: Page) -> bool:
        """Click Continue / Next / Submit. Returns True if a button was clicked."""
        button_names = re.compile(
            r"^(continue|next|review|submit|apply|send|send\s+application|"
            r"submit\s+application|submit\s+your\s+application|finish|complete)$",
            re.IGNORECASE,
        )
        for role in ("button", "link"):
            try:
                btn = page.get_by_role(role, name=button_names)
                count = await btn.count()
                for i in range(count):
                    candidate = btn.nth(i)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        text = (await candidate.inner_text()).strip()
                        logger.info(f"  [External] → Clicking: '{text}'")
                        await candidate.scroll_into_view_if_needed()
                        await candidate.click()
                        return True
            except Exception:
                continue

        # CSS fallback
        for sel in ['button[type="submit"]', 'input[type="submit"]',
                    'button:has-text("Submit")', 'button:has-text("Apply")',
                    'button:has-text("Continue")', 'button:has-text("Next")']:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    first = el.first
                    if await first.is_visible() and await first.is_enabled():
                        text = (await first.inner_text()).strip()
                        logger.info(f"  [External] → Clicking: '{text}'")
                        await first.scroll_into_view_if_needed()
                        await first.click()
                        return True
            except Exception:
                continue

        return False

    async def _is_application_complete(self, page: Page) -> bool:
        """Check for common success confirmation text across ATS platforms."""
        success_phrases = [
            "application submitted",
            "application received",
            "application sent",
            "thank you for applying",
            "thank you for your application",
            "your application has been",
            "successfully applied",
            "application complete",
            "we've received your application",
            "we received your application",
        ]
        try:
            body = (await page.locator("body").inner_text(timeout=3000)).lower()
            return any(p in body for p in success_phrases)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Page-wait helpers
    # ------------------------------------------------------------------

    async def _wait_for_page_ready(self, page: Page) -> None:
        """Wait for the page to be ready (DOM loaded + short network idle)."""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=self._max_wait_ms)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Outcome factories
    # ------------------------------------------------------------------

    def _make_account_needed(self, note: str) -> ExternalApplyOutcome:
        return ExternalApplyOutcome(
            result=ExternalApplyResult.ACCOUNT_NEEDED,
            note=note,
        )

    async def _make_manual_needed(
        self,
        job_id: str,
        job_title: str,
        company: str,
        location: str,
        job_url: str,
        description: str,
        apply_email: str = "",
        reason: str = "",
    ) -> ExternalApplyOutcome:
        """Persist lead to DB + CSV and return a MANUAL_NEEDED outcome."""
        desc_preview = description[:300].replace("\n", " ")

        # Persist to DB
        try:
            await self.db.save_manual_lead(
                indeed_job_id=job_id,
                job_title=job_title,
                company=company,
                location=location,
                job_url=job_url,
                apply_email=apply_email,
                reason=reason,
                description_preview=desc_preview,
            )
        except Exception as exc:
            logger.debug(f"Could not save manual lead to DB: {exc}")

        # Persist to CSV
        if self._save_email:
            try:
                _append_manual_csv({
                    "found_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "job_title": job_title,
                    "company": company,
                    "location": location,
                    "job_url": job_url,
                    "apply_email": apply_email,
                    "reason": reason,
                    "description_preview": desc_preview,
                })
                logger.info(f"📧 Manual lead saved → {_MANUAL_CSV}")
            except Exception as exc:
                logger.debug(f"Could not write manual lead CSV: {exc}")

        return ExternalApplyOutcome(
            result=ExternalApplyResult.MANUAL_NEEDED,
            note=reason,
            apply_email=apply_email,
        )
