"""Keyword filters applied before spending time (or AI tokens) on a job."""
from __future__ import annotations

import re

from .config import FilterConfig
from .models import Job


def find_word(text: str, words: list[str]) -> str | None:
    """Return the first of `words` that appears in `text` as a whole word/phrase."""
    for w in words:
        # Lookarounds instead of \b so terms like "C++" or ".NET" still match.
        if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text or "", re.I):
            return w
    return None


def reject_reason(job: Job, f: FilterConfig) -> str | None:
    if f.title_include and not find_word(job.title, f.title_include):
        return "title has none of the include keywords"
    if w := find_word(job.title, f.title_exclude):
        return f"title contains '{w}'"
    if w := find_word(job.company, f.company_exclude):
        return f"excluded company '{w}'"
    if job.description and (w := find_word(job.description, f.description_exclude)):
        return f"description contains '{w}'"
    return None
