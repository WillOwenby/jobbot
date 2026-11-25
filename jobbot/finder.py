"""Job finder: search public job sources and write unseen candidates for scoring.

No browser, no logins, no API keys. Sources:
  linkedin   LinkedIn's public job search (the logged-out pages anyone can open)
  himalayas  Himalayas remote-jobs API
  boards     companies' own public job boards (Greenhouse, Lever, Ashby)

    python -m jobbot.finder --settings settings.json --seen seen.txt --out candidates.json

settings.json holds the search terms and filters (see FINDER_RUNBOOK.md). seen.txt lists job
keys already judged, one per line; they're skipped without being fetched again.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from .config import FilterConfig
from .filters import find_word, reject_reason
from .linkedin_public import parse_listing, parse_posting
from .models import Job

log = logging.getLogger("finder")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
LEADERSHIP = ["manager", "director", "head", "lead", "leader", "vp", "vice president", "chief",
              "principal", "program", "architect", "owner", "officer", "senior manager", "supervisor"]
MAX_DESCRIPTION = 6000


@dataclass
class Settings:
    keywords: list[str]
    title_exclude: list[str] = field(default_factory=list)
    title_include: list[str] = field(default_factory=list)
    company_exclude: list[str] = field(default_factory=list)
    description_exclude: list[str] = field(default_factory=list)
    # Words that make a job from a broad source (Himalayas, company boards) worth scoring.
    relevance_words: list[str] = field(default_factory=lambda: [
        "health", "healthcare", "clinical", "epic", "ehr", "emr", "hl7", "fhir", "automation",
        "rpa", "uipath", "implementation", "integration", "informatics", "revenue cycle", "applications"])
    company_boards: list[str] = field(default_factory=list)  # "greenhouse:slug", "lever:slug", "ashby:slug"
    sources: list[str] = field(default_factory=lambda: ["linkedin", "himalayas", "boards"])
    days: int = 1                  # how far back to look
    max_candidates: int = 40       # descriptions fetched and handed to scoring per run
    linkedin_delay: tuple = (3, 7)  # seconds between LinkedIn requests
    linkedin_minutes: float = 8     # time budget for LinkedIn per run; the rest waits for the next run

    @property
    def filters(self) -> FilterConfig:
        return FilterConfig(self.title_include, self.title_exclude, self.company_exclude,
                            self.description_exclude)


@dataclass
class Candidate:
    key: str
    source: str
    title: str
    company: str
    location: str
    url: str
    posted: str = ""
    salary: str = ""
    description: str = ""


def doc_id(key: str) -> str:
    """A job key that is safe as an app database document id."""
    return re.sub(r"[^A-Za-z0-9_\-.~:@+]", "-", key)[:180]


def text_of(fragment: str) -> str:
    block = re.sub(r"<br\s*/?>|</(p|li|ul|ol|h\d|div)>", "\n", fragment or "")
    lines = (re.sub(r"[ \t\xa0]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", ln))).strip()
             for ln in block.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def http_get(url: str, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        log.debug("GET %s failed: %s", url, e)
        return 0, ""


def money(lo, hi, period="year", currency="USD") -> str:
    def fmt(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return ""
        return f"${v / 1000:.0f}k" if v >= 1000 else f"${v:.0f}"
    lo, hi = fmt(lo), fmt(hi)
    if not (lo or hi):
        return ""
    rng = f"{lo}–{hi}" if lo and hi and lo != hi else (lo or hi)
    return rng + ("" if period in ("year", "annual", "YEAR", None) else f" per {period}")


class Finder:
    def __init__(self, settings: Settings, seen: set[str], fetch=http_get, sleep=time.sleep):
        self.s = settings
        self.seen = seen
        self.fetch = fetch
        self.sleep = sleep
        self.found: dict[str, Candidate] = {}
        self._titles: set[tuple[str, str]] = set()
        self.counts: dict[str, int] = {}
        self.notes: list[str] = []

    # ---- shared ----
    def _consider(self, c: Candidate, broad: bool) -> bool:
        """Keep a job if it's new, passes the title rules and (for broad sources) looks relevant."""
        c.key = doc_id(c.key)
        if c.key in self.seen or c.key in self.found:
            return False
        same = (c.title.strip().lower(), c.company.strip().lower())
        if same in self._titles:
            return False
        job = Job(c.source, c.key, c.title, c.company, c.location, c.url, c.description)
        if reject_reason(job, self.s.filters):
            return False
        if broad:
            if not find_word(c.title, LEADERSHIP):
                return False
            if not find_word(f"{c.title} {c.company} {c.description[:3000]}", self.s.relevance_words):
                return False
        self._titles.add(same)
        self.found[c.key] = c
        self.counts[c.source] = self.counts.get(c.source, 0) + 1
        return True

    def run(self) -> list[Candidate]:
        for source in self.s.sources:
            try:
                getattr(self, f"_{source}")()
            except Exception as e:  # one broken source shouldn't sink the run
                self.notes.append(f"{source}: {type(e).__name__}: {e}")
                log.warning("%s failed: %s", source, e)
        ranked = sorted(self.found.values(), key=self._priority, reverse=True)
        keep = ranked[: self.s.max_candidates]
        if len(ranked) > len(keep):
            self.notes.append(f"{len(ranked) - len(keep)} more candidates left for the next run")
        self._fill_linkedin_descriptions([c for c in keep if c.source == "linkedin" and not c.description])
        return [c for c in keep if len(c.description) >= 200]

    def _priority(self, c: Candidate) -> int:
        words = {w.lower() for k in self.s.keywords for w in re.findall(r"[A-Za-z0-9]+", k) if len(w) > 2}
        title = set(re.findall(r"[a-z0-9]+", c.title.lower()))
        score = len(words & title) * 3 + (2 if find_word(c.title, self.s.relevance_words) else 0)
        return score + (3 if c.source != "linkedin" else 0)  # broad sources already passed a relevance check

    # ---- LinkedIn public search ----
    def _li_get(self, url: str) -> str:
        started = self.__dict__.setdefault("_li_started", time.monotonic())
        if time.monotonic() - started > self.s.linkedin_minutes * 60:
            raise RuntimeError("LinkedIn time budget used up; the rest waits for the next run")
        for attempt in range(3):
            self.sleep(random.uniform(*self.s.linkedin_delay))
            status, body = self.fetch(url)
            if status == 200:
                self._li_strikes = 0
                return body
            if status == 429:
                self._li_strikes = getattr(self, "_li_strikes", 0) + 1
                if self._li_strikes >= 6:
                    raise RuntimeError("LinkedIn kept refusing (too many requests); the rest waits for next run")
                self.sleep(30 * (attempt + 1))
                continue
            return ""
        return ""

    def _linkedin(self) -> None:
        base = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        for kw in self.s.keywords:
            params = {"keywords": f"{kw} remote", "location": "United States", "f_WT": "2",
                      "f_TPR": f"r{self.s.days * 86400}", "start": 0}
            try:
                body = self._li_get(f"{base}?{urlencode(params)}")
            except RuntimeError as e:
                self.notes.append(str(e))
                return
            for job in parse_listing(body):
                self._consider(Candidate(f"linkedin:{job.job_id}", "linkedin", job.title, job.company,
                                         job.location, job.url), broad=False)

    def _fill_linkedin_descriptions(self, cands: list[Candidate]) -> None:
        self._li_started = time.monotonic()  # descriptions get their own time budget
        for c in cands:
            try:
                body = self._li_get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{c.key.split(':', 1)[1]}")
            except RuntimeError as e:
                self.notes.append(str(e))
                return
            if body:
                c.description = parse_posting(body)["description"][:MAX_DESCRIPTION]

    # ---- Himalayas ----
    def _himalayas(self) -> None:
        cutoff = time.time() - self.s.days * 86400 - 3600
        queries = sorted({q for q in self.s.relevance_words[:8]} | {"healthcare", "epic", "clinical"})
        for q in queries:
            status, body = self.fetch("https://himalayas.app/jobs/api/search?" + urlencode({"q": q, "country": "US"}))
            if status != 200:
                continue
            for j in json.loads(body).get("jobs", []):
                try:
                    if float(j.get("pubDate") or 0) < cutoff:
                        continue
                except ValueError:
                    pass
                where = ", ".join(j.get("locationRestrictions") or []) or "Remote"
                if "United States" not in where and where != "Remote":
                    continue
                period = j.get("salaryPeriod") or "year"
                self._consider(Candidate(
                    "himalayas:" + (j.get("guid") or "").rstrip("/").split("/companies/")[-1],
                    "himalayas", j.get("title", ""), j.get("companyName", ""), f"Remote ({where})",
                    j.get("applicationLink") or j.get("guid", ""),
                    posted=_iso(j.get("pubDate")), salary=money(j.get("minSalary"), j.get("maxSalary"), period),
                    description=text_of(j.get("description", ""))[:MAX_DESCRIPTION]), broad=True)
            time.sleep(1)

    # ---- company job boards ----
    def _boards(self) -> None:
        for board in self.s.company_boards:
            kind, _, slug = board.partition(":")
            fn = getattr(self, f"_board_{kind}", None)
            if not fn:
                self.notes.append(f"unknown board type '{kind}'")
                continue
            try:
                fn(slug)
            except (ValueError, KeyError) as e:
                self.notes.append(f"{board}: unreadable ({e})")

    def _recent(self, iso_or_ms) -> bool:
        posted = _parse_time(iso_or_ms)
        return posted is None or posted >= time.time() - max(self.s.days, 1) * 86400 - 3600

    def _board_greenhouse(self, slug: str) -> None:
        status, body = self.fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        if status != 200:
            return
        for j in json.loads(body).get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            if not _remote_us(loc) or not self._recent(j.get("first_published") or j.get("updated_at")):
                continue
            self._consider(Candidate(f"greenhouse:{slug}:{j['id']}", "company site", j["title"],
                                     j.get("company_name") or slug.title(), loc, j.get("absolute_url", ""),
                                     posted=(j.get("first_published") or "")[:10],
                                     description=text_of(html.unescape(j.get("content", "")))[:MAX_DESCRIPTION]),
                           broad=True)

    def _board_lever(self, slug: str) -> None:
        status, body = self.fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if status != 200:
            return
        for j in json.loads(body):
            cats = j.get("categories") or {}
            loc = cats.get("location", "") or ", ".join(cats.get("allLocations") or [])
            remote = j.get("workplaceType") == "remote" or _remote_us(loc)
            if not remote or (j.get("country") not in (None, "", "US")) or not self._recent(j.get("createdAt")):
                continue
            pay = j.get("salaryRange") or {}
            desc = text_of(j.get("description", "")) + "\n" + "\n".join(
                f"{x.get('text', '')}\n{text_of(x.get('content', ''))}" for x in j.get("lists") or [])
            self._consider(Candidate(f"lever:{slug}:{j['id']}", "company site", j.get("text", ""),
                                     slug.title(), loc or "Remote", j.get("hostedUrl", ""),
                                     posted=_iso(j.get("createdAt")),
                                     salary=money(pay.get("min"), pay.get("max"), pay.get("interval", "year")),
                                     description=(desc + "\n" + text_of(j.get("additional", "")))[:MAX_DESCRIPTION]),
                           broad=True)

    def _board_ashby(self, slug: str) -> None:
        status, body = self.fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
        if status != 200:
            return
        for j in json.loads(body).get("jobs", []):
            loc = j.get("location", "")
            addr = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")
            remote = j.get("isRemote") or j.get("workplaceType") == "Remote"
            if not remote or addr not in ("", "United States", "USA", "US") or not self._recent(j.get("publishedAt")):
                continue
            pay = (j.get("compensation") or {}).get("compensationTierSummary", "")
            self._consider(Candidate(f"ashby:{slug}:{j['id']}", "company site", j.get("title", ""),
                                     slug.title(), loc or "Remote", j.get("jobUrl", ""),
                                     posted=(j.get("publishedAt") or "")[:10], salary=pay,
                                     description=(j.get("descriptionPlain") or text_of(j.get("descriptionHtml", "")))[:MAX_DESCRIPTION]),
                           broad=True)


