# src/handlers/question_matcher.py
"""Fuzzy-matches screening questions to previously saved answers.

Maintains a database table of question–answer pairs so that recurring
screening questions can be answered automatically.  When no match is
found, prompts the user interactively via the terminal using Rich.

Integrates with the ``screening_answers`` table managed by
:class:`src.database.Database`.
"""
from __future__ import annotations

import asyncio
import difflib
import re
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from src.utils.logger import logger

if TYPE_CHECKING:
    import aiosqlite

console = Console()

# Minimum similarity ratio to consider a saved answer a confident match.
CONFIDENCE_THRESHOLD: float = 0.85


class QuestionMatcher:
    """Fuzzy-match screening questions against a local answer database.

    The ``screening_answers`` table schema (created by
    :class:`src.database.Database`) is::

        id, job_id, question, answer, confidence, was_manual,
        times_used, answered_at

    Args:
        db: An open :class:`aiosqlite.Connection` with the
            ``screening_answers`` table already created.

    Usage::

        matcher = QuestionMatcher(db)
        answer = await matcher.get_or_ask("Do you have a driver's license?")
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Fuzzy search
    # ------------------------------------------------------------------

    async def find_answer(self, question: str, *, fuzzy: bool = True) -> tuple[str | None, float]:
        """Search saved answers for the closest match to *question*.

        Uses :func:`difflib.SequenceMatcher.ratio` to score similarity
        between the incoming question and each stored question.

        Args:
            question: The screening question text to look up.

        Returns:
            A ``(answer, confidence)`` tuple.  *confidence* is a float
            in ``[0.0, 1.0]``.  If no match exceeds
            :data:`CONFIDENCE_THRESHOLD`, returns ``(None, 0.0)``.
        """
        if not fuzzy:
            cursor = await self._db.execute(
                "SELECT id, answer, times_used FROM screening_answers "
                "WHERE question = ? "
                "ORDER BY times_used DESC, answered_at DESC "
                "LIMIT 1",
                (question,),
            )
            row = await cursor.fetchone()
            if row is None:
                return (None, 0.0)

            row_id = row[0] if isinstance(row, tuple) else row["id"]
            await self._db.execute(
                "UPDATE screening_answers "
                "SET times_used = times_used + 1, answered_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (row_id,),
            )
            await self._db.commit()
            answer = row[1] if isinstance(row, tuple) else row["answer"]
            return (answer, 1.0)

        cursor = await self._db.execute(
            "SELECT question, answer FROM screening_answers "
            "ORDER BY times_used DESC, answered_at DESC"
        )
        rows = await cursor.fetchall()

        if not rows:
            return (None, 0.0)

        best_answer: str | None = None
        best_ratio: float = 0.0
        question_normalized = self._normalize(question)

        for row in rows:
            saved_question = row[0] if isinstance(row, tuple) else row["question"]
            saved_answer = row[1] if isinstance(row, tuple) else row["answer"]

            ratio = difflib.SequenceMatcher(
                None,
                question_normalized,
                self._normalize(saved_question),
            ).ratio()

            if ratio > best_ratio:
                best_ratio = ratio
                best_answer = saved_answer

        if best_ratio >= CONFIDENCE_THRESHOLD:
            logger.debug(
                "Matched saved answer (confidence %.0f%%)",
                best_ratio * 100,
            )
            return (best_answer, best_ratio)

        return (None, 0.0)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def save_answer(
        self,
        question: str,
        answer: str,
        job_id: int | None = None,
    ) -> None:
        """Persist a question–answer pair to the database.

        If the exact question+answer already exists, ``times_used`` is
        incremented.  Otherwise a new row is inserted with
        ``was_manual=1`` and ``confidence=1.0``.

        Args:
            question: The screening question text.
            answer: The user-supplied or auto-matched answer.
            job_id: Optional job ID to associate the answer with.
        """
        # Check for an existing identical question+answer
        cursor = await self._db.execute(
            "SELECT id, times_used FROM screening_answers "
            "WHERE question = ? AND answer = ?",
            (question, answer),
        )
        existing = await cursor.fetchone()

        if existing:
            row_id = existing[0] if isinstance(existing, tuple) else existing["id"]
            await self._db.execute(
                "UPDATE screening_answers "
                "SET times_used = times_used + 1, "
                "    answered_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (row_id,),
            )
        else:
            await self._db.execute(
                "INSERT INTO screening_answers "
                "(job_id, question, answer, confidence, was_manual) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, question, answer, 1.0, True),
            )

        await self._db.commit()

        display_q = f"{question[:60]}…" if len(question) > 60 else question
        logger.debug("Saved screening answer for: %s", display_q)

    # ------------------------------------------------------------------
    # Interactive prompt
    # ------------------------------------------------------------------

    async def prompt_user(
        self,
        question: str,
        options: list[str] | None = None,
        allow_empty: bool = False,
    ) -> str:
        """Ask the user to answer a screening question via the terminal.

        Runs the blocking Rich prompt in a thread executor so the async
        event loop is not blocked.

        Args:
            question: The question text to display.
            options: Optional list of answer choices (e.g., from radio
                buttons or a dropdown).

        Returns:
            The user's answer string.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._prompt_sync,
            question,
            options,
            allow_empty,
        )

    def _prompt_sync(
        self,
        question: str,
        options: list[str] | None = None,
        allow_empty: bool = False,
    ) -> str:
        """Synchronous Rich prompt shown in the terminal."""
        console.print()
        console.print(
            Panel(
                question,
                title="[bold yellow]Screening Question[/bold yellow]",
                border_style="yellow",
                expand=False,
            )
        )

        if options:
            console.print("[bold]Available options:[/bold]")
            for idx, opt in enumerate(options, 1):
                console.print(f"  [cyan]{idx}.[/cyan] {opt}")
            console.print()

            choice = Prompt.ask(
                "[bold]Enter option number or type your answer[/bold]",
                default="" if allow_empty else None,
                show_default=False,
            )

            # If the user typed a number, map it to the option
            if choice.isdigit():
                choice_idx = int(choice) - 1
                if 0 <= choice_idx < len(options):
                    return options[choice_idx]

            return choice
        else:
            return Prompt.ask(
                "[bold]Your answer[/bold]",
                default="" if allow_empty else None,
                show_default=False,
            )

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    async def get_or_ask(
        self,
        question: str,
        options: list[str] | None = None,
        job_id: int | None = None,
        fuzzy: bool = True,
    ) -> str:
        """Find a saved answer or prompt the user, then persist the result.

        This is the primary entry-point for screening question handling.

        Args:
            question: The screening question text.
            options: Optional answer choices.
            job_id: Optional job ID for context.

        Returns:
            The final answer string (from DB or user input).
        """
        # 1. Try the database first
        saved_answer, confidence = await self.find_answer(question, fuzzy=fuzzy)
        if saved_answer is not None and confidence >= CONFIDENCE_THRESHOLD:
            logger.info(
                "[success]✓ AUTO-ANSWER[/success]  %r → %r (confidence %.0f%%)",
                question[:50],
                saved_answer,
                confidence * 100,
            )
            return saved_answer

        # 2. No confident match – ask the user
        logger.info(
            "[warning]? MANUAL[/warning]  No saved answer for: %s",
            question[:60],
        )
        answer = await self.prompt_user(question, options)

        # 3. Save for future use
        await self.save_answer(question, answer, job_id)

        return answer

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize text for fuzzy comparison.

        Lower-cases, strips whitespace, and collapses punctuation so
        that trivially different phrasings match more easily.
        """
        text = text.lower().strip()
        # Collapse multiple whitespace / punctuation
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text
