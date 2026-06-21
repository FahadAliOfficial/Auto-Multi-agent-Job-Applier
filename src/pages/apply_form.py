"""
Indeed Easy Apply Form Handler.

Handles the multi-step application form flow:
- Detects form steps and navigates through them
- Delegates field detection and filling to handler modules
- Handles resume upload
- Manages Continue/Next/Submit navigation
- Detects application success or failure
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from src.handlers.form_detector import FormDetector, FieldCategory, FormField
from src.handlers.form_filler import FormFiller
from src.handlers.question_matcher import QuestionMatcher
from src.database import Database
from src.control_center import REGISTRY
from src.utils.logger import cc_print, logger, console
from src.utils.delay import between_actions, between_pages
from src.utils.screenshot import capture_screenshot, capture_on_error


class ApplyForm:
    """Handles the Indeed Easy Apply multi-step form."""

    def __init__(
        self,
        page: Page,
        config: dict,
        database: Database,
        question_matcher: QuestionMatcher,
        job_context: dict[str, str] | None = None,
    ):
        self.page = page
        self.config = config
        self.db = database
        self.detector = FormDetector()
        self.filler = FormFiller(config, page, question_matcher, job_context)
        self.question_matcher = question_matcher
        self.job_context = job_context or {}
        self.max_steps = 10  # Safety limit to prevent infinite loops

    async def complete_application(self, job_id: str = "", resume_path: str | None = None) -> bool:
        """Complete the entire Easy Apply form flow.

        Navigates through all form steps, fills fields, and submits.

        Args:
            job_id: Indeed job ID for tracking
            resume_path: Resume PDF to upload when the step requires one

        Returns:
            True if application was submitted successfully
        """
        logger.info("📝 Starting application form...")

        step = 0
        last_filled_signature = ""
        while step < self.max_steps:
            step += 1
            logger.info(f"  Step {step}...")

            if self._skip_requested():
                logger.info("Skip requested by user; aborting current application")
                return False

            # Wait for form content to load
            await self._wait_for_form_content()

            # Check if we've reached the success page
            if await self._is_application_complete():
                logger.info("✅ Application submitted successfully!")
                return True

            # Check for errors
            error = await self._check_for_errors()
            if error:
                logger.error(f"❌ Form error: {error}")
                return False

            if resume_path and await self._looks_like_resume_step():
                if not await self.handle_resume_step(resume_path):
                    return False

            if await self._skip_optional_profile_detail_step():
                await between_actions()
                continue

            if await self._handle_captcha_if_present():
                await between_actions()
                continue

            # Detect and fill form fields on this step
            fields = await self.detector.detect_fields(self.page)
            if fields:
                logger.info(f"  Found {len(fields)} fields to fill")
                field_signature = self._field_signature(fields)
                if field_signature and field_signature == last_filled_signature:
                    logger.info("Repeated form step detected after Continue; skipping refill")
                    advanced = await self._advance_form()
                    if not advanced:
                        if await self._handle_captcha_if_present():
                            await between_actions()
                            continue
                        if await self._is_application_complete():
                            logger.info("âœ… Application submitted successfully!")
                            return True
                        logger.warning("Could not advance repeated form step")
                        break
                    await between_actions()
                    continue

                results = await self.filler.fill_fields(fields)

                if self._skip_requested():
                    logger.info("Skip requested by user during form fill; aborting current application")
                    return False

                # Log results
                for field, success, message in results:
                    if success:
                        logger.debug(f"  ✓ {field.category.name}: {message}")
                    else:
                        logger.warning(f"  ✗ {field.category.name}: {message}")

                if results and all(success for _, success, _ in results):
                    last_filled_signature = field_signature
                else:
                    last_filled_signature = ""

            # Try to advance to the next step
            advanced = await self._advance_form()
            if not advanced:
                if await self._handle_captcha_if_present():
                    await between_actions()
                    continue
                # Check if we're done
                if await self._is_application_complete():
                    logger.info("✅ Application submitted successfully!")
                    return True
                logger.warning("Could not advance form — may be stuck")
                break

            await between_actions()

        logger.error("❌ Hit max form steps — something went wrong")
        return False

    def _skip_requested(self) -> bool:
        agent_id = str(self.job_context.get("agent_id", "")).strip()
        if not agent_id:
            return False
        return REGISTRY.is_skip_requested(agent_id)

    @staticmethod
    def _field_signature(fields: list[FormField]) -> str:
        """Build a compact identity for a form step."""
        parts = [
            f"{field.field_type.name}:{field.label.strip().lower()}:{','.join(field.options).lower()}"
            for field in fields
        ]
        return "|".join(parts)

    async def _looks_like_resume_step(self) -> bool:
        """Return True when the current step is asking for a resume."""
        patterns = [
            re.compile(r"add a resume", re.IGNORECASE),
            re.compile(r"upload a resume", re.IGNORECASE),
            re.compile(r"indeed resume", re.IGNORECASE),
            re.compile(r"build an indeed resume", re.IGNORECASE),
        ]

        try:
            visible_text = await self.page.locator("body").inner_text(timeout=3000)
        except Exception:
            visible_text = ""

        if any(pattern.search(visible_text) for pattern in patterns):
            return True

        try:
            return await self.page.locator('input[type="file"]').count() > 0
        except Exception:
            return False

    async def _skip_optional_profile_detail_step(self) -> bool:
        """Skip Indeed Resume enrichment steps requested inside applications."""
        try:
            body_text = await self.page.locator("body").inner_text(timeout=3000)
        except Exception:
            return False

        if not re.search(
            r"\b(add|review)\s+(education|work experience)\b",
            body_text,
            re.IGNORECASE,
        ):
            return False

        skip_buttons = [
            self.page.get_by_role("button", name=re.compile(r"^skip$", re.IGNORECASE)),
            self.page.get_by_text(re.compile(r"^skip$", re.IGNORECASE)),
        ]
        for locator in skip_buttons:
            try:
                count = await locator.count()
                for idx in range(count):
                    button = locator.nth(idx)
                    if await button.is_visible() and await button.is_enabled():
                        logger.info("Skipping optional Indeed Resume detail step")
                        await button.scroll_into_view_if_needed()
                        await button.click()
                        await self._wait_after_navigation_click("Continue")
                        return True
            except Exception:
                continue

        logger.warning("Optional profile detail step found, but no Skip button was clickable")
        return False

    async def _handle_captcha_if_present(self) -> bool:
        """Pause for manual CAPTCHA solving when Indeed blocks submit."""
        if not await self._captcha_present():
            return False

        agent_id = str(self.job_context.get("agent_id", "")).strip()
        if agent_id:
            REGISTRY.set_state(agent_id, "captcha_wait")
            REGISTRY.set_captcha_wait_count()
            REGISTRY.set_prompt(
                agent_id,
                "Captcha detected in the apply form. Solve it in the browser, then type ok to continue.",
                options=["ok"],
            )
            REGISTRY.append_log(agent_id, "captcha detected in apply form")

        # Bring the active apply tab to front and scroll to the bottom so the
        # captcha/submit section is visible for manual solve.
        await self._focus_and_scroll_to_bottom_for_captcha()

        cc_print(
            "\n[bold yellow]CAPTCHA detected.[/bold yellow] "
            "Solve it in the browser, then confirm to continue."
        )

        # Auto-resume if captcha vanishes quickly on its own.
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

        submit = await self._wait_for_submit_enabled(timeout=90000)
        if submit is not None:
            logger.info("CAPTCHA solved; submitting application")
            if agent_id:
                REGISTRY.set_state(agent_id, "applying")
                REGISTRY.set_captcha_wait_count()
            await submit.scroll_into_view_if_needed()
            previous_step = await self._current_step_fingerprint()
            await submit.click()
            await self._wait_after_navigation_click("Submit your application", previous_step)
            return True

        logger.warning("CAPTCHA still appears unresolved; submit button was not enabled")
        if agent_id:
            REGISTRY.set_captcha_wait_count()
        return False

    async def _focus_and_scroll_to_bottom_for_captcha(self) -> None:
        """Focus current tab and scroll to page end for faster manual captcha solve."""
        try:
            await self.page.bring_to_front()
        except Exception:
            pass

        try:
            await self.page.evaluate(
                """() => {
                    window.scrollTo(0, document.body.scrollHeight);
                }"""
            )
            await self.page.wait_for_timeout(250)
        except Exception:
            pass

    async def _wait_for_submit_enabled(self, timeout: int = 90000):
        """Wait for the final submit button to become clickable after CAPTCHA."""
        deadline = asyncio.get_running_loop().time() + (timeout / 1000)
        submit = self.page.get_by_role(
            "button",
            name=re.compile(r"^(submit your application|submit application|submit)$", re.IGNORECASE),
        )

        while asyncio.get_running_loop().time() < deadline:
            try:
                count = await submit.count()
                for idx in range(count):
                    button = submit.nth(idx)
                    if await button.is_visible() and await button.is_enabled():
                        return button
            except Exception:
                pass
            await asyncio.sleep(1)

        return None

    async def _captcha_present(self) -> bool:
        """Detect visible reCAPTCHA widgets on the current page."""
        selectors = [
            'iframe[src*="recaptcha"]',
            '.g-recaptcha',
            '[title*="reCAPTCHA"]',
            """:has-text("I'm not a robot")""",
            ':has-text("I’m not a robot")',
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

    async def _wait_for_form_content(self, timeout: int = 10000) -> None:
        """Wait for form content to be visible."""
        try:
            # Indeed's apply modal/iframe typically has these containers
            form_selectors = [
                '.ia-BasePage',
                '.ia-FormPage',
                '#ia-container',
                '[data-testid="ia-container"]',
                'form[class*="indeed"]',
                '.indeed-apply-widget',
                'iframe[title*="Apply"]',
            ]

            # Try each selector
            for sel in form_selectors:
                try:
                    await self.page.wait_for_selector(sel, timeout=3000)
                    await self._wait_for_step_ready()
                    return
                except PlaywrightTimeout:
                    continue

            # Check if we're in an iframe
            iframe = self.page.frame_locator('iframe[title*="Apply"], iframe[id*="indeedapply"]')
            try:
                await iframe.locator('input, select, textarea, button').first.wait_for(timeout=5000)
                await self._wait_for_step_ready()
            except Exception:
                pass

        except Exception:
            # Give the page a moment
            await asyncio.sleep(2)

    async def _wait_for_step_ready(self, timeout: int = 20000) -> None:
        """Wait until the apply step has controls or recognizable content."""
        try:
            await self.page.wait_for_function(
                """
                () => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const box = el.getBoundingClientRect();
                        return style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && box.width > 0
                            && box.height > 0;
                    };

                    const text = (document.body.innerText || '').toLowerCase();
                    if (/your application has been submitted|application submitted|you have applied|application sent/.test(text)) {
                        return true;
                    }
                    if (/add a resume|upload a resume|build an indeed resume/.test(text)) {
                        return true;
                    }

                    const fields = [...document.querySelectorAll(
                        'input:not([type="hidden"]), select, textarea'
                    )].filter(visible);
                    if (fields.length > 0) return true;

                    const actions = [...document.querySelectorAll(
                        'button, [role="button"], input[type="submit"], input[type="button"]'
                    )].filter((el) => {
                        if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') {
                            return false;
                        }
                        const label = (
                            el.innerText || el.value || el.getAttribute('aria-label') || ''
                        ).toLowerCase();
                        return /continue|next|review|submit|apply/.test(label)
                            && !/save and close|report/.test(label);
                    });

                    return actions.length > 0;
                }
                """,
                timeout=timeout,
            )
        except PlaywrightTimeout:
            logger.warning("Timed out waiting for apply form content to finish loading")

    async def _advance_form(self) -> bool:
        """Click the Continue/Next/Submit button to advance to the next step.

        Returns:
            True if successfully advanced
        """
        button_names = re.compile(
            r"^(continue|next|review|review your application|"
            r"submit your application|submit application|submit|apply)$",
            re.IGNORECASE,
        )

        # Priority order: Continue first, then Review, then Submit.
        button_selectors = [
            # Continue / Next buttons
            'button:has-text("Continue")',
            'button:has-text("Next")',
            '[role="button"]:has-text("Continue")',
            '[role="button"]:has-text("Next")',
            # Review button (final step before submit)
            'button:has-text("Review")',
            'button:has-text("Review your application")',
            '[role="button"]:has-text("Review")',
            # Submit buttons
            'button:has-text("Submit your application")',
            'button:has-text("Submit application")',
            'button:has-text("Submit")',
            'button:has-text("Apply")',
            # Generic fallbacks
            'button[type="submit"]',
            '[data-testid="ia-continue"]',
        ]

        role_buttons = [
            self.page.get_by_role("button", name=button_names),
            self.page.locator('input[type="submit"], input[type="button"]').filter(has_text=button_names),
        ]
        if await self._click_first_available(role_buttons):
            return True

        for sel in button_selectors:
            try:
                btn = self.page.locator(sel)
                if await btn.count() > 0:
                    # Make sure button is visible and enabled
                    first_btn = btn.first
                    if await first_btn.is_visible() and await first_btn.is_enabled():
                        btn_text = (await first_btn.inner_text()).strip()
                        if not self._is_form_navigation_label(btn_text):
                            continue
                        logger.info(f"  → Clicking: '{btn_text}'")
                        previous_step = await self._current_step_fingerprint()
                        await first_btn.click()
                        await self._wait_after_navigation_click(btn_text, previous_step)
                        return True
            except Exception:
                continue

        # Also check inside iframes
        try:
            frames = self.page.frames
            for frame in frames:
                frame_role_buttons = [
                    frame.get_by_role("button", name=button_names),
                ]
                if await self._click_first_available(frame_role_buttons):
                    return True

                for sel in button_selectors[:6]:
                    try:
                        btn = frame.locator(sel)
                        if await btn.count() > 0:
                            first_btn = btn.first
                            if await first_btn.is_visible() and await first_btn.is_enabled():
                                btn_text = (await first_btn.inner_text()).strip()
                                if not self._is_form_navigation_label(btn_text):
                                    continue
                                logger.info(f"  â†’ Clicking: '{btn_text}'")
                                previous_step = await self._current_step_fingerprint()
                                await first_btn.click()
                                await self._wait_after_navigation_click(btn_text, previous_step)
                                return True
                    except Exception:
                        continue
        except Exception:
            pass

        return False

    async def _click_first_available(self, locators: list) -> bool:
        """Click the first visible, enabled button-like locator."""
        for locator in locators:
            try:
                count = await locator.count()
                for idx in range(count):
                    candidate = locator.nth(idx)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        try:
                            text = (await candidate.inner_text()).strip()
                        except Exception:
                            text = await candidate.get_attribute("value") or ""
                        logger.info(f"  â†’ Clicking: '{text or 'button'}'")
                        if not self._is_form_navigation_label(text):
                            continue
                        await candidate.scroll_into_view_if_needed()
                        previous_step = await self._current_step_fingerprint()
                        await candidate.click()
                        await self._wait_after_navigation_click(text, previous_step)
                        return True
            except Exception:
                continue

        return False

    @staticmethod
    def _is_form_navigation_label(text: str) -> bool:
        """Return True for real form navigation buttons, not preview links."""
        normalized = re.sub(r"\s+", " ", text or "").strip().lower()
        if not normalized:
            return True
        if "preview what the employer sees" in normalized:
            return False
        if "save and close" in normalized or "report" in normalized:
            return False
        return bool(
            re.fullmatch(
                r"(continue|next|review|review your application|"
                r"submit your application|submit application|submit|apply)",
                normalized,
            )
        )

    async def _current_step_fingerprint(self) -> str:
        """Return a compact signature for the visible apply step."""
        try:
            text = await self.page.locator("body").inner_text(timeout=2000)
        except Exception:
            return ""

        text = re.sub(r"\s+", " ", text).strip().lower()
        return text[:2000]

    async def _wait_after_navigation_click(
        self,
        button_text: str,
        previous_step: str = "",
    ) -> None:
        """Wait briefly after Continue/Review/Submit actions."""
        if re.search(r"\bsubmit\b", button_text or "", re.IGNORECASE):
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            for _ in range(10):
                if await self._is_application_complete():
                    return
                await asyncio.sleep(1)
            return

        for _ in range(16):
            if await self._is_application_complete():
                return
            current_step = await self._current_step_fingerprint()
            if previous_step and current_step and current_step != previous_step:
                await asyncio.sleep(1)
                return
            await asyncio.sleep(0.5)

        logger.debug("Step did not visibly change after clicking %r", button_text)

    async def _is_application_complete(self) -> bool:
        """Check if we've reached the success/confirmation page."""
        success_indicators = [
            'text="Your application has been submitted"',
            'text="Application submitted"',
            ':has-text("application has been submitted")',
            ':has-text("You have applied")',
            ':has-text("Application sent")',
            '[data-testid="ia-success"]',
            '.ia-PostApply',
            '.jobsearch-IndeedApplySuccessContainer',
        ]

        for sel in success_indicators:
            try:
                el = self.page.locator(sel)
                if await el.count() > 0:
                    return True
            except Exception:
                continue

        return False

    async def _check_for_errors(self) -> str | None:
        """Check for form validation errors.

        Returns:
            Error message if found, None otherwise
        """
        error_selectors = [
            '.ia-BasePage-errors',
            '[data-testid="ia-error"]',
            '.css-k3fko4',  # Indeed's error styling
            '.error-text',
            '[role="alert"]',
        ]

        for sel in error_selectors:
            try:
                el = self.page.locator(sel)
                if await el.count() > 0:
                    text = (await el.first.inner_text()).strip()
                    if text and "error" in text.lower():
                        return text
            except Exception:
                continue

        return None

    async def handle_resume_step(self, resume_path: str) -> bool:
        """Handle the resume upload step specifically.

        Indeed often has a dedicated step for resume selection/upload.

        Args:
            resume_path: Path to resume PDF file

        Returns:
            True if resume was handled successfully
        """
        path = Path(resume_path).expanduser().resolve()
        if not path.is_file():
            logger.error(f"Resume file not found: {resume_path}")
            return False

        # Look for file input
        file_input = self.page.locator('input[type="file"]')
        if await file_input.count() > 0:
            await file_input.first.set_input_files(str(path))
            logger.info(f"📎 Uploaded resume: {path.name}")
            await between_actions()
            return True

        upload_option = self.page.get_by_text(re.compile(r"upload a resume", re.IGNORECASE)).first
        try:
            if await upload_option.count() > 0 and await upload_option.is_visible():
                logger.info("Selecting Upload a resume")
                async with self.page.expect_file_chooser(timeout=5000) as chooser_info:
                    await upload_option.click()
                chooser = await chooser_info.value
                await chooser.set_files(str(path))
                logger.info(f"Uploaded resume: {path.name}")
                await between_actions()
                return True
        except PlaywrightTimeout:
            logger.debug("Upload resume click did not open a file chooser; checking for file input")
        except Exception as exc:
            logger.debug(f"Upload resume click failed: {exc}")

        file_input = self.page.locator('input[type="file"]')
        if await file_input.count() > 0:
            await file_input.first.set_input_files(str(path))
            logger.info(f"Uploaded resume: {path.name}")
            await between_actions()
            return True

        # Check for "Use my Indeed resume" option
        indeed_resume = self.page.locator(
            ':has-text("Indeed Resume"), :has-text("Use my resume")'
        )
        if await indeed_resume.count() > 0:
            logger.info("📎 Using Indeed's stored resume")
            return True

        logger.warning("Could not find resume upload field")
        return False
