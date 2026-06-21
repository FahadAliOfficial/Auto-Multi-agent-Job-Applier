"""Structured logging module using Rich library.

Provides pretty terminal output with Rich Console and RichHandler,
plus file logging to data/logs/bot.log. Includes semantic helper
functions for common bot events (job found, applied, skipped, error).
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

from src.control_center import REGISTRY

# ── Rich console singleton ────────────────────────────────────────────
_THEME = Theme(
    {
        "info": "cyan",
        "warning": "yellow",
        "error": "bold red",
        "success": "bold green",
        "job.title": "bold white",
        "job.company": "bold #4fc3f7",
        "job.location": "dim white",
        "skip.reason": "italic yellow",
    }
)

console = Console(theme=_THEME)

# ── Agent context for control center logging ─────────────────────────
_CURRENT_AGENT_ID: ContextVar[str | None] = ContextVar("CURRENT_AGENT_ID", default=None)


def set_current_agent(agent_id: str | None) -> object:
    """Set the current agent id for log routing; returns a reset token."""
    return _CURRENT_AGENT_ID.set(agent_id or None)


def reset_current_agent(token: object) -> None:
    """Reset the current agent id using the provided token."""
    _CURRENT_AGENT_ID.reset(token)


def _strip_rich_markup(message: str) -> str:
    return re.sub(r"\[[^\]]+\]", "", message).strip()


def _render_rich_to_text(*args, **kwargs) -> str:
    temp_console = Console(record=True, force_terminal=False, width=120)
    temp_console.print(*args, **kwargs)
    return temp_console.export_text(clear=True)


def cc_print(*args, **kwargs) -> None:
    """Print to console and mirror the output to the control center if set."""
    console.print(*args, **kwargs)
    agent_id = _CURRENT_AGENT_ID.get()
    if not agent_id:
        return

    try:
        text = _render_rich_to_text(*args, **kwargs).strip("\n")
    except Exception:
        return

    for line in text.splitlines():
        line = _strip_rich_markup(line)
        if line:
            REGISTRY.append_log(agent_id, line)


class ControlCenterLogHandler(logging.Handler):
    """Mirrors log lines to the control center for the active agent."""

    def emit(self, record: logging.LogRecord) -> None:
        agent_id = _CURRENT_AGENT_ID.get()
        if not agent_id:
            return

        try:
            message = record.getMessage()
            message = _strip_rich_markup(message)
            if message:
                REGISTRY.append_log(agent_id, message)
        except Exception:
            return

# ── Log directory setup ───────────────────────────────────────────────
_LOG_DIR = Path("data/logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_DEFAULT_LOG_FILE = _LOG_DIR / "bot.log"


def setup_logger(
    name: str,
    log_file: Path | str | None = None,
    level: str = "INFO",
) -> logging.Logger:
    """Create and configure a logger with both Rich console and file handlers.

    Args:
        name: Logger name (usually ``__name__``).
        log_file: Optional path to a log file.  Defaults to
            ``data/logs/bot.log``.
        level: Logging level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).

    Returns:
        Configured :class:`logging.Logger` instance.
    """
    log_file = Path(log_file) if log_file else _DEFAULT_LOG_FILE
    log_file.parent.mkdir(parents=True, exist_ok=True)

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    _logger = logging.getLogger(name)
    _logger.setLevel(numeric_level)

    # Avoid duplicate handlers when called multiple times
    if _logger.handlers:
        return _logger

    # ── Rich console handler ──────────────────────────────────────
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
        tracebacks_show_locals=True,
        log_time_format="[%Y-%m-%d %H:%M:%S]",
    )
    rich_handler.setLevel(numeric_level)
    rich_fmt = logging.Formatter("%(message)s", datefmt="[%X]")
    rich_handler.setFormatter(rich_fmt)

    # ── File handler ──────────────────────────────────────────────
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(numeric_level)
    file_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)

    _logger.addHandler(rich_handler)
    _logger.addHandler(file_handler)
    _logger.addHandler(ControlCenterLogHandler())

    return _logger


# ── Default logger instance ───────────────────────────────────────────
logger = setup_logger("indeed_bot")


# ── Semantic helper functions ─────────────────────────────────────────

def log_job_found(title: str, company: str, location: str) -> None:
    """Log discovery of a new job listing.

    Args:
        title: Job title.
        company: Company name.
        location: Job location string.
    """
    logger.info(
        "[info]FOUND[/info]  "
        "[job.title]%s[/job.title] at "
        "[job.company]%s[/job.company]  "
        "([job.location]%s[/job.location])",
        title,
        company,
        location,
    )


def log_applied(title: str, company: str) -> None:
    """Log a successful application.

    Args:
        title: Job title.
        company: Company name.
    """
    logger.info(
        "[success]✓ APPLIED[/success]  "
        "[job.title]%s[/job.title] at "
        "[job.company]%s[/job.company]",
        title,
        company,
    )


def log_skipped(title: str, reason: str) -> None:
    """Log a skipped job with the reason.

    Args:
        title: Job title.
        reason: Human-readable reason the job was skipped.
    """
    logger.warning(
        "[warning]⊘ SKIPPED[/warning]  "
        "[job.title]%s[/job.title]  — "
        "[skip.reason]%s[/skip.reason]",
        title,
        reason,
    )


def log_error(title: str, error: str | Exception) -> None:
    """Log an error encountered during processing.

    Args:
        title: Job title (or context label).
        error: The error message or exception.
    """
    logger.error(
        "[error]✗ ERROR[/error]  "
        "[job.title]%s[/job.title]  — %s",
        title,
        error,
    )
