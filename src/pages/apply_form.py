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

from src.handlers.form_detector import FormDetector, FieldCategory, FieldType, FormField
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
        self._submission_confirmed = False
        self._submit_attempted = False
        self._manual_completion_expected = False
        browser_cfg = config.get("bot", {}).get("browser", {})
        extension_cfg = browser_cfg.get("extension", {}) or {}
        self.fast_extension_forms = (
            browser_cfg.get("mode") == "extension"
            and bool(extension_cfg.get("fast_form_mode", True))
        )

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
        loading_retries = 0
        empty_step_retries = 0
        last_filled_signature = ""
        while step < self.max_steps:
            step += 1
            logger.info(f"  Step {step}...")

            if not await self._control_allows_continue():
                return False

            # Confirmation pages intentionally have no form controls. Detect
            # completion before waiting for another editable application step.
            if getattr(self, "_submission_confirmed", False) or await self._is_application_complete():
                logger.info("✅ Application submitted successfully!")
                return True

            # Wait for form content to load
            if not await self._wait_for_form_content():
                if await self._is_application_complete():
                    logger.info("✅ Application submitted successfully!")
                    return True
                if loading_retries < 1:
                    loading_retries += 1
                    step -= 1
                    logger.warning(
                        "Apply form is still loading; waiting once more before asking"
                    )
                    continue
                if await self._prompt_stalled_form():
                    loading_retries = 0
                    step -= 1
                    continue
                logger.info("Application form was skipped after it failed to become ready")
                self.job_context["stalled_form_skipped"] = True
                return False
            loading_retries = 0

            if not await self._control_allows_continue():
                return False

            # Check if we've reached the success page
            if await self._is_application_complete():
                logger.info("✅ Application submitted successfully!")
                return True

            # Check for errors
            error = await self._check_for_errors()
            if error:
                logger.warning("Form needs manual attention: %s", error)
                if await self._request_manual_intervention(
                    f"Indeed is showing this validation message:\n\n{error}"
                ):
                    step -= 1
                    continue
                return False

            resume_handled = False
            if resume_path and await self._looks_like_resume_step():
                if not await self.handle_resume_step(resume_path):
                    if await self._request_manual_intervention(
                        "The bot could not upload or select the resume. "
                        "Please complete the resume step manually."
                    ):
                        step -= 1
                        continue
                    return False
                resume_handled = True

            if await self._skip_optional_profile_detail_step():
                await self._pause_between_actions()
                continue

            if await self._handle_captcha_if_present():
                await self._pause_between_actions()
                continue

            requirements_action = await self._handle_employer_requirements_warning()
            if requirements_action is not None:
                if not requirements_action:
                    return False
                await self._pause_between_actions()
                continue

            # Detect and fill form fields on this step
            fields = await self.detector.detect_fields(self.page)
            if resume_handled:
                # handle_resume_step already populated this control. Re-uploading
                # can restart Indeed's asynchronous resume processing.
                fields = [
                    field for field in fields
                    if field.field_type != FieldType.FILE_UPLOAD
                ]
            if fields:
                empty_step_retries = 0
                logger.info(f"  Found {len(fields)} fields to fill")
                field_signature = self._field_signature(fields)
                if field_signature and field_signature == last_filled_signature:
                    logger.warning("The same form step remained after Continue")
                    if await self._request_manual_intervention(
                        "Indeed kept the same application step after Continue. "
                        "Please resolve any highlighted field or validation message."
                    ):
                        last_filled_signature = ""
                        step -= 1
                        continue
                    return False

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

                failed_results = [
                    (field, message)
                    for field, success, message in results
                    if not success
                ]
                if failed_results:
                    details = "\n".join(
                        f"- {field.label or field.category.name}: {message}"
                        for field, message in failed_results
                    )
                    if await self._request_manual_intervention(
                        "The bot could not complete these fields:\n\n" + details
                    ):
                        last_filled_signature = ""
                        step -= 1
                        continue
                    return False

                if results:
                    last_filled_signature = field_signature
                else:
                    last_filled_signature = ""

            # Try to advance to the next step
            advanced = await self._advance_form()
            if not advanced:
                if await self._handle_captcha_if_present():
                    await self._pause_between_actions()
                    continue
                # Check if we're done
                if await self._is_application_complete():
                    logger.info("✅ Application submitted successfully!")
                    return True
                if not fields and empty_step_retries < 2:
                    empty_step_retries += 1
                    step -= 1
                    logger.warning(
                        "Application controls are not ready yet; retrying empty step (%s/2)",
                        empty_step_retries,
                    )
                    await self.page.wait_for_timeout(
                        500 if self.fast_extension_forms else 1500
                    )
                    continue
                reason = (
                    "No usable application controls were detected."
                    if not fields
                    else "The bot could not find or activate Continue, Review, or Submit."
                )
                logger.warning("%s Requesting manual help.", reason)
                if await self._request_manual_intervention(reason):
                    empty_step_retries = 0
                    last_filled_signature = ""
                    step -= 1
                    continue
                return False

            empty_step_retries = 0
            await self._pause_between_actions()

        # Do not abandon the current job at the safety limit. Automation stops
        # clicking here; the user can finish the remaining steps manually, and
        # Continue only re-checks for Indeed's submission confirmation.
        while True:
            if await self._is_application_complete():
                logger.info("✅ Application submitted successfully after manual completion!")
                return True
            if not await self._request_manual_intervention(
                "The automated step safety limit was reached. Please finish and "
                "submit the current application manually, then click Continue."
            ):
                return False

    def _skip_requested(self) -> bool:
        agent_id = str(self.job_context.get("agent_id", "")).strip()
        if not agent_id:
            return False
        return REGISTRY.is_skip_requested(agent_id)

    async def _control_allows_continue(self) -> bool:
        """Honor dashboard controls while an application form is active."""
        agent_id = str(self.job_context.get("agent_id", "")).strip()
        if not agent_id:
            return True
        if REGISTRY.is_stopped(agent_id):
            logger.info("Stop requested by user; aborting current application")
            return False
        if REGISTRY.consume_focus_flag(agent_id):
            try:
                await self.page.bring_to_front()
                REGISTRY.append_log(agent_id, "brought browser tab to front")
            except Exception as exc:
                REGISTRY.append_log(agent_id, f"focus failed: {exc}")
        if not await REGISTRY.wait_if_paused(agent_id):
            logger.info("Stopped while application was paused")
            return False
        if self._skip_requested():
            logger.info("Skip requested by user; aborting current application")
            return False
        return True

    async def _pause_between_actions(self) -> None:
        """Use a short deterministic pause for the extension's direct DOM bridge."""
        if self.fast_extension_forms:
            await self.page.wait_for_timeout(250)
            return
        await between_actions()

    async def _handle_employer_requirements_warning(self) -> bool | None:
        """Ask before proceeding past Indeed's unmet-requirements warning.

        Returns None when this is not the warning page, True after the user
        chooses Apply anyway, and False when they choose to stop.
        """
        try:
            body_text = await self.page.locator("body").inner_text(timeout=3000)
        except Exception:
            return None

        normalized = re.sub(r"\s+", " ", body_text).strip()
        if not re.search(
            r"(?:don.t|do not) meet (?:these|the) employer requirements",
            normalized,
            re.IGNORECASE,
        ):
            return None

        requirement = ""
        for line in body_text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if re.search(r"\(required\)|\brequired\b", line, re.IGNORECASE):
                requirement = line
                break

        question = (
            "Indeed says your answers may not meet the employer's requirements."
            + (f"\n\nRequirement: {requirement}" if requirement else "")
            + "\n\nDo you want to apply anyway?"
        )
        agent_id = str(self.job_context.get("agent_id", "")).strip() or None
        answer = await self.question_matcher.prompt_user(
            question,
            options=["Apply anyway", "Return to job search"],
            agent_id=agent_id,
        )
        choice = re.sub(r"\s+", " ", answer or "").strip().lower()
        if choice not in {"apply anyway", "apply", "yes", "y", "1"}:
            logger.info("User declined to apply after the employer-requirements warning")
            self.job_context["requirements_declined"] = True
            return False

        apply_anyway = re.compile(r"^apply anyway$", re.IGNORECASE)
        locators = [
            self.page.get_by_role("link", name=apply_anyway),
            self.page.get_by_role("button", name=apply_anyway),
            self.page.get_by_text(apply_anyway, exact=True),
        ]
        previous_step = await self._current_step_fingerprint()
        for locator in locators:
            try:
                count = await locator.count()
                for idx in range(count):
                    candidate = locator.nth(idx)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        logger.info("User confirmed employer-requirements warning; applying anyway")
                        await candidate.scroll_into_view_if_needed()
                        await candidate.click()
                        await self._wait_after_navigation_click("Apply anyway", previous_step)
                        return True
            except Exception:
                continue

        logger.warning("Apply anyway was selected, but its link was not clickable")
        return False

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

        # Let the orchestrator apply a longer cooldown before it touches the
        # next job. The current application remains active until this CAPTCHA
        # is solved or the user explicitly skips/stops it.
        self.job_context["captcha_encountered"] = True

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
            self._submit_attempted = True
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

    async def _wait_for_form_content(self, timeout: int = 20000) -> bool:
        """Wait until the apply step has usable fields or navigation controls."""
        # Extension mode has a native 100 ms polling trigger across every
        # permitted frame. Use it immediately: waiting for a container first
        # adds up to five seconds even when fields are already interactive.
        if self.fast_extension_forms:
            return await self._wait_for_step_ready(timeout=timeout)

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

            # One combined wait avoids paying a separate timeout for every
            # selector, which is especially costly across extension frames.
            try:
                await self.page.wait_for_selector(
                    ", ".join(form_selectors),
                    timeout=min(timeout, 5000),
                )
                return await self._wait_for_step_ready(timeout=timeout)
            except Exception:
                pass

            # Check if we're in an iframe
            iframe = self.page.frame_locator('iframe[title*="Apply"], iframe[id*="indeedapply"]')
            try:
                await iframe.locator('input, select, textarea, button').first.wait_for(timeout=5000)
                return await self._wait_for_step_ready(timeout=timeout)
            except Exception:
                pass

        except Exception:
            # Give the page a moment
            await asyncio.sleep(2)
        return False

    async def _wait_for_step_ready(self, timeout: int = 20000) -> bool:
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
                    if (/your application (?:has been|was) submitted|application (?:submitted|sent|complete)|you(?: have|'ve) applied|successfully applied|your application was sent/.test(text)) {
                        return true;
                    }
                    if (/add a resume|upload a resume|build an indeed resume/.test(text)) {
                        return true;
                    }
                    if (/(?:don.t|do not) meet (?:these|the) employer requirements/.test(text)) {
                        return true;
                    }
                    if ([...document.querySelectorAll('iframe')].some((el) =>
                        visible(el) && /apply/i.test(el.title || el.id || el.name || '')
                    )) return true;

                    const fields = [...document.querySelectorAll(
                        'input:not([type="hidden"]), select, textarea'
                    )].filter(visible);
                    if (fields.length > 0) return true;

                    const actions = [...document.querySelectorAll(
                        'button, a, [role="button"], input[type="submit"], input[type="button"]'
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
            return True
        except PlaywrightTimeout:
            logger.warning("Timed out waiting for apply form content to finish loading")
            return False

    async def _prompt_stalled_form(self) -> bool:
        """Ask whether to keep waiting when Indeed leaves the form on a spinner."""
        return await self._request_manual_intervention(
            "Indeed's application form is still loading and has no usable "
            "fields. You may wait, reload the application area, or complete "
            "the visible step manually."
        )

    async def _request_manual_intervention(self, reason: str) -> bool:
        """Keep the current application open until the user fixes or skips it."""
        agent_id = str(self.job_context.get("agent_id", "")).strip()
        message = (
            "Manual help is needed on the current application.\n\n"
            f"{reason}\n\n"
            "Complete the required action in the Indeed automation tab without "
            "closing it, then click Continue. The bot will re-read this same step."
        )

        try:
            await self.page.bring_to_front()
        except Exception:
            pass

        if agent_id:
            REGISTRY.set_state(agent_id, "manual_wait")
            REGISTRY.append_log(agent_id, f"manual help requested: {reason}")

        # The user may press the final Submit button while the bot is paused.
        # Allow textual confirmation copy to count only after that explicit
        # hand-off (or after an automated submit attempt), never on an untouched
        # review page where similar wording may be explanatory text.
        self._manual_completion_expected = True

        answer = await self.question_matcher.prompt_user(
            message,
            options=["Continue", "Skip job"],
            agent_id=agent_id or None,
        )
        normalized = re.sub(r"\s+", " ", answer or "").strip().lower()

        if agent_id and not REGISTRY.is_stopped(agent_id):
            REGISTRY.set_state(agent_id, "applying")

        if normalized in {"continue", "resume", "done", "ok", "yes", "y"}:
            if agent_id:
                REGISTRY.append_log(agent_id, "manual action confirmed; re-reading current step")
            return True

        if not (agent_id and REGISTRY.is_stopped(agent_id)):
            self.job_context["manual_intervention_skipped"] = True
            if agent_id:
                REGISTRY.append_log(agent_id, "manual intervention skipped by user")
        return False

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

        # Keep each action in an explicit priority group. A combined role
        # locator uses DOM order, which can select a stale Continue control
        # before the visible final Submit control on Indeed's review page.
        role_buttons = [
            self.page.get_by_role(
                "button",
                name=re.compile(
                    r"^(submit your application|submit application|submit)$",
                    re.IGNORECASE,
                ),
            ),
            self.page.locator('input[type="submit"]').filter(has_text=button_names),
            self.page.get_by_role(
                "button",
                name=re.compile(r"^(review|review your application)$", re.IGNORECASE),
            ),
            self.page.get_by_role(
                "button",
                name=re.compile(r"^(continue|next)$", re.IGNORECASE),
            ),
            self.page.locator('input[type="button"]').filter(has_text=button_names),
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
                        btn_text = await self._button_label(first_btn)
                        if not self._is_form_navigation_label(btn_text):
                            continue
                        logger.info(f"  → Clicking: '{btn_text}'")
                        previous_step = await self._current_step_fingerprint()
                        if re.search(r"\bsubmit\b", btn_text or "", re.IGNORECASE):
                            self._submit_attempted = True
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

                for sel in button_selectors:
                    try:
                        btn = frame.locator(sel)
                        if await btn.count() > 0:
                            first_btn = btn.first
                            if await first_btn.is_visible() and await first_btn.is_enabled():
                                btn_text = await self._button_label(first_btn)
                                if not self._is_form_navigation_label(btn_text):
                                    continue
                                logger.info(f"  â†’ Clicking: '{btn_text}'")
                                previous_step = await self._current_step_fingerprint()
                                if re.search(r"\bsubmit\b", btn_text or "", re.IGNORECASE):
                                    self._submit_attempted = True
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
                        text = await self._button_label(candidate)
                        logger.info(f"  â†’ Clicking: '{text or 'button'}'")
                        if not self._is_form_navigation_label(text):
                            continue
                        await candidate.scroll_into_view_if_needed()
                        previous_step = await self._current_step_fingerprint()
                        if re.search(r"\bsubmit\b", text or "", re.IGNORECASE):
                            self._submit_attempted = True
                        await candidate.click()
                        await self._wait_after_navigation_click(text, previous_step)
                        return True
            except Exception:
                continue

        return False

    @staticmethod
    async def _button_label(candidate) -> str:
        """Read a button label from text, value, or its accessible name."""
        try:
            text = (await candidate.inner_text()).strip()
        except Exception:
            text = ""
        if text:
            return text
        for attribute in ("value", "aria-label", "title"):
            try:
                text = (await candidate.get_attribute(attribute) or "").strip()
            except Exception:
                text = ""
            if text:
                return text
        return ""

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
        if self.fast_extension_forms:
            if re.search(r"\bsubmit\b", button_text or "", re.IGNORECASE):
                deadline = asyncio.get_running_loop().time() + 8
                while asyncio.get_running_loop().time() < deadline:
                    if await self._is_application_complete():
                        self._submission_confirmed = True
                        return
                    await asyncio.sleep(0.2)
                return
            await self.page.wait_for_timeout(400)
            return

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
        if getattr(self, "_submission_confirmed", False):
            return True

        success_indicators = [
            '[data-testid="ia-success"]',
            '[data-testid*="postApply" i]',
            '[data-testid*="application-success" i]',
            '.ia-PostApply',
            '.jobsearch-IndeedApplySuccessContainer',
            '[class*="PostApply"]',
            '[class*="ApplicationSuccess"]',
        ]

        for sel in success_indicators:
            try:
                el = self.page.locator(sel)
                count = await el.count()
                for index in range(count):
                    if await el.nth(index).is_visible():
                        return True
            except Exception:
                continue

        confirmation_is_expected = bool(
            getattr(self, "_submit_attempted", False)
            or getattr(self, "_manual_completion_expected", False)
        )
        if confirmation_is_expected:
            confirmation_pattern = re.compile(
                r"your application (?:has been|was) submitted|"
                r"application (?:submitted|sent|complete)|"
                r"you(?: have|'ve|’ve) applied|successfully applied|"
                r"your application was sent",
                re.IGNORECASE,
            )
            try:
                body_text = await self.page.locator("body").inner_text(timeout=2000)
                if confirmation_pattern.search(body_text or ""):
                    return True
            except Exception:
                pass

        if confirmation_is_expected:
            try:
                applied_status = self.page.get_by_text(
                    re.compile(r"^(applied|application submitted|application sent)$", re.IGNORECASE),
                    exact=True,
                )
                count = await applied_status.count()
                for index in range(count):
                    if await applied_status.nth(index).is_visible():
                        return True
            except Exception:
                pass

        current_url = str(getattr(self.page, "url", "")).lower()
        if confirmation_is_expected and any(
            marker in current_url
            for marker in ("postapply", "application-success", "confirmation")
        ):
            return True

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
                count = await el.count()
                for index in range(count):
                    candidate = el.nth(index)
                    if not await candidate.is_visible():
                        continue
                    text = (await candidate.inner_text()).strip()
                    if text:
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
            await self._pause_between_actions()
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
                await self._pause_between_actions()
                return True
        except PlaywrightTimeout:
            logger.debug("Upload resume click did not open a file chooser; checking for file input")
        except Exception as exc:
            logger.debug(f"Upload resume click failed: {exc}")

        file_input = self.page.locator('input[type="file"]')
        if await file_input.count() > 0:
            await file_input.first.set_input_files(str(path))
            logger.info(f"Uploaded resume: {path.name}")
            await self._pause_between_actions()
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
