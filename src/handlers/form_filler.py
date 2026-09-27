# src/handlers/form_filler.py
"""Fills form fields based on user config and detected field types.

Matches each detected :class:`FormField` to the appropriate value from
the user's configuration, then interacts with the page element to set
the value — typing text humanistically, selecting dropdown options,
checking radio buttons, and uploading files.
"""
from __future__ import annotations

from datetime import datetime
import re
from pathlib import Path
from typing import Any, TYPE_CHECKING

from rich.console import Console

from src.handlers.form_detector import FieldCategory, FieldType, FormField
from src.handlers.question_matcher import QuestionMatcher
from src.utils.delay import type_like_human
from src.utils.logger import logger

if TYPE_CHECKING:
    from playwright.async_api import Page

console = Console()


# ---------------------------------------------------------------------------
# Fill result type
# ---------------------------------------------------------------------------

FillResult = tuple[FormField, bool, str]
"""``(field, success, message)`` for each attempted fill."""


# ---------------------------------------------------------------------------
# FormFiller
# ---------------------------------------------------------------------------

class FormFiller:
    """Fills an Indeed Easy Apply form step using the supplied config.

    Args:
        config: The user's configuration dictionary, expected to have
            keys like ``personal``, ``resume``, ``preferences``, etc.
        page: The active Playwright page.
        question_matcher: A :class:`QuestionMatcher` for screening Qs.

    Usage::

        filler = FormFiller(config, page, question_matcher)
        results = await filler.fill_fields(detected_fields)
        for field, ok, msg in results:
            print(field.label, "✓" if ok else "✗", msg)
    """

    def __init__(
        self,
        config: dict[str, Any],
        page: Page,
        question_matcher: QuestionMatcher,
        job_context: dict[str, str] | None = None,
    ) -> None:
        self._config = config
        self._page = page
        self._qm = question_matcher
        self._job_context = job_context or {}
        self._agent_id = (self._job_context.get("agent_id") or "").strip()
        self._transient_answers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Config value resolution
    # ------------------------------------------------------------------

    def _resolve_value(self, category: FieldCategory) -> str | None:
        """Look up the config value for a given :class:`FieldCategory`.

        Returns:
            The config string, or ``None`` if no mapping exists.
        """
        personal: dict[str, str] = self._config.get("personal", {})
        preferences: dict[str, str] = self._config.get("preferences", {})
        resume_cfg: dict[str, str] = self._config.get("resume", {})

        mapping: dict[FieldCategory, str | None] = {
            FieldCategory.FIRST_NAME: personal.get("first_name"),
            FieldCategory.LAST_NAME: personal.get("last_name"),
            FieldCategory.FULL_NAME: self._build_full_name(personal),
            FieldCategory.EMAIL: personal.get("email"),
            FieldCategory.PHONE: personal.get("phone"),
            FieldCategory.LOCATION: personal.get("location"),
            FieldCategory.ADDRESS: personal.get("address"),
            FieldCategory.CITY: personal.get("city"),
            FieldCategory.STATE: personal.get("state"),
            FieldCategory.ZIP_CODE: personal.get("zip_code"),
            FieldCategory.LINKEDIN: personal.get("linkedin"),
            FieldCategory.WEBSITE: personal.get("website"),
            FieldCategory.COUNTRY: personal.get("country"),
            FieldCategory.YEARS_EXPERIENCE: preferences.get("years_experience"),
            FieldCategory.EDUCATION: preferences.get("education"),
            FieldCategory.SALARY_EXPECTATION: preferences.get("salary_expectation"),
            FieldCategory.START_DATE: preferences.get("start_date"),
            FieldCategory.WORK_AUTHORIZATION: preferences.get("work_authorization"),
            FieldCategory.WILLING_TO_RELOCATE: preferences.get("willing_to_relocate"),
            FieldCategory.RESUME: resume_cfg.get("default_path"),
            FieldCategory.COVER_LETTER: resume_cfg.get("cover_letter_path"),
        }
        return mapping.get(category)

    @staticmethod
    def _build_full_name(personal: dict[str, str]) -> str | None:
        """Combine first + last name if both are present."""
        first = personal.get("first_name", "")
        last = personal.get("last_name", "")
        full = f"{first} {last}".strip()
        return full or None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fill_fields(self, fields: list[FormField]) -> list[FillResult]:
        """Fill every field in *fields* and return per-field results.

        Args:
            fields: Output of :meth:`FormDetector.detect_fields`.

        Returns:
            A list of ``(field, success, message)`` tuples.
        """
        results: list[FillResult] = []

        for field in fields:
            try:
                result = await self._fill_single(field)
                results.append(result)
            except Exception as exc:
                msg = f"Exception: {exc}"
                logger.error(
                    "[error]✗ FILL ERROR[/error]  %s: %s",
                    field.label or field.selector,
                    msg,
                )
                results.append((field, False, msg))

        # Summary
        ok_count = sum(1 for _, ok, _ in results if ok)
        console.print(
            f"\n[bold]Fill summary:[/bold] "
            f"[green]{ok_count}[/green]/{len(results)} fields filled successfully."
        )
        return results

    # ------------------------------------------------------------------
    # Single-field fill dispatch
    # ------------------------------------------------------------------

    async def _fill_single(self, field: FormField) -> FillResult:
        """Fill a single field, dispatching by type and category."""
        # Screening questions are special – delegate to QuestionMatcher
        if (
            field.category == FieldCategory.SCREENING_QUESTION
            or (field.field_type == FieldType.RADIO and field.options)
            or (
                field.category == FieldCategory.UNKNOWN
                and field.options
                and field.field_type in {
                    FieldType.CHECKBOX,
                    FieldType.SELECT,
                    FieldType.COMBOBOX,
                }
            )
        ):
            return await self._fill_screening_question(field)

        # File uploads
        if field.field_type == FieldType.FILE_UPLOAD:
            return await self._fill_file_upload(field)

        if field.field_type == FieldType.CHECKBOX and field.category == FieldCategory.UNKNOWN:
            return (field, True, "Left unchecked")

        if field.category == FieldCategory.SALARY_EXPECTATION:
            return await self._fill_salary_question(field)

        # Resolve the value from config
        value = self._resolve_value(field.category)
        if value is None:
            if self._should_prompt_when_config_missing(field):
                return await self._fill_screening_question(field)

            msg = f"No config value for category {field.category.name}"
            logger.warning(
                "[warning]⚠ NO VALUE[/warning]  %s: %s",
                field.label,
                msg,
            )
            return (field, False, msg)

        match field.field_type:
            case FieldType.TEXT_INPUT | FieldType.EMAIL | FieldType.PHONE | FieldType.NUMBER | FieldType.DATE:
                # Skip if Indeed already pre-filled this field
                if await self._text_field_already_filled(field):
                    logger.info(
                        "[dim]⏭ SKIPPED (pre-filled)[/dim]  %s",
                        field.label,
                    )
                    return (field, True, "Already filled by Indeed – skipped")
                if field.field_type == FieldType.PHONE:
                    await self._select_phone_country(value)
                    value = self._normalize_phone_for_input(value)
                elif field.category == FieldCategory.COUNTRY:
                    label_lower = (field.label or "").lower()
                    if "code" in label_lower or "dial" in label_lower:
                        value = self._map_country_to_code(value)
                result = await self._fill_text(field, value)
            case FieldType.TEXTAREA:
                if await self._text_field_already_filled(field):
                    logger.info(
                        "[dim]⏭ SKIPPED (pre-filled)[/dim]  %s",
                        field.label,
                    )
                    return (field, True, "Already filled by Indeed – skipped")
                result = await self._fill_text(field, value)
            case FieldType.SELECT | FieldType.COMBOBOX:
                if await self._select_field_already_filled(field):
                    logger.info(
                        "[dim]⏭ SKIPPED (pre-filled)[/dim]  %s",
                        field.label,
                    )
                    return (field, True, "Already filled by Indeed – skipped")
                result = await self._fill_select(field, value)
            case FieldType.RADIO:
                if await self._radio_already_checked(field):
                    logger.info(
                        "[dim]⏭ SKIPPED (pre-filled)[/dim]  %s",
                        field.label,
                    )
                    return (field, True, "Already filled by Indeed – skipped")
                result = await self._fill_radio(field, value)
            case FieldType.CHECKBOX:
                if await self._checkbox_already_checked(field):
                    logger.info(
                        "[dim]⏭ SKIPPED (pre-filled)[/dim]  %s",
                        field.label,
                    )
                    return (field, True, "Already filled by Indeed – skipped")
                result = await self._fill_checkbox(field, value)
            case _:
                msg = f"Unsupported field type: {field.field_type.name}"
                logger.warning(
                    "[warning]⚠ UNSUPPORTED[/warning]  %s: %s",
                    field.label,
                    msg,
                )
                result = (field, False, msg)

        if not result[1] and field.required:
            logger.info("Programmatic fill failed for '%s', falling back to user prompt", field.label)
            return await self._fill_screening_question(field)
            
        return result

    # ------------------------------------------------------------------
    # Pre-fill detection helpers
    # ------------------------------------------------------------------

    async def _text_field_already_filled(self, field: FormField) -> bool:
        """Return True if a text/textarea/email/phone/number field already has a value.

        Indeed sometimes pre-fills profile data (name, email, phone…).
        We detect this by reading the element's current value via JS.  If the
        field is non-empty we leave it alone rather than overwriting it.
        """
        if not field.selector:
            return False
        try:
            locator = self._page.locator(field.selector).first
            current: str = await locator.input_value(timeout=3000)
            return bool((current or "").strip())
        except Exception:
            return False

    async def _select_field_already_filled(self, field: FormField) -> bool:
        """Return True if a <select> already has a meaningful (non-placeholder) value."""
        if not field.selector:
            return False
        try:
            locator = self._page.locator(field.selector).first
            if field.field_type == FieldType.COMBOBOX:
                selected_text = (
                    await locator.get_attribute("aria-valuetext")
                    or await locator.inner_text()
                    or ""
                ).strip()
                return bool(
                    selected_text
                    and not re.match(
                        r"^(select|choose|please select|please choose)\b",
                        selected_text,
                        re.IGNORECASE,
                    )
                )
            selected_value: str = await locator.input_value(timeout=3000)
            if not selected_value or not selected_value.strip():
                return False
            # Consider placeholder-like values as "not filled"
            placeholder_patterns = re.compile(
                r"^(select|choose|please select|please choose|--|none|n/a)\b",
                re.IGNORECASE,
            )
            # Also check the visible label text
            selected_text: str = await self._page.evaluate(
                """
                (sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return '';
                    const opt = el.options[el.selectedIndex];
                    return opt ? opt.text : '';
                }
                """,
                field.selector,
            )
            selected_text = (selected_text or "").strip()
            if placeholder_patterns.match(selected_text):
                return False
            # If value is 0 / empty string it is likely the placeholder option
            if selected_value.strip() in {"", "0"}:
                return False
            return bool(selected_text)
        except Exception:
            return False

    async def _radio_already_checked(self, field: FormField) -> bool:
        """Return True if any radio button in the group is already checked."""
        if not field.selector:
            return False
        try:
            radios = self._page.locator(field.selector)
            count = await radios.count()
            for i in range(count):
                if await radios.nth(i).is_checked():
                    return True
            return False
        except Exception:
            return False

    async def _checkbox_already_checked(self, field: FormField) -> bool:
        """Return True if the (single) checkbox is already checked."""
        if not field.selector:
            return False
        try:
            checkbox = self._page.locator(field.selector).first
            return await checkbox.is_checked()
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Type-specific fill methods
    # ------------------------------------------------------------------

    async def _fill_text(self, field: FormField, value: str) -> FillResult:
        """Fill a text / email / phone / number / textarea field."""
        error = await self._type_field(field, value)
        if error:
            return (field, False, error)
            
        # Verify it actually stuck (company sites sometimes wipe inputs if validation fails)
        try:
            current_value = await self._page.locator(field.selector).first.input_value(timeout=1000)
            if not current_value.strip():
                return (field, False, "Field remains empty after typing (React/Vue issue?)")
        except Exception:
            pass

        logger.info(
            "[success]✓ TYPED[/success]  %s: %r",
            field.label,
            value,
        )
        return (field, True, f"Typed: {value}")

    async def _type_field(self, field: FormField, value: str) -> str | None:
        """Type into a field, guarding against stale/empty selectors."""
        if not field.selector:
            return "Missing selector; page likely changed while reading fields"

        try:
            await self._page.locator(field.selector).first.wait_for(
                state="visible",
                timeout=5000,
            )
        except Exception as exc:
            return f"Field not visible before typing: {exc}"

        delays = self._config.get("bot", {}).get("delays", {})
        min_ms = float(delays.get("typing_min_ms", 12))
        max_ms = float(delays.get("typing_max_ms", 35))
        if max_ms < min_ms:
            min_ms, max_ms = max_ms, min_ms

        await type_like_human(
            self._page,
            field.selector,
            value,
            min_keystroke_ms=min_ms,
            max_keystroke_ms=max_ms,
        )
        try:
            await self._page.keyboard.press("Escape")
            await self._page.wait_for_timeout(300)
        except Exception:
            pass
        return None

    @staticmethod
    def _normalize_phone_for_input(value: str) -> str:
        """Return the local number portion for split country-code phone widgets."""
        stripped = value.strip()
        match = re.match(r"^\+?\d{1,3}[\s.-]+(.+)$", stripped)
        if match:
            stripped = match.group(1)
        return re.sub(r"\D+", "", stripped) or value

    @staticmethod
    def _map_country_to_code(country: str) -> str:
        """Map a country name to its 2-letter ISO code."""
        if not country:
            return ""
        mapping = {
            "pakistan": "PK",
            "united states": "US",
            "usa": "US",
            "united kingdom": "GB",
            "uk": "GB",
            "india": "IN",
            "canada": "CA",
            "australia": "AU",
            "germany": "DE",
            "france": "FR",
            "united arab emirates": "AE",
            "uae": "AE",
            "spain": "ES",
            "italy": "IT",
            "netherlands": "NL",
            "brazil": "BR",
        }
        return mapping.get(country.strip().lower(), country.strip())

    async def _select_phone_country(self, phone: str) -> None:
        """Best-effort selection for Indeed's split country-code phone widget."""
        dial_code = self._extract_dial_code(phone)
        country = self._config.get("personal", {}).get("country", "")
        if not dial_code and not country:
            return

        button_patterns = [
            r"\+\d{1,4}",
            r"country",
            r"calling code",
        ]
        buttons = [
            self._page.get_by_role("button", name=re.compile(pattern, re.IGNORECASE))
            for pattern in button_patterns
        ]

        opened = False
        for buttons_locator in buttons:
            try:
                count = await buttons_locator.count()
                for idx in range(count):
                    button = buttons_locator.nth(idx)
                    if await button.is_visible() and await button.is_enabled():
                        await button.click()
                        await self._page.wait_for_timeout(300)
                        opened = True
                        break
                if opened:
                    break
            except Exception:
                continue

        if not opened:
            return

        option_patterns = [
            re.escape(country) if country else "",
            re.escape(dial_code) if dial_code else "",
        ]
        option_patterns = [pattern for pattern in option_patterns if pattern]

        for pattern in option_patterns:
            try:
                option = self._page.get_by_role(
                    "option",
                    name=re.compile(pattern, re.IGNORECASE),
                )
                if await self._click_visible_option(option):
                    logger.info("✓ PHONE COUNTRY  selected %s", country or dial_code)
                    return
            except Exception:
                pass

            try:
                option = self._page.get_by_text(
                    re.compile(pattern, re.IGNORECASE),
                    exact=False,
                )
                if await self._click_visible_option(option):
                    logger.info("✓ PHONE COUNTRY  selected %s", country or dial_code)
                    return
            except Exception:
                pass

    async def _click_visible_option(self, locator: Any) -> bool:
        """Click the first visible option-like locator."""
        count = await locator.count()
        for idx in range(count):
            item = locator.nth(idx)
            if await item.is_visible():
                await item.click()
                await self._page.wait_for_timeout(300)
                return True
        return False

    @staticmethod
    def _extract_dial_code(phone: str) -> str:
        """Extract a leading international dial code from a phone number."""
        match = re.match(r"^\s*(\+\d{1,4})", phone)
        return match.group(1) if match else ""

    async def _fill_select(self, field: FormField, value: str) -> FillResult:
        """Select the best-matching option in a ``<select>`` dropdown.

        Tries exact match first, then case-insensitive substring match,
        then falls back to the first non-empty option.
        """
        selected = await self._choose_select_option(field, value)

        if not selected and field.category == FieldCategory.COUNTRY:
            iso_code = self._map_country_to_code(value)
            if iso_code != value:
                selected = await self._choose_select_option(field, iso_code)

        if selected:
            # Verify the select isn't pointing to a placeholder
            if not await self._select_field_already_filled(field):
                return (field, False, "Select field reverted to empty/placeholder after selection")

            logger.info(
                "[success]✓ SELECTED[/success]  %s: %r",
                field.label,
                selected,
            )
            return (field, True, f"Selected: {selected}")

        # We no longer fallback to an arbitrary option. If we can't select,
        # we return False so the caller can fallback to prompting the user.
        return (field, False, "No matching select option found")

    async def _fill_radio(self, field: FormField, value: str) -> FillResult:
        """Click the radio button whose label best matches *value*."""
        best_option = self._best_option_match(value, field.options)

        if best_option is not None:
            idx = field.options.index(best_option)
            # Click the nth radio in the group
            radios = self._page.locator(field.selector)
            await radios.nth(idx).check()
            logger.info(
                "[success]✓ RADIO[/success]  %s: selected %r",
                field.label,
                best_option,
            )
            return (field, True, f"Radio selected: {best_option}")

        msg = f"No matching radio option for {value!r}"
        logger.warning("[warning]⚠ NO MATCH[/warning]  %s: %s", field.label, msg)
        return (field, False, msg)

    async def _fill_checkbox(self, field: FormField, value: str) -> FillResult:
        """Check checkboxes whose labels match the expected answer.

        *value* can be ``"yes"``/``"true"`` for single checkboxes,
        or a comma-separated list of labels for multi-checkbox groups.
        """
        checkboxes = self._page.locator(field.selector)
        count = await checkboxes.count()

        # Single checkbox (e.g., "I agree")
        if count == 1:
            if value.lower() in {"yes", "true", "1", "agree"}:
                await checkboxes.first.check()
                logger.info(
                    "[success]✓ CHECKED[/success]  %s",
                    field.label,
                )
                return (field, True, "Checked")
            else:
                logger.debug("Left unchecked: %s (value=%r)", field.label, value)
                return (field, True, "Left unchecked")

        # Multi-checkbox – check all whose labels appear in value
        desired = {v.strip().lower() for v in value.split(",")}
        checked_labels: list[str] = []
        for idx, option_label in enumerate(field.options):
            if option_label.lower() in desired:
                await checkboxes.nth(idx).check()
                checked_labels.append(option_label)

        if checked_labels:
            logger.info(
                "[success]✓ CHECKED[/success]  %s: %s",
                field.label,
                checked_labels,
            )
            return (field, True, f"Checked: {checked_labels}")

        msg = f"No matching checkbox option for {value!r}"
        logger.warning("[warning]⚠ NO MATCH[/warning]  %s: %s", field.label, msg)
        return (field, False, msg)

    async def _fill_file_upload(self, field: FormField) -> FillResult:
        """Upload a file (resume or cover letter)."""
        file_path_str = self._resolve_value(field.category)
        if not file_path_str:
            msg = f"No file path configured for {field.category.name}"
            logger.warning("[warning]⚠ NO FILE[/warning]  %s: %s", field.label, msg)
            return (field, False, msg)

        file_path = Path(file_path_str).expanduser().resolve()
        if not file_path.is_file():
            msg = f"File not found: {file_path}"
            logger.error("[error]✗ MISSING FILE[/error]  %s: %s", field.label, msg)
            return (field, False, msg)

        locator = self._page.locator(field.selector).first
        await locator.set_input_files(str(file_path))
        logger.info(
            "[success]✓ UPLOADED[/success]  %s: %s",
            field.label,
            file_path.name,
        )
        return (field, True, f"Uploaded: {file_path.name}")

    async def _fill_screening_question(self, field: FormField) -> FillResult:
        """Handle a screening question via the QuestionMatcher."""
        # A user may have completed this field during a manual-intervention
        # pause. Preserve that work when the bot re-reads the same step.
        if field.field_type in {
            FieldType.TEXT_INPUT,
            FieldType.TEXTAREA,
            FieldType.NUMBER,
        } and await self._text_field_already_filled(field):
            return (field, True, "Already filled manually - skipped")
        if field.field_type in {FieldType.SELECT, FieldType.COMBOBOX} and await self._select_field_already_filled(field):
            return (field, True, "Already selected manually - skipped")
        if field.field_type == FieldType.RADIO and await self._radio_already_checked(field):
            return (field, True, "Already selected manually - skipped")
        if field.field_type == FieldType.CHECKBOX and await self._checkbox_already_checked(field):
            return (field, True, "Already checked manually - skipped")

        if self._requires_integer_answer(field):
            return await self._fill_integer_screening_question(field)

        if self._is_transient_work_history_field(field):
            return await self._fill_transient_work_history_field(field)

        options = field.options if field.options else None
        question_key = self._question_key_for_field(field)

        if field.field_type in {FieldType.SELECT, FieldType.COMBOBOX} and field.options:
            auto_select_answer = self._auto_answer_select_by_options(field.options)
            if auto_select_answer:
                selected = await self._choose_select_option(field, auto_select_answer)
                if selected:
                    logger.info(
                        "[success]AUTO-ANSWER[/success]  %r -> %r (select options)",
                        field.label[:60],
                        selected,
                    )
                    return (field, True, f"Screening answer: {selected}")
        answer = self._auto_answer_dynamic_question(field.label)
        if answer:
            logger.info("[success]âœ“ AUTO-DYNAMIC[/success]  %s: %r", field.label, answer)
        else:
            answer = await self._qm.get_or_ask(
                question=question_key,
                options=options,
                agent_id=self._agent_id or None,
                allow_empty=not field.required,
            )

        if not answer or not str(answer).strip():
            if not field.required:
                return (field, True, "Skipped optional field")
            return (field, False, "No answer provided for screening question")
        if str(answer).strip().lower() == "__skip_job__":
            return (field, False, "Skip requested by user")

        # Now fill the answer into the actual field
        match field.field_type:
            case FieldType.TEXT_INPUT | FieldType.TEXTAREA | FieldType.NUMBER:
                error = await self._type_field(field, answer)
                if error:
                    return (field, False, error)
            case FieldType.SELECT | FieldType.COMBOBOX:
                selected = await self._choose_select_option(field, answer)
                if not selected:
                    return (field, False, f"No matching select option for {answer!r}")
            case FieldType.RADIO:
                best = self._best_option_match(answer, field.options)
                if best is not None:
                    idx = field.options.index(best)
                    await self._page.locator(field.selector).nth(idx).check()
                else:
                    return (field, False, f"No matching radio option for {answer!r}")
            case FieldType.CHECKBOX:
                checkbox_result = await self._fill_checkbox(field, answer)
                if not checkbox_result[1]:
                    return checkbox_result
            case _:
                error = await self._type_field(field, answer)
                if error:
                    return (field, False, error)

        logger.info(
            "[success]✓ SCREENING[/success]  %s: answered %r",
            field.label,
            answer,
        )
        return (field, True, f"Screening answer: {answer}")

    async def _fill_salary_question(self, field: FormField) -> FillResult:
        """Always ask salary questions with job salary context; do not save."""
        prompt = field.label
        context = self._salary_context_prompt()
        if context:
            prompt = f"{field.label}\n\n{context}"

        answer = await self._qm.prompt_user(prompt, agent_id=self._agent_id or None)
        if answer is None:
            answer = ""
        if not answer.strip():
            return (field, False, "No salary answer provided")
        if str(answer).strip().lower() == "__skip_job__":
            return (field, False, "Skip requested by user")

        match field.field_type:
            case FieldType.SELECT | FieldType.COMBOBOX:
                selected = await self._choose_select_option(field, answer)
                if not selected:
                    return (field, False, f"No matching select option for {answer!r}")
            case FieldType.RADIO:
                best = self._best_option_match(answer, field.options)
                if best is None:
                    return (field, False, f"No matching radio option for {answer!r}")
                await self._page.locator(field.selector).nth(field.options.index(best)).check()
            case _:
                error = await self._type_field(field, answer)
                if error:
                    return (field, False, error)

        logger.info("[success]âœ“ SALARY[/success]  %s: answered %r", field.label, answer)
        return (field, True, f"Salary answer: {answer}")

    def _salary_context_prompt(self) -> str:
        """Build salary hints from listing salary and job description."""
        salary = (self._job_context.get("salary") or "").strip()
        description = self._job_context.get("description") or ""
        hints: list[str] = []

        if salary:
            hints.append(f"Listed salary: {salary}")

        salary_lines = self._extract_salary_lines(description)
        if salary_lines:
            hints.append("Salary details found in description:")
            hints.extend(f"- {line}" for line in salary_lines[:5])

        if not hints:
            return "No salary range was found in the listing or description."
        return "\n".join(hints)

    @staticmethod
    def _extract_salary_lines(description: str) -> list[str]:
        """Extract likely compensation lines from a job description."""
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in description.splitlines()
        ]
        lines = [line for line in lines if line]
        patterns = [
            re.compile(r"[$€£]\s?\d", re.IGNORECASE),
            re.compile(r"\b(salary|compensation|pay range|base pay|hourly|per hour|per year|annually)\b", re.IGNORECASE),
        ]

        matches: list[str] = []
        for line in lines:
            if any(pattern.search(line) for pattern in patterns):
                matches.append(line[:240])
        return matches

    async def _fill_transient_work_history_field(self, field: FormField) -> FillResult:
        """Prompt for per-job work-history fields without saving/reusing them."""
        cache_key = self._transient_work_history_key(field.label)
        
        if self._job_context.get("mode") == "auto" and cache_key in {"job title", "company"}:
            self._transient_answers[cache_key] = ""
            logger.info("⏭ Skipping transient %s field in auto mode (leaving empty)", cache_key)
            return (field, True, f"Skipped {cache_key} field in auto mode")

        is_optional_company = cache_key == "company" and not field.required

        if cache_key in self._transient_answers:
            answer = self._transient_answers[cache_key]
            if not answer:
                return (field, True, "Skipped optional field")

            error = await self._type_field(field, answer)
            if error:
                return (field, False, error)
            logger.info(
                "[success]âœ“ MANUAL[/success]  %s: reused %r for this application",
                field.label,
                answer,
            )
            return (field, True, f"Manual answer reused: {answer}")

        prompt = field.label
        if is_optional_company:
            prompt = f"{field.label}\n\nPress Enter to skip."

        answer = await self._qm.prompt_user(
            prompt,
            allow_empty=is_optional_company,
            agent_id=self._agent_id or None,
        )
        if answer is None:
            answer = ""
        self._transient_answers[cache_key] = answer.strip()
        if not answer.strip():
            return (field, True, "Skipped optional field")

        error = await self._type_field(field, answer)
        if error:
            return (field, False, error)
        logger.info(
            "[success]âœ“ MANUAL[/success]  %s: answered %r",
            field.label,
            answer,
        )
        return (field, True, f"Manual answer: {answer}")

    @classmethod
    def _is_transient_work_history_field(cls, field: FormField) -> bool:
        """Fields whose answers should not be saved/reused globally."""
        return cls._transient_work_history_key(field.label) in {"job title", "company"}

    @classmethod
    def _transient_work_history_key(cls, label: str) -> str:
        normalized = cls._normalized_label(label)
        normalized = re.sub(r"\b(input|field|textbox|text box)\b", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()

        if normalized in {"job title", "title", "position", "position title"}:
            return "job title"
        if normalized in {"company", "company name", "employer", "employer name"}:
            return "company"
        return normalized

    @staticmethod
    def _normalized_label(label: str) -> str:
        return re.sub(r"\s+", " ", (label or "").strip().rstrip("*")).lower()

    async def _fill_integer_screening_question(self, field: FormField) -> FillResult:
        """Prompt/cache a numeric-only variant for integer fields."""
        cache_key = f"{field.label} [number only]"
        saved_answer, confidence = await self._qm.find_answer(cache_key, fuzzy=False)
        if saved_answer:
            answer = saved_answer
            logger.info(
                "[success]âœ“ AUTO-ANSWER[/success]  %r â†’ %r (number-only)",
                field.label[:50],
                answer,
            )
        else:
            logger.info(
                "[warning]? MANUAL[/warning]  No saved numeric answer for: %s",
                field.label[:60],
            )
            raw_answer = await self._qm.prompt_user(
                f"{field.label}\n\nEnter a whole number only.",
                agent_id=self._agent_id or None,
            )
            answer = self._extract_integer_answer(raw_answer) or raw_answer.strip()

            while not self._is_integer_answer(answer):
                raw_answer = await self._qm.prompt_user(
                    f"{field.label}\n\nPlease enter a valid whole number, e.g. 3.",
                    agent_id=self._agent_id or None,
                )
                answer = self._extract_integer_answer(raw_answer) or raw_answer.strip()

            await self._qm.save_answer(cache_key, answer)

        error = await self._type_field(field, answer)
        if error:
            return (field, False, error)
        logger.info(
            "[success]âœ“ SCREENING[/success]  %s: answered %r",
            field.label,
            answer,
        )
        return (field, True, f"Screening answer: {answer}")

    def _question_key_for_field(self, field: FormField) -> str:
        """Build a cache key; include select-option context for ambiguous labels."""
        base = field.label or ""
        if field.field_type not in {FieldType.SELECT, FieldType.COMBOBOX} or not field.options:
            return base
        signature = self._options_signature(field.options)
        return f"{base} [options:{signature}]"

    @staticmethod
    def _options_signature(options: list[str]) -> str:
        normalized = [
            re.sub(r"\s+", " ", (opt or "").strip().lower())
            for opt in options
            if (opt or "").strip()
        ]
        return "|".join(normalized[:10])

    def _auto_answer_select_by_options(self, options: list[str]) -> str | None:
        """Return a sensible default for known select-option sets."""
        normalized = [re.sub(r"\s+", " ", (opt or "").strip()) for opt in options if (opt or "").strip()]
        lowered = {opt.lower() for opt in normalized}

        proficiency_tokens = {
            "beginner", "intermediate", "advanced", "fluent",
            "native", "proficient", "basic", "conversational",
        }
        language_tokens = {
            "english", "hindi", "spanish", "french", "chinese",
            "urdu", "arabic", "german", "japanese", "korean",
        }

        if lowered & proficiency_tokens:
            preferred = str(self._config.get("preferences", {}).get("language_proficiency", "")).strip()
            if preferred:
                match = self._best_option_match(preferred, normalized)
                if match:
                    return match
            for fallback in ("Fluent", "Advanced", "Intermediate", "Beginner"):
                match = self._best_option_match(fallback, normalized)
                if match:
                    return match

        if lowered & language_tokens:
            preferred_language = (
                str(self._config.get("preferences", {}).get("language", "")).strip()
                or str(self._config.get("personal", {}).get("language", "")).strip()
                or "English"
            )
            match = self._best_option_match(preferred_language, normalized)
            if match:
                return match

        return None

    @staticmethod
    def _requires_integer_answer(field: FormField) -> bool:
        """Return True for fields whose validation expects a whole number."""
        label = (field.label or "").lower()
        return (
            field.category == FieldCategory.YEARS_EXPERIENCE
            and field.field_type in {FieldType.TEXT_INPUT, FieldType.NUMBER}
            and ("year" in label or field.required)
        )

    @staticmethod
    def _extract_integer_answer(answer: str) -> str | None:
        """Extract a whole-number answer from casual text."""
        match = re.search(r"\b(\d+)\s*\+?\s*(?:years?|yrs?)?\b", answer, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _is_integer_answer(answer: str) -> bool:
        return bool(re.fullmatch(r"\d+", answer.strip()))

    @staticmethod
    def _should_prompt_when_config_missing(field: FormField) -> bool:
        """Ask the user for required/question-like fields missing config."""
        if field.field_type == FieldType.FILE_UPLOAD:
            return False
        if field.field_type == FieldType.CHECKBOX and not field.options:
            return False

        # Selects are often required even when the required marker is not detected.
        # For known decision categories, prompt anyway when we have selectable options.
        if field.field_type in {FieldType.SELECT, FieldType.COMBOBOX} and field.options:
            has_real_options = any(
                not re.fullmatch(r"\s*select\s+(an\s+)?option\s*", opt.strip(), re.IGNORECASE)
                for opt in field.options
            )
            if has_real_options and field.category in {
                FieldCategory.EDUCATION,
                FieldCategory.WORK_AUTHORIZATION,
                FieldCategory.WILLING_TO_RELOCATE,
                FieldCategory.SCREENING_QUESTION,
                FieldCategory.UNKNOWN,
            }:
                return True

        label = field.label or ""
        question_like = "?" in label
        fillable_type = field.field_type in {
            FieldType.TEXT_INPUT,
            FieldType.TEXTAREA,
            FieldType.NUMBER,
            FieldType.DATE,
            FieldType.SELECT,
            FieldType.COMBOBOX,
        }
        return fillable_type and (field.required or question_like)

    @staticmethod
    def _auto_answer_dynamic_question(question: str) -> str | None:
        """Answer prompts whose value changes with the current run."""
        normalized = re.sub(r"\s+", " ", question).strip().lower().rstrip("* ")
        if re.search(r"\b(today'?s|current)\s+date\b", normalized):
            return datetime.now().strftime("%m/%d/%Y")
        return None

    # ------------------------------------------------------------------
    # Option matching helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _try_select_option(
        locator: Any,
        value: str,
        options: list[str],
    ) -> str | None:
        """Try to select a ``<select>`` option matching *value*.

        Tries exact label match, then case-insensitive substring.

        Returns:
            The label of the selected option, or ``None``.
        """
        # Exact match
        if value in options:
            await locator.select_option(label=value, timeout=5000)
            return value

        # Case-insensitive substring
        value_lower = value.lower()
        for opt in options:
            if value_lower in opt.lower() or opt.lower() in value_lower:
                await locator.select_option(label=opt, timeout=5000)
                return opt

        return None

    async def _choose_select_option(self, field: FormField, value: str) -> str | None:
        """Choose a value in either a native select or an ARIA combobox."""
        if field.field_type == FieldType.COMBOBOX:
            return await self._try_combobox_option(field, value)
        return await self._try_select_option(
            self._page.locator(field.selector).first,
            value,
            field.options,
        )

    async def _try_combobox_option(self, field: FormField, value: str) -> str | None:
        """Open a custom listbox and click its best matching visible option."""
        best = self._best_option_match(value, field.options) or value.strip()
        if not best:
            return None

        trigger = self._page.locator(field.selector).first
        await trigger.click()
        await self._page.wait_for_timeout(100)
        exact = re.compile(rf"^{re.escape(best)}$", re.IGNORECASE)
        candidates = [
            self._page.get_by_role("option", name=exact),
            self._page.locator('[role="option"]').filter(has_text=exact),
            self._page.get_by_text(exact, exact=True),
        ]
        for candidate in candidates:
            try:
                count = await candidate.count()
                for index in range(count):
                    option = candidate.nth(index)
                    if await option.is_visible() and await option.is_enabled():
                        await option.click()
                        await self._page.wait_for_timeout(150)
                        return best
            except Exception:
                continue
        try:
            await trigger.press("Escape")
        except Exception:
            pass
        return None

    @staticmethod
    def _best_option_match(value: str, options: list[str]) -> str | None:
        """Find the best-matching option label for *value*.

        Uses case-insensitive comparison and substring matching.
        """
        if not options:
            return None

        value_lower = value.lower()

        # Exact (case-insensitive)
        for opt in options:
            if opt.lower() == value_lower:
                return opt

        # Substring
        for opt in options:
            if value_lower in opt.lower() or opt.lower() in value_lower:
                return opt

        # Convert a numeric free-text answer (for example "4+ years") to a
        # bounded choice such as "Less than 5 years" or "5-6 years".
        numeric = re.search(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", value_lower)
        if numeric:
            number = float(numeric.group(1))
            for opt in options:
                label = opt.lower().replace("\u2013", "-").replace("\u2014", "-")
                bounds = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", label)]
                if not bounds:
                    continue
                if re.search(r"\b(less than|under|below)\b", label) and number < bounds[0]:
                    return opt
                if re.search(r"\b(more than|over|above)\b", label) and number > bounds[0]:
                    return opt
                if re.search(r"\b(at least|minimum)\b", label) and number >= bounds[0]:
                    return opt
                if len(bounds) >= 2 and bounds[0] <= number <= bounds[1]:
                    return opt
                if re.search(r"\d\s*\+", label) and number >= bounds[0]:
                    return opt

        return None
