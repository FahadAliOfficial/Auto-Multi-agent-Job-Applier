"""
Database module for Indeed Easy Apply Bot.

Manages all persistent storage using async SQLite:
- Jobs found during searches
- Application tracking and history
- Search session records
- Screening question answers (learn-as-you-go)
"""
from __future__ import annotations

import aiosqlite
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class Job:
    """Represents a job listing found on Indeed."""
    id: int | None = None
    indeed_job_id: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    salary: str = ""
    job_type: str = ""
    url: str = ""
    description: str = ""
    status: str = "found"  # found | applied | skipped | failed
    found_at: str = ""
    applied_at: str | None = None
    notes: str = ""


@dataclass
class SearchSession:
    """Tracks a single search session run."""
    id: int | None = None
    query: str = ""
    location: str = ""
    filters: str = ""
    started_at: str = ""
    ended_at: str | None = None
    jobs_found: int = 0
    jobs_applied: int = 0
    jobs_skipped: int = 0


@dataclass
class ScreeningAnswer:
    """A saved screening question + answer pair."""
    id: int | None = None
    job_id: int | None = None
    question: str = ""
    answer: str = ""
    confidence: float = 1.0
    was_manual: bool = False
    times_used: int = 1
    answered_at: str = ""


# ---------------------------------------------------------------------------
# Database Manager
# ---------------------------------------------------------------------------

