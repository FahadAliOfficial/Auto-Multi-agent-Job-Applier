# src/handlers/form_detector.py
"""Detects and classifies form fields on Indeed Easy Apply pages.

Scans the current application step for all interactive form elements,
extracts metadata (labels, options, required status), and classifies
each field into a semantic category for downstream filling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from rich.table import Table

from src.utils.logger import cc_print, console

if TYPE_CHECKING:
    from playwright.async_api import ElementHandle, Page


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FieldType(Enum):
    """Low-level HTML control type."""

    TEXT_INPUT = auto()
    TEXTAREA = auto()
    SELECT = auto()
    COMBOBOX = auto()
    RADIO = auto()
    CHECKBOX = auto()
    FILE_UPLOAD = auto()
    DATE = auto()
    PHONE = auto()
    EMAIL = auto()
    NUMBER = auto()
    UNKNOWN = auto()


class FieldCategory(Enum):
    """Semantic meaning of a form field."""

    FIRST_NAME = auto()
    LAST_NAME = auto()
    FULL_NAME = auto()
    EMAIL = auto()
    PHONE = auto()
    LOCATION = auto()
    ADDRESS = auto()
    CITY = auto()
    STATE = auto()
    ZIP_CODE = auto()
    RESUME = auto()
    COVER_LETTER = auto()
    YEARS_EXPERIENCE = auto()
    EDUCATION = auto()
    SALARY_EXPECTATION = auto()
    START_DATE = auto()
    WORK_AUTHORIZATION = auto()
    WILLING_TO_RELOCATE = auto()
    LINKEDIN = auto()
    WEBSITE = auto()
    COUNTRY = auto()
    SCREENING_QUESTION = auto()
    UNKNOWN = auto()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FormField:
    """Represents a single detected form field with its metadata."""

    field_type: FieldType
    category: FieldCategory
    label: str
    selector: str  # Playwright locator string
    required: bool = False
    options: list[str] = field(default_factory=list)  # For select/radio/checkbox
    current_value: str = ""
    placeholder: str = ""


# ---------------------------------------------------------------------------
# Category classification patterns
# ---------------------------------------------------------------------------

# Order matters – first match wins.  Patterns are compiled once at import.
_CATEGORY_PATTERNS: list[tuple[re.Pattern[str], FieldCategory]] = [
    (re.compile(r"\b(first\s*name)\b", re.IGNORECASE), FieldCategory.FIRST_NAME),
    (re.compile(r"\b(last\s*name|surname|family\s*name)\b", re.IGNORECASE), FieldCategory.LAST_NAME),
    (re.compile(r"\b(full\s*name|your\s*name)\b", re.IGNORECASE), FieldCategory.FULL_NAME),
    (re.compile(r"\b(e[\-\s]?mail)\b", re.IGNORECASE), FieldCategory.EMAIL),
    (re.compile(r"\b(phone|mobile|cell|telephone)\b", re.IGNORECASE), FieldCategory.PHONE),
    (re.compile(r"\b(resume|cv|curriculum\s*vitae)\b", re.IGNORECASE), FieldCategory.RESUME),
    (re.compile(r"\b(cover\s*letter)\b", re.IGNORECASE), FieldCategory.COVER_LETTER),
    (re.compile(r"\b(year(?:s)?\b.*\bexperience|experience\b.*\byear(?:s)?)\b", re.IGNORECASE), FieldCategory.YEARS_EXPERIENCE),
    (re.compile(r"\b(education|degree|school|university)\b", re.IGNORECASE), FieldCategory.EDUCATION),
    (re.compile(r"\b(salary|compensation|pay|wage)\b", re.IGNORECASE), FieldCategory.SALARY_EXPECTATION),
    (re.compile(r"\b(start\s*date|available\s*date|earliest\s*date)\b", re.IGNORECASE), FieldCategory.START_DATE),
    (re.compile(r"\b(work\s*auth|authorized?\s*to\s*work|visa|sponsorship|eligib)", re.IGNORECASE), FieldCategory.WORK_AUTHORIZATION),
    (re.compile(r"\b(relocat\w*|willing\s*to\s*move)\b", re.IGNORECASE), FieldCategory.WILLING_TO_RELOCATE),
    (re.compile(r"\b(linkedin)\b", re.IGNORECASE), FieldCategory.LINKEDIN),
    (re.compile(r"\b(website|portfolio|url|github|personal\s*site)\b", re.IGNORECASE), FieldCategory.WEBSITE),
    (re.compile(r"\b(city)\b", re.IGNORECASE), FieldCategory.CITY),
    (re.compile(r"\b(state|province)\b", re.IGNORECASE), FieldCategory.STATE),
    (re.compile(r"\b(zip\s*code|postal\s*code|zip)\b", re.IGNORECASE), FieldCategory.ZIP_CODE),
    (re.compile(r"\b(address|street)\b", re.IGNORECASE), FieldCategory.ADDRESS),
    (re.compile(r"\b(location|where\s*are\s*you)\b", re.IGNORECASE), FieldCategory.LOCATION),
    (re.compile(r"\b(country)\b", re.IGNORECASE), FieldCategory.COUNTRY),
]


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class FormDetector:
    """Scans an Indeed Easy Apply page step and returns structured field data.

    Usage::

        detector = FormDetector()
        fields = await detector.detect_fields(page)
        for f in fields:
            print(f.category, f.label)
    """

    # Indeed wraps each form step inside this container (may vary).
    _FORM_CONTAINER_SELECTORS: list[str] = [
        '[data-testid="ia-BasePage"]',
        '[class*="ia-BasePage"]',
        'form',
        '[role="dialog"]',
    ]

    def __init__(self) -> None:
        self._field_seq = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def detect_fields(self, page: Page) -> list[FormField]:
        """Scan the current form step and return all detected fields.

        Args:
            page: The active Playwright page with a visible form step.

        Returns:
            A list of :class:`FormField` objects, one per interactive element.
        """
        fields: list[FormField] = []

        # --- text-like inputs ------------------------------------------------
        input_elements = await page.query_selector_all(
            'input:not([type="hidden"]):not([type="submit"]):not([type="button"])'
        )
        for el in input_elements:
            if await self._should_skip_element(el):
                continue

            input_type = (await el.get_attribute("type") or "text").lower()

            # Skip radio / checkbox here – handled separately below
            if input_type in {"radio", "checkbox"}:
                continue

            field_type = self._input_type_to_field_type(input_type)
            label_text = await self._extract_label(page, el)
            placeholder = await el.get_attribute("placeholder") or ""
            combined_label = label_text or placeholder
            if self._is_progress_control_label(combined_label, field_type):
                continue
            required = await self._is_required(el, combined_label)
            selector = await self._build_selector(el)
            if not selector:
                continue
            current_value = await el.input_value() if field_type != FieldType.FILE_UPLOAD else ""

            category = self._classify_field(combined_label, field_type)

            fields.append(
                FormField(
                    field_type=field_type,
                    category=category,
                    label=combined_label,
                    selector=selector,
                    required=required,
                    current_value=current_value,
                    placeholder=placeholder,
                )
            )

        # --- textareas -------------------------------------------------------
        for el in await page.query_selector_all("textarea"):
            if await self._should_skip_element(el):
                continue

            label_text = await self._extract_label(page, el)
            placeholder = await el.get_attribute("placeholder") or ""
            combined_label = label_text or placeholder
            required = await self._is_required(el, combined_label)
            selector = await self._build_selector(el)
            if not selector:
                continue
            current_value = await el.input_value()

            fields.append(
                FormField(
                    field_type=FieldType.TEXTAREA,
                    category=self._classify_field(combined_label, FieldType.TEXTAREA),
                    label=combined_label,
                    selector=selector,
                    required=required,
                    current_value=current_value,
                    placeholder=placeholder,
                )
            )

        # --- select dropdowns ------------------------------------------------
        for el in await page.query_selector_all("select"):
            if await self._should_skip_element(el):
                continue

            label_text = await self._extract_label(page, el)
            required = await self._is_required(el, label_text)
            selector = await self._build_selector(el)
            if not selector:
                continue
            options = await self._extract_select_options(el)
            current_value = await el.input_value()

            fields.append(
                FormField(
                    field_type=FieldType.SELECT,
                    category=self._classify_field(label_text, FieldType.SELECT),
                    label=label_text,
                    selector=selector,
                    required=required,
                    options=options,
                    current_value=current_value,
                )
            )

        # --- custom ARIA dropdowns -------------------------------------------
        # Current Indeed forms render many required dropdowns as buttons/divs
        # backed by listboxes rather than native <select> elements.
        custom_comboboxes = await page.query_selector_all(
            '[role="combobox"]:not(select), '
            'button[aria-haspopup="listbox"], '
            '[role="button"][aria-haspopup="listbox"]'
        )
        for el in custom_comboboxes:
            if await self._should_skip_element(el):
                continue
            label_text = await self._extract_label(page, el)
            placeholder = (
                await el.get_attribute("placeholder")
                or await el.get_attribute("aria-placeholder")
                or ""
            )
            combined_label = label_text or placeholder
            selector = await self._build_selector(el)
            if not selector:
                continue
            options = await self._extract_combobox_options(page, el)
            try:
                current_value = (await el.inner_text()).strip()
            except Exception:
                current_value = ""
            fields.append(
                FormField(
                    field_type=FieldType.COMBOBOX,
                    category=self._classify_field(combined_label, FieldType.COMBOBOX),
                    label=combined_label,
                    selector=selector,
                    required=await self._is_required(el, combined_label),
                    options=options,
                    current_value=current_value,
                    placeholder=placeholder,
                )
            )

        # --- radio groups ----------------------------------------------------
        fields.extend(await self._detect_radio_groups(page))

        # --- checkbox groups -------------------------------------------------
        fields.extend(await self._detect_checkbox_groups(page))

        # --- file uploads (explicit <input type="file">) ---------------------
        for el in await page.query_selector_all('input[type="file"]'):
            if await self._is_recaptcha_element(el):
                continue

            label_text = await self._extract_label(page, el)
            selector = await self._build_selector(el)
            if not selector:
                continue
            required = await self._is_required(el)

            fields.append(
                FormField(
                    field_type=FieldType.FILE_UPLOAD,
                    category=self._classify_field(label_text, FieldType.FILE_UPLOAD),
                    label=label_text,
                    selector=selector,
                    required=required,
                )
            )

        self._log_detected_fields(fields)
        return fields

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify_field(self, label: str, field_type: FieldType) -> FieldCategory:
        """Classify a field into a semantic category via keyword matching.

        Args:
            label: The human-readable label / placeholder text.
            field_type: The HTML control type.

        Returns:
            The best-matching :class:`FieldCategory`.
        """
        if not label:
            return FieldCategory.UNKNOWN

        # Walk through compiled patterns (first match wins).
        for pattern, category in _CATEGORY_PATTERNS:
            if pattern.search(label):
                return category

        # Heuristic fallback: text-like fields with no label match are
        # likely screening questions; everything else is UNKNOWN.
        match field_type:
            case FieldType.TEXT_INPUT | FieldType.TEXTAREA | FieldType.NUMBER:
                return FieldCategory.SCREENING_QUESTION
            case FieldType.EMAIL:
                return FieldCategory.EMAIL
            case FieldType.PHONE:
                return FieldCategory.PHONE
            case FieldType.FILE_UPLOAD:
                return FieldCategory.RESUME
            case FieldType.DATE:
                return FieldCategory.START_DATE
            case _:
                return FieldCategory.UNKNOWN

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _input_type_to_field_type(input_type: str) -> FieldType:
        """Map an HTML ``<input type="…">`` value to a :class:`FieldType`."""
        mapping: dict[str, FieldType] = {
            "text": FieldType.TEXT_INPUT,
            "email": FieldType.EMAIL,
            "tel": FieldType.PHONE,
            "number": FieldType.NUMBER,
            "date": FieldType.DATE,
            "file": FieldType.FILE_UPLOAD,
            "url": FieldType.TEXT_INPUT,
            "search": FieldType.TEXT_INPUT,
            "password": FieldType.TEXT_INPUT,
        }
        return mapping.get(input_type, FieldType.TEXT_INPUT)

    async def _extract_label(self, page: Page, element: ElementHandle) -> str:
        """Try multiple strategies to find the label for *element*.

        Priority order:
        1. Explicit ``<label for="id">`` association.
        2. ``aria-label`` attribute.
        3. ``aria-labelledby`` → referenced element's text.
        4. Parent ``<label>`` element.
        5. ``data-testid`` attribute (Indeed-specific).
        6. Preceding sibling text.

        Returns:
            Cleaned label string, or ``""`` if nothing found.
        """
        # 1. <label for="id">
        el_id = await element.get_attribute("id")
        if el_id:
            label_el = await page.query_selector(f'label[for={self._css_string(el_id)}]')
            if label_el:
                text = (await label_el.inner_text()).strip()
                if text:
                    return text

        # 2. aria-label
        aria_label = await element.get_attribute("aria-label")
        if aria_label:
            cleaned = aria_label.strip()
            if cleaned.lower() not in ("attach", "upload", "choose file", "browse"):
                return cleaned

        # 3. aria-labelledby
        labelledby = await element.get_attribute("aria-labelledby")
        if labelledby:
            ref_el = await page.query_selector(f'[id={self._css_string(labelledby)}]')
            if ref_el:
                text = (await ref_el.inner_text()).strip()
                if text:
                    return text

        # 4. Parent <label>
        parent_label = await element.evaluate_handle(
            """el => {
                let node = el.parentElement;
                while (node) {
                    if (node.tagName === 'LABEL') return node;
                    node = node.parentElement;
                }
                return null;
            }"""
        )
        if parent_label:
            try:
                text = await parent_label.as_element().inner_text()
                text = text.strip()
                if text:
                    return text
            except Exception:
                pass

        # 5. data-testid (Indeed-specific hints)
        test_id = await element.get_attribute("data-testid")
        if test_id:
            # e.g. "input-firstName" → "firstName"
            cleaned = re.sub(r"^(input|select|textarea)[-_]?", "", test_id)
            cleaned = re.sub(r"[-_]+", " ", cleaned).strip()
            if cleaned:
                return cleaned

        # 5b. Wrapper label (useful for Greenhouse file uploads)
        wrapper_label = await element.evaluate(
            """el => {
                let node = el.parentElement;
                for (let i = 0; i < 5 && node; i++) {
                    const labelNode = node.querySelector('label, [class*="label"], [class*="question"]');
                    if (labelNode && labelNode.innerText) {
                        return labelNode.innerText;
                    }
                    node = node.parentElement;
                }
                return '';
            }"""
        )
        if wrapper_label:
            cleaned = wrapper_label.split('\n')[0].replace('*', '').strip()
            if cleaned and cleaned.lower() not in ("attach", "upload", "choose file", "browse"):
                return cleaned

        # 6. Name attribute as last resort
        name = await element.get_attribute("name")
        if name:
            cleaned = re.sub(r"[-_\[\]]+", " ", name).strip()
            if cleaned:
                return cleaned

        return ""

    @staticmethod
    async def _is_required(element: ElementHandle, label_text: str = "") -> bool:
        """Check whether the element is marked as required."""
        if await element.get_attribute("required") is not None:
            return True
        aria_required = await element.get_attribute("aria-required")
        if aria_required and aria_required.lower() == "true":
            return True
        if "*" in label_text:
            return True
        return False

    async def _build_selector(self, element: ElementHandle) -> str:
        """Build a stable Playwright selector string for *element*.

        Prefers ``data-testid``, then ``data-qa``, then ``id``. If none exist, injects ``data-bot-field-id``.
        """
        test_id = await element.get_attribute("data-testid")
        if test_id:
            return f'[data-testid={FormDetector._css_string(test_id)}]'

        qa_id = await element.get_attribute("data-qa")
        if qa_id:
            return f'[data-qa={FormDetector._css_string(qa_id)}]'

        el_id = await element.get_attribute("id")
        if el_id:
            return f'[id={FormDetector._css_string(el_id)}]'

        # Fallback: inject a custom attribute
        self._field_seq += 1
        bot_id = f"field-{self._field_seq}"
        try:
            await element.evaluate(
                "(el, value) => el.setAttribute('data-bot-field-id', value)",
                bot_id,
            )
            return f'[data-bot-field-id={self._css_string(bot_id)}]'
        except Exception:
            pass

        # Absolute fallback: build a CSS selector with tag + name + type
        tag = await element.evaluate("el => el.tagName.toLowerCase()")
        name = await element.get_attribute("name")
        input_type = await element.get_attribute("type")

        parts = [tag]
        if name:
            parts.append(f'[name={FormDetector._css_string(name)}]')
        if input_type:
            parts.append(f'[type={FormDetector._css_string(input_type)}]')

        return "".join(parts)

    @staticmethod
    def _css_string(value: str) -> str:
        """Return a safely quoted CSS attribute string."""
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    async def _is_recaptcha_element(element: ElementHandle) -> bool:
        """Return True for hidden Google reCAPTCHA plumbing fields."""
        attrs = [
            await element.get_attribute("id") or "",
            await element.get_attribute("name") or "",
            await element.get_attribute("aria-label") or "",
            await element.get_attribute("data-testid") or "",
        ]
        return any("recaptcha" in attr.lower() for attr in attrs)

    @staticmethod
    async def _should_skip_element(element: ElementHandle) -> bool:
        """Skip hidden/browser-managed fields that users should not fill."""
        if await FormDetector._is_recaptcha_element(element):
            return True
        progress_attrs = " ".join(
            [
                await element.get_attribute("id") or "",
                await element.get_attribute("name") or "",
                await element.get_attribute("aria-label") or "",
                await element.get_attribute("data-testid") or "",
                await element.get_attribute("role") or "",
            ]
        ).lower()
        if (
            "current page" in progress_attrs
            or "pagination" in progress_attrs
            or "progress" in progress_attrs
        ):
            return True
        if (await element.get_attribute("aria-hidden") or "").lower() == "true":
            return True
        try:
            return not await element.is_visible()
        except Exception:
            return False

    @staticmethod
    def _is_progress_control_label(label: str, field_type: FieldType) -> bool:
        """Reject accessibility controls that describe form-step progress."""
        if field_type != FieldType.NUMBER:
            return False
        normalized = re.sub(r"\s+", " ", (label or "").strip().lower())
        return bool(
            re.fullmatch(
                r"(?:current\s+)?(?:page|step)(?:\s+(?:number|progress))?",
                normalized,
            )
        )

    @staticmethod
    async def _extract_select_options(element: ElementHandle) -> list[str]:
        """Return all ``<option>`` text values for a ``<select>``."""
        return await element.evaluate(
            """el => Array.from(el.options)
                .map(o => o.textContent.trim())
                .filter(t => t.length > 0)"""
        )

    @staticmethod
    async def _extract_combobox_options(page: Page, element: ElementHandle) -> list[str]:
        """Open an ARIA combobox briefly and collect its visible choices."""
        options: list[str] = []
        try:
            await element.click()
            await page.wait_for_timeout(150)
            candidates = await page.query_selector_all(
                '[role="option"], [role="listbox"] li, [data-testid*="option"]'
            )
            for candidate in candidates:
                try:
                    if not await candidate.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", (await candidate.inner_text()).strip())
                    if text and text.lower() not in {"select an option", "choose an option"}:
                        options.append(text)
                except Exception:
                    continue
        except Exception:
            pass
        finally:
            try:
                await element.press("Escape")
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        return list(dict.fromkeys(options))

    async def _detect_radio_groups(self, page: Page) -> list[FormField]:
        """Detect radio-button groups and return one :class:`FormField` per group."""
        fields: list[FormField] = []
        seen_names: set[str] = set()

        for el in await page.query_selector_all('input[type="radio"]'):
            if await self._should_skip_element(el):
                continue

            name = await el.get_attribute("name") or ""
            if name in seen_names:
                continue
            seen_names.add(name)

            # Gather all radios in this group
            group_els = await page.query_selector_all(f'input[name={self._css_string(name)}]')
            option_labels: list[str] = []
            for radio in group_els:
                lbl = await self._extract_label(page, radio)
                if lbl:
                    option_labels.append(lbl)

            # The group label is often a nearby heading or fieldset legend
            group_label = await self._find_group_label(page, el)
            required = await self._is_required(el, group_label)
            selector = f'input[name={self._css_string(name)}]'

            fields.append(
                FormField(
                    field_type=FieldType.RADIO,
                    category=self._classify_field(group_label, FieldType.RADIO),
                    label=group_label,
                    selector=selector,
                    required=required,
                    options=option_labels,
                )
            )

        return fields

    async def _detect_checkbox_groups(self, page: Page) -> list[FormField]:
        """Detect checkbox groups and return one :class:`FormField` per group."""
        fields: list[FormField] = []
        seen_names: set[str] = set()

        for el in await page.query_selector_all('input[type="checkbox"]'):
            if await self._should_skip_element(el):
                continue

            name = await el.get_attribute("name") or ""
            if name in seen_names:
                continue
            seen_names.add(name)

            group_els = await page.query_selector_all(f'input[name={self._css_string(name)}]')
            option_labels: list[str] = []
            for cb in group_els:
                lbl = await self._extract_label(page, cb)
                if lbl:
                    option_labels.append(lbl)

            group_label = await self._find_group_label(page, el)
            required = await self._is_required(el, group_label)
            selector = f'input[name={self._css_string(name)}]'

            fields.append(
                FormField(
                    field_type=FieldType.CHECKBOX,
                    category=self._classify_field(group_label, FieldType.CHECKBOX),
                    label=group_label,
                    selector=selector,
                    required=required,
                    options=option_labels,
                )
            )

        return fields

    @staticmethod
    async def _find_group_label(page: Page, member_element: ElementHandle) -> str:
        """Find the label for a radio/checkbox group.

        Looks for an ancestor ``<fieldset>``'s ``<legend>``, then tries
        nearby heading or ``div`` with a label role.
        """
        legend_text: str = await member_element.evaluate(
            """el => {
                let node = el.parentElement;
                while (node) {
                    if (node.tagName === 'FIELDSET') {
                        const legend = node.querySelector('legend');
                        return legend ? legend.textContent.trim() : '';
                    }
                    node = node.parentElement;
                }
                return '';
            }"""
        )
        if legend_text:
            return legend_text

        # Indeed's screening questions often render as a div containing
        # question text plus Yes/No labels, without a fieldset/legend.
        nearby_question: str = await member_element.evaluate(
            """el => {
                const name = el.getAttribute('name');
                const sameGroup = (node) => {
                    if (!name) return false;
                    return node.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"], input[type="checkbox"][name="${CSS.escape(name)}"]`).length > 0;
                };

                let node = el.parentElement;
                for (let depth = 0; depth < 8 && node; depth++) {
                    const groupInputs = name
                        ? [...node.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"], input[type="checkbox"][name="${CSS.escape(name)}"]`)]
                        : [];
                    if (groupInputs.length >= 2 || sameGroup(node)) {
                        const optionTexts = new Set(
                            [...node.querySelectorAll('label')]
                                .map(label => label.innerText.trim().toLowerCase())
                                .filter(Boolean)
                        );
                        const lines = (node.innerText || '')
                            .split('\\n')
                            .map(line => line.replace(/\\s+/g, ' ').trim())
                            .filter(Boolean);

                        const candidates = lines.filter(line => {
                            const normalized = line.toLowerCase();
                            if (optionTexts.has(normalized)) return false;
                            if (/^(yes|no|choose an option to continue\\.?|required)$/i.test(line)) return false;
                            if (/^\\*$/.test(line)) return false;
                            return true;
                        });

                        const question = candidates.find(line => /\\?$/.test(line))
                            || candidates.find(line => line.length > 8);
                        if (question) return question.replace(/\\s*\\*$/, '').trim();
                    }
                    node = node.parentElement;
                }
                return '';
            }"""
        )
        if nearby_question:
            return nearby_question

        # Fallback: look for aria-labelledby on a wrapping div
        wrapper_label: str = await member_element.evaluate(
            """el => {
                let node = el.parentElement;
                for (let i = 0; i < 5 && node; i++) {
                    const lbl = node.getAttribute('aria-label')
                        || node.querySelector('[class*="label"], [class*="question"]')?.textContent?.trim();
                    if (lbl) return lbl;
                    node = node.parentElement;
                }
                return '';
            }"""
        )
        return wrapper_label

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    @staticmethod
    def _log_detected_fields(fields: list[FormField]) -> None:
        """Pretty-print the detected fields using Rich."""
        if not fields:
            cc_print("[yellow]⚠ No form fields detected on this step.[/yellow]")
            return

        table = Table(
            title="Detected Form Fields",
            show_lines=True,
            title_style="bold cyan",
        )
        table.add_column("#", style="dim", width=4)
        table.add_column("Label", style="white", max_width=40)
        table.add_column("Type", style="green")
        table.add_column("Category", style="magenta")
        table.add_column("Req", style="red", width=4)
        table.add_column("Options", style="dim", max_width=30)

        for idx, f in enumerate(fields, 1):
            options_str = ", ".join(f.options[:5])
            if len(f.options) > 5:
                options_str += f" (+{len(f.options) - 5})"
            table.add_row(
                str(idx),
                f.label or "[dim]<no label>[/dim]",
                f.field_type.name,
                f.category.name,
                "✓" if f.required else "",
                options_str or "",
            )

        cc_print(table)
