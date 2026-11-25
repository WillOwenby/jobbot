"""Readers for LinkedIn's public (logged-out) job pages. No browser needed."""
from __future__ import annotations

import html
import re

from .models import Job

LISTING_CARD_SPLIT = re.compile(r'(?=<li[\s>])')


def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_listing(text: str) -> list[Job]:
    """Parse the job-listing feed's HTML cards into Jobs."""
    jobs = []
    for card in LISTING_CARD_SPLIT.split(text):
        m = (re.search(r"urn:li:jobPosting:(\d+)", card)
             or re.search(r"/jobs/view/(?:[^\"/?]*?-)?(\d{6,})", card))
        if not m:
            continue
        def field(cls: str) -> str:
            f = re.search(rf'class="[^"]*\b{cls}\b[^"]*"[^>]*>(.*?)</(?:h3|h4|span|a|div)>', card, re.S)
            return _strip_tags(f.group(1)) if f else ""
        title = field("base-search-card__title")
        if title:
            jobs.append(Job(
                site="linkedin", job_id=m.group(1), title=title,
                company=field("base-search-card__subtitle") or field("hidden-nested-link"),
                location=field("job-search-card__location"),
                url=f"https://www.linkedin.com/jobs/view/{m.group(1)}/",
            ))
    return jobs


def parse_posting(text: str) -> dict:
    """Parse a public job page into title, company, location and description."""
    def first(cls: str, tag: str = r"\w+") -> str:
        m = re.search(rf'class="[^"]*\b{cls}\b[^"]*"[^>]*>(.*?)</{tag}>', text, re.S)
        return _strip_tags(m.group(1)) if m else ""
    desc = re.search(r'class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', text, re.S)
    description = ""
    if desc:
        block = re.sub(r"<br\s*/?>|</(p|li|ul|ol|h\d)>", "\n", desc.group(1))
        description = "\n".join(
            line for line in (re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", ln))).strip()
                              for ln in block.split("\n")) if line)
    return {
        "title": first("top-card-layout__title"),
        "company": first("topcard__org-name-link", "a"),
        "location": first("topcard__flavor--bullet", "span"),
        "description": description,
    }