class Database:
    """Async SQLite database manager for the Indeed bot."""

    def __init__(self, db_path: str | Path = "data/indeed_bot.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open database connection and initialize schema."""
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _create_tables(self) -> None:
        """Create tables if they don't exist."""
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                indeed_job_id   TEXT UNIQUE,
                title           TEXT NOT NULL,
                company         TEXT DEFAULT '',
                location        TEXT DEFAULT '',
                salary          TEXT DEFAULT '',
                job_type        TEXT DEFAULT '',
                url             TEXT NOT NULL,
                description     TEXT DEFAULT '',
                status          TEXT DEFAULT 'found'
                                    CHECK(status IN ('found','applied','skipped','failed')),
                found_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                applied_at      TIMESTAMP,
                notes           TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS search_sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                query           TEXT,
                location        TEXT,
                filters         TEXT DEFAULT '',
                started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at        TIMESTAMP,
                jobs_found      INTEGER DEFAULT 0,
                jobs_applied    INTEGER DEFAULT 0,
                jobs_skipped    INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS screening_answers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          INTEGER REFERENCES jobs(id),
                question        TEXT NOT NULL,
                answer          TEXT NOT NULL,
                confidence      REAL DEFAULT 1.0,
                was_manual      BOOLEAN DEFAULT 0,
                times_used      INTEGER DEFAULT 1,
                answered_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_indeed_id ON jobs(indeed_job_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_screening_question ON screening_answers(question);
        """)
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Jobs CRUD
    # ------------------------------------------------------------------

    async def add_job(self, job: Job) -> int:
        """Insert a new job. Returns the row ID."""
        cursor = await self.conn.execute(
            """INSERT OR IGNORE INTO jobs
               (indeed_job_id, title, company, location, salary, job_type, url, description, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job.indeed_job_id, job.title, job.company, job.location,
             job.salary, job.job_type, job.url, job.description, job.status),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def job_exists(self, indeed_job_id: str) -> bool:
        """Check if a job has already been recorded."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM jobs WHERE indeed_job_id = ?", (indeed_job_id,)
        )
        return await cursor.fetchone() is not None

    async def is_already_applied(self, indeed_job_id: str) -> bool:
        """Check if we already applied to this job."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM jobs WHERE indeed_job_id = ? AND status = 'applied'",
            (indeed_job_id,),
        )
        return await cursor.fetchone() is not None

    async def update_job_status(
        self, indeed_job_id: str, status: str, notes: str = ""
    ) -> None:
        """Update job status (applied, skipped, failed)."""
        params: list = [status]
        sql = "UPDATE jobs SET status = ?"
        if status == "applied":
            sql += ", applied_at = CURRENT_TIMESTAMP"
        if notes:
            sql += ", notes = ?"
            params.append(notes)
        sql += " WHERE indeed_job_id = ?"
        params.append(indeed_job_id)
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def get_job(self, indeed_job_id: str) -> Job | None:
        """Fetch a single job by Indeed ID."""
        cursor = await self.conn.execute(
            "SELECT * FROM jobs WHERE indeed_job_id = ?", (indeed_job_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    async def get_jobs(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        """Fetch jobs with optional status filter."""
        sql = "SELECT * FROM jobs"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY found_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_job(r) for r in rows]

    async def count_jobs(self, status: str | None = None) -> int:
        """Count jobs with optional status filter."""
        sql = "SELECT COUNT(*) FROM jobs"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Search Sessions
    # ------------------------------------------------------------------

    async def start_session(self, query: str, location: str, filters: str = "") -> int:
        """Create a new search session. Returns session ID."""
        cursor = await self.conn.execute(
            """INSERT INTO search_sessions (query, location, filters)
               VALUES (?, ?, ?)""",
            (query, location, filters),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def end_session(
        self, session_id: int, jobs_found: int, jobs_applied: int, jobs_skipped: int
    ) -> None:
        """Finalize a search session with stats."""
        await self.conn.execute(
            """UPDATE search_sessions
               SET ended_at = CURRENT_TIMESTAMP,
                   jobs_found = ?, jobs_applied = ?, jobs_skipped = ?
               WHERE id = ?""",
            (jobs_found, jobs_applied, jobs_skipped, session_id),
        )
        await self.conn.commit()

    async def get_sessions(self, limit: int = 20) -> list[SearchSession]:
        """Fetch recent search sessions."""
        cursor = await self.conn.execute(
            "SELECT * FROM search_sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            SearchSession(
                id=r["id"], query=r["query"], location=r["location"],
                filters=r["filters"], started_at=r["started_at"],
                ended_at=r["ended_at"], jobs_found=r["jobs_found"],
                jobs_applied=r["jobs_applied"], jobs_skipped=r["jobs_skipped"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Screening Answers
    # ------------------------------------------------------------------

    async def save_screening_answer(
        self,
        question: str,
        answer: str,
        job_id: int | None = None,
        confidence: float = 1.0,
        was_manual: bool = False,
    ) -> int:
        """Save a screening question answer. If the exact question exists, increment times_used."""
        # Check for exact duplicate
        cursor = await self.conn.execute(
            "SELECT id, times_used FROM screening_answers WHERE question = ? AND answer = ?",
            (question, answer),
        )
        existing = await cursor.fetchone()
        if existing:
            await self.conn.execute(
                "UPDATE screening_answers SET times_used = times_used + 1, answered_at = CURRENT_TIMESTAMP WHERE id = ?",
                (existing["id"],),
            )
            await self.conn.commit()
            return existing["id"]

        cursor = await self.conn.execute(
            """INSERT INTO screening_answers (job_id, question, answer, confidence, was_manual)
               VALUES (?, ?, ?, ?, ?)""",
            (job_id, question, answer, confidence, was_manual),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def get_screening_answers(self) -> list[ScreeningAnswer]:
        """Fetch all saved screening answers."""
        cursor = await self.conn.execute(
            "SELECT * FROM screening_answers ORDER BY times_used DESC"
        )
        rows = await cursor.fetchall()
        return [
            ScreeningAnswer(
                id=r["id"], job_id=r["job_id"], question=r["question"],
                answer=r["answer"], confidence=r["confidence"],
                was_manual=bool(r["was_manual"]), times_used=r["times_used"],
                answered_at=r["answered_at"],
            )
            for r in rows
        ]

    async def find_similar_answer(self, question: str) -> tuple[str | None, float]:
        """Find a saved answer for a similar question using basic text matching.

        Returns (answer, confidence) or (None, 0.0) if no match.
        """
        import difflib

        cursor = await self.conn.execute("SELECT question, answer FROM screening_answers")
        rows = await cursor.fetchall()
        if not rows:
            return None, 0.0

        best_answer = None
        best_ratio = 0.0
        question_lower = question.lower().strip()

        for row in rows:
            saved_q = row["question"].lower().strip()
            ratio = difflib.SequenceMatcher(None, question_lower, saved_q).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_answer = row["answer"]

        if best_ratio >= 0.85:
            return best_answer, best_ratio
        return None, best_ratio

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict:
        """Get aggregate statistics for the dashboard."""
        stats = {}
        for status in ("found", "applied", "skipped", "failed"):
            stats[status] = await self.count_jobs(status)
        stats["total"] = sum(stats.values())
        stats["success_rate"] = (
            round(stats["applied"] / max(stats["applied"] + stats["failed"], 1) * 100, 1)
        )
        return stats

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_job(row) -> Job:
        return Job(
            id=row["id"],
            indeed_job_id=row["indeed_job_id"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            salary=row["salary"],
            job_type=row["job_type"],
            url=row["url"],
            description=row["description"],
            status=row["status"],
            found_at=row["found_at"],
            applied_at=row["applied_at"],
            notes=row["notes"],
        )
