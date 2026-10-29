from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class Job:
    site: str
    job_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    fit_score: int | None = None
    fit_reason: str = ""
    concerns: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.site}:{self.job_id}"


class Status(str, Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    SKIPPED = "skipped"
    FLAGGED = "flagged"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass
class ApplyResult:
    status: Status
    detail: str = ""
    questions: list[str] = field(default_factory=list)
