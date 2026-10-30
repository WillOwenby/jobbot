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
    workplace: str = ""  # "Remote", "Hybrid" or "On-site" when the site says so
    external: bool = False  # applies on the company's own site, so jobbot can't apply
    easy_apply: bool | None = None  # from which search it came; None = unknown

    @property
    def key(self) -> str:
        return f"{self.site}:{self.job_id}"


class Status(str, Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    SKIPPED = "skipped"
    FLAGGED = "flagged"
    APPLY_YOURSELF = "apply_yourself"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass
class ApplyResult:
    status: Status
    detail: str = ""
    questions: list[str] = field(default_factory=list)