def _parse_time(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return f / 1000 if f > 1e11 else f
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(v) -> str:
    t = _parse_time(v)
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat() if t else ""


def _remote_us(loc: str) -> bool:
    loc = (loc or "").lower()
    if "remote" not in loc and "anywhere" not in loc:
        return False
    foreign = ["canada", "uk", "united kingdom", "india", "europe", "emea", "germany", "mexico", "brazil",
               "australia", "philippines", "poland", "spain", "ireland", "france", "israel", "apac", "latam"]
    us = any(w in loc for w in ("us", "u.s.", "united states", "usa", "america"))
    return us or not any(w in loc for w in foreign)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobbot.finder")
    ap.add_argument("--settings", required=True)
    ap.add_argument("--seen", help="file of job keys already judged, one per line")
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, help="override how many days back to look")
    ap.add_argument("--max", type=int, help="override max candidates")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    raw = json.load(open(args.settings, encoding="utf-8"))
    names = set(Settings.__dataclass_fields__)
    settings = Settings(**{k: v for k, v in raw.items() if k in names})
    if args.days:
        settings.days = args.days
    if args.max:
        settings.max_candidates = args.max
    seen = set()
    if args.seen:
        try:
            seen = {ln.strip() for ln in open(args.seen, encoding="utf-8") if ln.strip()}
        except FileNotFoundError:
            pass

    finder = Finder(settings, seen)
    cands = finder.run()
    out = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "found_by_source": finder.counts, "notes": finder.notes,
           "candidates": [asdict(c) for c in cands]}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)
    log.info("%d candidates to score (new by source: %s)%s", len(cands), finder.counts,
             "".join(f"\n  note: {n}" for n in finder.notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
