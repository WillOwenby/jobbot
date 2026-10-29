"""SQLite log of every job seen, plus a cache of AI-generated answers."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone

from .models import ApplyResult, Job, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    key TEXT PRIMARY KEY, site TEXT, job_id TEXT, title TEXT, company TEXT,
    location TEXT, url TEXT, status TEXT, detail TEXT, questions TEXT,
    fit_score INTEGER, fit_reason TEXT, concerns TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_answers (
    question TEXT PRIMARY KEY, answer TEXT, created_at TEXT
);
"""

DONE = (Status.APPLIED.value, Status.ALREADY_APPLIED.value)
# Jobs already judged; not re-scored on later runs unless --recheck is passed.
JUDGED = DONE + (Status.SKIPPED.value, Status.FLAGGED.value, Status.APPLY_YOURSELF.value)
UNSCORED_PREFIXES = ("not scored", "couldn't read the job description")
# Skips that depended on the page (e.g. logged out), so they're looked at again next run.
RETRY_SKIPS = ("no Easy Apply button",)


class Tracker:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def is_done(self, job: Job, recheck: bool = False) -> bool:
        row = self.conn.execute("SELECT status, detail FROM applications WHERE key = ?", (job.key,)).fetchone()
        if not row:
            return False
        status, detail = row
        if status == Status.FLAGGED.value and detail and detail.startswith(UNSCORED_PREFIXES):
            return False  # flagged only because scoring failed (older versions); score it now
        if status == Status.SKIPPED.value and detail in RETRY_SKIPS:
            return False
        return status in (DONE if recheck else JUDGED)

    def judged_duplicate(self, job: Job) -> sqlite3.Row | None:
        """Another listing of the same job (same title and company) that was already judged.
        Companies often post one job in several cities, each with its own listing."""
        if not job.title or not job.company:
            return None
        self.conn.row_factory = sqlite3.Row
        try:
            rows = self.conn.execute(
                "SELECT key, status, detail FROM applications WHERE key != ? "
                "AND lower(trim(title)) = lower(trim(?)) AND lower(trim(company)) = lower(trim(?))",
                (job.key, job.title, job.company)).fetchall()
        finally:
            self.conn.row_factory = None
        for row in rows:
            unscored = row["status"] == Status.FLAGGED.value and (row["detail"] or "").startswith(UNSCORED_PREFIXES)
            retry = row["status"] == Status.SKIPPED.value and row["detail"] in RETRY_SKIPS
            if row["status"] in JUDGED and not unscored and not retry:
                return row
        return None

    def record(self, job: Job, result: ApplyResult) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO applications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.key, job.site, job.job_id, job.title, job.company, job.location, job.url,
             result.status.value, result.detail, json.dumps(result.questions),
             job.fit_score, job.fit_reason, json.dumps(job.concerns),
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def rows(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        try:
            return self.conn.execute("SELECT * FROM applications ORDER BY updated_at DESC").fetchall()
        finally:
            self.conn.row_factory = None

    def export_csv(self, path: str) -> int:
        rows = self.rows()
        with open(path, "w", newline="") as fh:
            writer = csv.writer(fh)
            if rows:
                writer.writerow(rows[0].keys())
                writer.writerows(tuple(r) for r in rows)
        return len(rows)

    def shortlist(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        try:
            return self.conn.execute(
                "SELECT * FROM applications WHERE status = ? ORDER BY fit_score DESC, updated_at DESC",
                (Status.APPLY_YOURSELF.value,)).fetchall()
        finally:
            self.conn.row_factory = None

    def unanswered_questions(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for (raw,) in self.conn.execute(
            "SELECT questions FROM applications WHERE status = ?", (Status.NEEDS_INPUT.value,)
        ):
            for q in json.loads(raw or "[]"):
                counts[q] = counts.get(q, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])

    def cached_answer(self, question: str) -> str | None:
        row = self.conn.execute("SELECT answer FROM ai_answers WHERE question = ?", (question,)).fetchone()
        return row[0] if row else None

    def cache_answer(self, question: str, answer: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ai_answers VALUES (?,?,?)",
            (question, answer, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def ai_answers(self) -> list[tuple[str, str]]:
        return self.conn.execute("SELECT question, answer FROM ai_answers ORDER BY question").fetchall()
