"""Screenshot utility for Playwright browser automation.

Captures full-page screenshots on demand or automatically on errors,
storing them in ``data/screenshots/`` with timestamped filenames.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from src.utils.logger import logger

# ── Screenshot directory ──────────────────────────────────────────────
_SCREENSHOTS_DIR = Path("data/screenshots")
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def _sanitise_filename(raw: str) -> str:
    """Strip non-alphanumeric characters (except hyphens/underscores) from *raw*.

    Args:
        raw: Arbitrary string to be used in a filename.

    Returns:
        A filesystem-safe version of the string (lowercase, max 60 chars).
    """
    cleaned = re.sub(r"[^\w\-]", "_", raw.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:60]


def _safe_path_part(raw: str, max_len: int = 120) -> str:
    """Return a readable Windows-safe path component."""
    cleaned = re.sub(r"[\r\n]+", " - ", raw.strip())
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return (cleaned or "screenshot")[:max_len]


def _timestamped_path(name: str, context: str = "") -> Path:
    """Build a screenshot path like ``data/screenshots/20260529_145823_name.png``.

    Args:
        name: Primary label for the screenshot.
        context: Optional extra context appended after *name*.

    Returns:
        :class:`Path` to the (not-yet-existing) PNG file.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _sanitise_filename(name)
    parts = [ts, safe_name]
    if context:
        parts.append(_sanitise_filename(context))
    filename = "__".join(parts) + ".png"
    return _SCREENSHOTS_DIR / filename


async def capture_screenshot(
    page,  # playwright.async_api.Page
    name: str,
    context: str = "",
    directory: str | Path | None = None,
    filename: str | None = None,
) -> Path:
    """Capture a full-page screenshot and save it to disk.

    Args:
        page: Playwright async ``Page`` object.
        name: Descriptive label used in the filename.
        context: Optional extra context (e.g. step name) added to filename.

    Returns:
        :class:`Path` to the saved PNG file.
    """
    target_dir = Path(directory) if directory else _SCREENSHOTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    if filename:
        safe_filename = _safe_path_part(filename)
        if not safe_filename.lower().endswith(".png"):
            safe_filename += ".png"
        filepath = target_dir / safe_filename
    else:
        filepath = _timestamped_path(name, context)

    await page.screenshot(path=str(filepath), full_page=True)
    logger.info("📸 Screenshot saved → %s", filepath)
    return filepath


async def capture_application_screenshot(
    page,
    search_query: str,
    job_title: str,
    company: str,
) -> Path:
    """Capture an applied-job screenshot in a dated search folder."""
    today = datetime.now().strftime("%Y-%m-%d")
    folder = _SCREENSHOTS_DIR / _safe_path_part(f"{today} - {search_query}")
    filename = _safe_path_part(f"📌 {job_title} - {company}") + ".png"
    filename = _safe_path_part(f"{chr(0x1F4CC)} {job_title} - {company}") + ".png"
    return await capture_screenshot(
        page,
        name="applied",
        directory=folder,
        filename=filename,
    )


async def capture_on_error(
    page,  # playwright.async_api.Page
    error: Exception,
    job_title: str = "",
) -> Path:
    """Capture a screenshot when an error occurs and log the details.

    Args:
        page: Playwright async ``Page`` object.
        error: The exception that triggered the capture.
        job_title: Optional job title for contextual labelling.

    Returns:
        :class:`Path` to the saved PNG file.
    """
    label = job_title if job_title else "unknown"
    error_type = type(error).__name__

    logger.error(
        "🔴 Error on '%s': [%s] %s — capturing screenshot",
        label,
        error_type,
        error,
    )

    filepath = await capture_screenshot(
        page,
        name=f"error_{error_type}",
        context=label,
    )
    return filepath
