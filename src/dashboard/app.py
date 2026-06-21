"""Flask + HTMX web dashboard for the Indeed Easy Apply Bot.

Provides a dark-themed UI to monitor applications, review jobs,
manage screening question answers, and track session statistics.

Usage::

    from src.dashboard.app import create_app

    app = create_app("data/indeed_bot.db")
    app.run(debug=True, port=5000)
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    render_template,
    request,
)
from src.control_center import REGISTRY


# ── Database helpers ──────────────────────────────────────────────────

def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    """Convert an SQLite row to a :class:`dict` keyed by column name."""
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


@contextmanager
def _get_db(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """Context-managed SQLite connection with dict row factory.

    Args:
        db_path: Path to the SQLite database file.

    Yields:
        An open :class:`sqlite3.Connection`.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def _get_request_db() -> sqlite3.Connection:
    """Return a per-request database connection stored on ``g``."""
    if "db" not in g:
        g.db = sqlite3.connect(g.db_path)
        g.db.row_factory = _dict_factory
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


# ── Ensure tables exist (aligned with src/database.py schema) ────────

_INIT_SQL = """
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
"""


def _ensure_tables(db_path: str) -> None:
    """Create tables if they do not already exist.

    Args:
        db_path: Path to the SQLite database.
    """
    with _get_db(db_path) as conn:
        conn.executescript(_INIT_SQL)
        conn.commit()


# ── App factory ───────────────────────────────────────────────────────

