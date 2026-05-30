"""Human-like delay patterns for browser automation.

Every public function is async and uses :func:`asyncio.sleep`.  Gaussian
distributions (via :func:`random.gauss`) produce natural-looking variance
while hard-clipping prevents outliers from causing unreasonable waits.
"""

from __future__ import annotations

import asyncio
import random

from src.utils.logger import logger


def _clipped_gauss(mu: float, sigma: float, lo: float, hi: float) -> float:
    """Return a gaussian sample clipped to *[lo, hi]*.

    Args:
        mu: Mean of the distribution.
        sigma: Standard deviation.
        lo: Hard lower bound.
        hi: Hard upper bound.

    Returns:
        A float in the range ``[lo, hi]``.
    """
    return max(lo, min(hi, random.gauss(mu, sigma)))


async def random_delay(min_s: float, max_s: float) -> None:
    """Sleep for a gaussian-distributed duration between *min_s* and *max_s*.

    The mean is centred between ``min_s`` and ``max_s``; sigma is set to
    one-sixth of the range so that ~99.7 % of raw samples already fall
    within bounds before clipping.

    Args:
        min_s: Minimum sleep time in seconds.
        max_s: Maximum sleep time in seconds.
    """
    mu = (min_s + max_s) / 2.0
    sigma = (max_s - min_s) / 6.0 if max_s > min_s else 0.0
    delay = _clipped_gauss(mu, sigma, min_s, max_s)
    logger.debug("Sleeping %.2f s (range %.1f–%.1f)", delay, min_s, max_s)
    await asyncio.sleep(delay)


async def typing_delay() -> None:
    """Tiny delay (50-150 ms) that simulates the gap between keystrokes."""
    await random_delay(0.05, 0.15)


async def between_actions() -> None:
    """Short pause (1-3 s) between UI interactions on the same page."""
    await random_delay(1.0, 3.0)


async def between_pages() -> None:
    """Medium pause (3-8 s) mimicking a person reading a new page."""
    await random_delay(3.0, 8.0)


async def between_jobs() -> None:
    """Long pause (30-90 s) between successive job applications.

    This is intentionally long to avoid triggering rate-limit or bot
    detection heuristics.
    """
    await random_delay(30.0, 90.0)


async def type_like_human(
    page,  # playwright.async_api.Page
    selector: str,
    text: str,
    *,
    clear_first: bool = True,
    min_keystroke_ms: float = 35.0,
    max_keystroke_ms: float = 160.0,
) -> None:
    """Type *text* into the element matched by *selector* one character at a time.

    Each keystroke is followed by a gaussian-distributed pause that
    mimics real human typing speed (default 35-160 ms per character).

    Occasionally inserts a longer "thinking" pause to add realism.

    Args:
        page: Playwright async ``Page`` object.
        selector: CSS or Playwright selector for the target element.
        text: The string to type.
        clear_first: Whether to clear any existing value before typing.
        min_keystroke_ms: Fastest inter-keystroke delay in milliseconds.
        max_keystroke_ms: Slowest inter-keystroke delay in milliseconds.
    """
    logger.debug("Typing %d chars into '%s'", len(text), selector)

    # Click the element first to ensure focus
    await page.click(selector)
    await random_delay(0.1, 0.3)

    if clear_first:
        await page.press(selector, "Control+A")
        await page.press(selector, "Backspace")
        await random_delay(0.05, 0.15)

    for i, char in enumerate(text):
        await page.press(selector, char)

        # Base inter-keystroke delay
        mu = (min_keystroke_ms + max_keystroke_ms) / 2.0
        sigma = (max_keystroke_ms - min_keystroke_ms) / 6.0
        delay_ms = _clipped_gauss(mu, sigma, min_keystroke_ms, max_keystroke_ms)

        # Occasional longer "thinking" pause (~8 % chance)
        if random.random() < 0.08:
            delay_ms += _clipped_gauss(300.0, 100.0, 150.0, 600.0)

        await asyncio.sleep(delay_ms / 1000.0)

    logger.debug("Finished typing into '%s'", selector)