def create_app(db_path: str = "data/indeed_bot.db") -> Flask:
    """Application factory for the dashboard.

    Args:
        db_path: Path to the SQLite database file used by the bot.

    Returns:
        Configured :class:`Flask` application.
    """
    db_path_resolved = str(Path(db_path).resolve())
    Path(db_path_resolved).parent.mkdir(parents=True, exist_ok=True)

    _ensure_tables(db_path_resolved)

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
    )
    app.config["SECRET_KEY"] = "indeed-bot-dashboard-dev-key"

    # Store db_path on g for every request
    @app.before_request
    def _inject_db_path() -> None:
        g.db_path = db_path_resolved

    @app.teardown_appcontext
    def _close_db(exc: BaseException | None) -> None:  # noqa: ARG001
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # ── Template helpers ──────────────────────────────────────────
    @app.template_filter("status_color")
    def _status_color(status: str) -> str:
        return {
            "applied": "#4caf50",
            "skipped": "#ffb74d",
            "failed": "#ef5350",
            "found": "#9e9e9e",
        }.get((status or "").lower(), "#9e9e9e")

    @app.template_filter("status_badge")
    def _status_badge(status: str) -> str:
        color = _status_color(status)
        label = (status or "unknown").upper()
        return (
            f'<span style="background:{color};color:#fff;padding:2px 10px;'
            f'border-radius:12px;font-size:0.78rem;font-weight:600;">'
            f"{label}</span>"
        )

    # ── Routes ────────────────────────────────────────────────────

    @app.route("/")
    def index():
        """Dashboard home — stats + recent applications."""
        db = _get_request_db()
        stats = _fetch_stats(db)
        recent = db.execute(
            "SELECT * FROM jobs ORDER BY found_at DESC LIMIT 20"
        ).fetchall()
        return render_template("index.html", stats=stats, recent=recent)

    @app.route("/jobs")
    def jobs_list():
        """Paginated, filterable jobs list."""
        db = _get_request_db()
        status = request.args.get("status", "")
        search = request.args.get("search", "")
        page_num = max(1, request.args.get("page", 1, type=int))
        per_page = 25

        where_clauses: list[str] = []
        params: list[Any] = []

        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if search:
            where_clauses.append(
                "(title LIKE ? OR company LIKE ? OR location LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like])

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        total = db.execute(
            f"SELECT COUNT(*) AS cnt FROM jobs{where_sql}", params
        ).fetchone()["cnt"]

        total_pages = max(1, math.ceil(total / per_page))
        offset = (page_num - 1) * per_page

        rows = db.execute(
            f"SELECT * FROM jobs{where_sql} ORDER BY found_at DESC LIMIT ? OFFSET ?",
            [*params, per_page, offset],
        ).fetchall()

        return render_template(
            "jobs.html",
            jobs=rows,
            page=page_num,
            total_pages=total_pages,
            total=total,
            status=status,
            search=search,
        )

    @app.route("/jobs/<int:job_id>")
    def job_detail(job_id: int):
        """Individual job detail page."""
        db = _get_request_db()
        job = db.execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone()
        if not job:
            abort(404)
        return render_template("job_detail.html", job=job)

    @app.route("/sessions")
    def sessions_list():
        """List of search sessions with stats."""
        db = _get_request_db()
        rows = db.execute(
            "SELECT * FROM search_sessions ORDER BY started_at DESC"
        ).fetchall()
        return render_template("sessions.html", sessions=rows)

    @app.route("/control-center")
    def control_center():
        """Live multi-agent control center."""
        snap = REGISTRY.snapshot()
        return render_template("control_center.html", snap=snap)

    @app.route("/api/agents")
    def api_agents():
        return jsonify(REGISTRY.snapshot())

    @app.route("/api/agents/<agent_id>/logs")
    def api_agent_logs(agent_id: str):
        return jsonify({"agent_id": agent_id, "logs": REGISTRY.logs(agent_id)})

    @app.route("/api/agents/<agent_id>/action", methods=["POST"])
    def api_agent_action(agent_id: str):
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action", "")).strip()
        ok = REGISTRY.action(agent_id, action)
        return jsonify({"ok": ok, "agent_id": agent_id, "action": action}), (200 if ok else 400)

    @app.route("/api/agents/<agent_id>/prompt")
    def api_agent_prompt(agent_id: str):
        return jsonify({"agent_id": agent_id, "prompt": REGISTRY.get_prompt(agent_id)})

    @app.route("/api/agents/<agent_id>/answer", methods=["POST"])
    def api_agent_answer(agent_id: str):
        payload = request.get_json(silent=True) or {}
        answer = str(payload.get("answer", ""))
        ok = REGISTRY.set_answer(agent_id, answer)
        return jsonify({"ok": ok, "agent_id": agent_id}), (200 if ok else 400)

    @app.route("/api/agents/<agent_id>/job")
    def api_agent_job(agent_id: str):
        return jsonify({"agent_id": agent_id, "job": REGISTRY.get_job_info(agent_id)})

    @app.route("/questions", methods=["GET"])
    def questions_list():
        """List screening questions and saved answers."""
        db = _get_request_db()
        search = request.args.get("search", "")

        if search:
            rows = db.execute(
                "SELECT * FROM screening_answers WHERE question LIKE ? "
                "ORDER BY times_used DESC",
                [f"%{search}%"],
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM screening_answers ORDER BY times_used DESC"
            ).fetchall()

        return render_template("questions.html", questions=rows, search=search)

    @app.route("/questions/<int:q_id>/answer", methods=["POST"])
    def update_answer(q_id: int):
        """HTMX endpoint — update a question's saved answer."""
        db = _get_request_db()
        new_answer = request.form.get("answer", "").strip()
        db.execute(
            "UPDATE screening_answers SET answer = ?, was_manual = 1 WHERE id = ?",
            [new_answer, q_id],
        )
        db.commit()

        row = db.execute(
            "SELECT * FROM screening_answers WHERE id = ?", [q_id]
        ).fetchone()
        if not row:
            abort(404)

        # Return just the updated table row for HTMX swap
        return render_template("_question_row.html", q=row)

    @app.route("/api/stats")
    def api_stats():
        """JSON stats endpoint for HTMX polling."""
        db = _get_request_db()
        return jsonify(_fetch_stats(db))

    return app


# ── Internal helpers ──────────────────────────────────────────────────

def _fetch_stats(db: sqlite3.Connection) -> dict[str, Any]:
    """Query aggregate statistics from the jobs table.

    Args:
        db: Open database connection.

    Returns:
        Dict with keys: total_found, applied, skipped, failed, success_rate.
    """
    row = db.execute(
        """
        SELECT
            COUNT(*)                                    AS total_found,
            SUM(CASE WHEN status='applied' THEN 1 ELSE 0 END) AS applied,
            SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
            SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed
        FROM jobs
        """
    ).fetchone()

    total = row["total_found"] or 0
    applied = row["applied"] or 0
    skipped = row["skipped"] or 0
    failed = row["failed"] or 0
    success_rate = round((applied / max(applied + failed, 1) * 100), 1)

    return {
        "total_found": total,
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
        "success_rate": success_rate,
    }


# ── Standalone entry point ────────────────────────────────────────────

if __name__ == "__main__":
    application = create_app()
    application.run(debug=True, host="127.0.0.1", port=5000)
