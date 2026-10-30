"""LinkedIn.

Account safety: searching and reading job descriptions use LinkedIn's public,
logged-out pages, fetched without your cookies, so a dry run never touches your
account. The logged-in browser is only used to apply, a limited number of times
per run, with long pauses in between.

LinkedIn changes its markup often. When something stops working, the selectors
below are the first place to look.
"""
from __future__ import annotations

import html
import logging
import random
import re
import time
import urllib.error
import urllib.request
from typing import Iterator
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..models import ApplyResult, Job, Status
from .base import MAX_FORM_STEPS, JobSite, SiteLoggedOut, SiteStop

log = logging.getLogger(__name__)

# Public (logged-out) endpoints. Their HTML has been stable for years.
LISTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
LISTING_PAGE_SIZE = 10
PAGE_SIZE = 25
FEED_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
PUBLIC_DELAY = (3.0, 7.0)      # seconds between public requests
RATE_LIMIT_WAIT = 120          # seconds to back off after a "too many requests" answer

# Limits on the logged-in browser, whatever the config says.
MAX_LOGGED_IN_PAGES = 12       # job pages per run
MIN_LOGGED_IN_GAP = 45         # seconds between logged-in job pages (plus up to 45 random)

EASY_APPLY_BUTTON = ("button.jobs-apply-button:has-text('Easy Apply'), button[aria-label*='Easy Apply'], "
                     "a[aria-label*='Easy Apply'], button:has-text('Easy Apply'), a:has-text('Easy Apply')")
ALREADY_APPLIED = "text=/Applied\\s+\\d+\\s+\\w+\\s+ago/i"

MODAL = ".jobs-easy-apply-modal, div[role=dialog]:has(button[aria-label*='Dismiss'])"
SUBMIT = "button[aria-label='Submit application']"
NEXT = "button[aria-label='Continue to next step'], button[aria-label='Review your application']"
FIELD_ERROR = ".artdeco-inline-feedback--error"
FOLLOW_CHECKBOX = "#follow-company-checkbox"
DISMISS = "button[aria-label='Dismiss']"
DISCARD_CONFIRM = "button[data-control-name='discard_application_confirm_btn'], button:has-text('Discard')"

ERROR_QUESTION_JS = """
e => {
  const box = e.closest('.jobs-easy-apply-form-section__grouping, [data-test-form-element], fieldset')
              || e.parentElement;
  return (box.innerText || '').split('\\n')[0];
}
"""


class LinkedIn(JobSite):
    name = "linkedin"
    login_url = "https://www.linkedin.com/login"

    def is_logged_in(self) -> bool:
        self.goto("https://www.linkedin.com/feed/")
        self.page.wait_for_load_state("domcontentloaded")
        return "/feed" in self.page.url

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_public = 0.0
        self._last_logged_in = 0.0
        self._logged_in_pages = 0

    def search(self, query: str, location: str) -> Iterator[Job]:
        s = self.cfg.search
        if s.remote and "remote" not in query.lower():
            # The listing feed ignores the remote filter (f_WT); the word itself helps a lot.
            query = f"{query} remote"
        params = {
            "keywords": query,
            "location": location,
            "f_TPR": f"r{s.posted_within_days * 86400}",
            "sortBy": "DD",
        }
        if s.remote:
            params["f_WT"] = "2"
        # The public job page doesn't show Easy Apply, so search with the Easy Apply filter
        # first and, if company-site jobs are wanted too, again without it.
        seen: set[str] = set()
        for easy_apply in ([True, False] if s.include_external_jobs else [True]):
            pass_params = {**params, "f_AL": "true"} if easy_apply else params
            try:
                for job in self._search_listing(pass_params):
                    if job.job_id not in seen:
                        seen.add(job.job_id)
                        job.easy_apply = easy_apply
                        yield job
            except ListingUnavailable as e:
                log.info("    listing feed unavailable (%s); skipping this search", e)

    def _search_listing(self, params: dict) -> Iterator[Job]:
        seen: set[str] = set()
        for start in range(0, self.cfg.search.max_pages * PAGE_SIZE, LISTING_PAGE_SIZE):
            try:
                status, body = self._public_get(f"{LISTING_URL}?{urlencode({**params, 'start': start})}")
            except OSError as e:
                if seen:
                    return
                raise ListingUnavailable(str(e)) from None
            if status != 200:
                if seen or start:
                    return  # past the last page
                raise ListingUnavailable(f"HTTP {status}")
            jobs = [j for j in parse_listing(body) if j.job_id not in seen]
            if start == 0 or jobs:
                log.info("    results %d-%d: %d new jobs", start + 1, start + LISTING_PAGE_SIZE, len(jobs))
            if not jobs:
                return
            seen.update(j.job_id for j in jobs)
            yield from jobs

    def _public_get(self, url: str) -> tuple[int, str]:
        """GET a public LinkedIn page at a polite pace, backing off once if rate-limited."""
        for attempt in range(2):
            wait = random.uniform(*PUBLIC_DELAY) - (time.monotonic() - self._last_public)
            if wait > 0:
                time.sleep(wait)
            status, body = self._fetch_public(url)
            self._last_public = time.monotonic()
            if status != 429:
                return status, body
            if attempt == 0:
                log.warning("    LinkedIn says too many requests; waiting %ds", RATE_LIMIT_WAIT)
                time.sleep(RATE_LIMIT_WAIT)
        raise LinkedInRateLimited("LinkedIn is limiting requests from this connection. "
                                  "Stopped; try again in a few hours")

    @staticmethod
    def _fetch_public(url: str) -> tuple[int, str]:
        """Fetch a public page WITHOUT the browser's cookies, so searching and reading jobs
        never involve the logged-in account."""
        req = urllib.request.Request(url, headers={"User-Agent": FEED_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""

    def open(self, job: Job) -> ApplyResult | None:
        """Read the job from its public page; no logged-in browser involved."""
        status, body = self._public_get(POSTING_URL.format(job_id=job.job_id))
        if status == 404:
            return ApplyResult(Status.SKIPPED, "job is no longer listed")
        if status != 200:
            return ApplyResult(Status.FAILED, f"couldn't load the public job page (HTTP {status})")
        posting = parse_posting(body)
        job.description = posting["description"]
        job.title = job.title or posting["title"]
        job.company = job.company or posting["company"]
        job.location = job.location or posting["location"]
        log.debug("    description: %d chars", len(job.description))
        if job.easy_apply is False:
            if not self.cfg.search.include_external_jobs:
                return ApplyResult(Status.SKIPPED, "no Easy Apply button")
            job.external = True  # still worth scoring; you'd apply on the company site
        return None

    def _open_logged_in(self, job: Job) -> ApplyResult | None:
        """Open the job in the logged-in browser, only to apply, within the per-run limits."""
        if self._logged_in_pages >= MAX_LOGGED_IN_PAGES:
            return ApplyResult(Status.FAILED, "logged-in page limit for this run reached; try next run")
        gap = MIN_LOGGED_IN_GAP + random.uniform(0, MIN_LOGGED_IN_GAP)
        wait = gap - (time.monotonic() - self._last_logged_in)
        if self._last_logged_in and wait > 0:
            log.info("    pausing %ds before opening LinkedIn", wait)
            time.sleep(wait)
        self._logged_in_pages += 1
        self._last_logged_in = time.monotonic()
        self.goto(job.url)
        if self._looks_logged_out():
            self.save_debug("logged-out")
            raise SiteLoggedOut("LinkedIn is showing the logged-out page. Run "
                                "'python -m jobbot login linkedin', log in again, then rerun")
        try:
            ready = self.page.locator(EASY_APPLY_BUTTON).or_(self.page.locator(ALREADY_APPLIED))
            ready.first.wait_for(timeout=15000)
        except PlaywrightTimeout:
            pass
        if self.page.locator(ALREADY_APPLIED).count():
            return ApplyResult(Status.ALREADY_APPLIED)
        if not self.page.locator(EASY_APPLY_BUTTON).count():
            self.save_debug("job-no-easy-apply")
            return ApplyResult(Status.SKIPPED, "no Easy Apply button")
        return None

    def _looks_logged_out(self) -> bool:
        # The public (logged-out) job page has "Join now" and "Sign in" in its header.
        return bool(self.page.get_by_role("link", name="Join now", exact=True).count()
                    and self.page.get_by_role("link", name="Sign in", exact=True).count())

    def apply(self, job: Job) -> ApplyResult:
        try:
            if early := self._open_logged_in(job):
                return early
            return self._apply(job)
        except PlaywrightError as e:
            log.exception("LinkedIn apply failed")
            self.save_debug("apply-error")
            self._discard()
            return ApplyResult(Status.FAILED, str(e).split("\n")[0])

    def _apply(self, job: Job) -> ApplyResult:
        self.page.locator(EASY_APPLY_BUTTON).first.click()
        modal = self.page.locator(MODAL).first
        modal.wait_for(timeout=10000)

        for _ in range(MAX_FORM_STEPS):
            self.page.wait_for_timeout(800)
            unanswered = self.filler.fill(modal)
            if unanswered and not self.prompter.resolve_manually(job, unanswered):
                self._discard()
                return ApplyResult(Status.NEEDS_INPUT, "unanswered questions", unanswered)

            submit = modal.locator(SUBMIT)
            if submit.count():
                self._set_follow(modal)
                if not self.prompter.confirm_submit(job):
                    self._discard()
                    return ApplyResult(Status.SKIPPED, "declined at review")
                submit.first.click()
                self.page.wait_for_timeout(3000)
                if self.page.locator(DISMISS).count():
                    self.page.locator(DISMISS).first.click()
                return ApplyResult(Status.APPLIED)

            nxt = modal.locator(NEXT)
            if not nxt.count():
                self._discard()
                return ApplyResult(Status.FAILED, "no Next/Review/Submit button found")
            nxt.first.click()
            self.page.wait_for_timeout(1000)

            errors = modal.locator(FIELD_ERROR)
            if errors.count():
                questions = [q for q in (e.evaluate(ERROR_QUESTION_JS).strip() for e in errors.all()) if q]
                if not self.prompter.resolve_manually(job, questions or ["(form shows validation errors)"]):
                    self._discard()
                    return ApplyResult(Status.NEEDS_INPUT, "form validation errors", questions)

        self._discard()
        return ApplyResult(Status.FAILED, f"gave up after {MAX_FORM_STEPS} steps")

    def _set_follow(self, modal) -> None:
        box = modal.locator(FOLLOW_CHECKBOX)
        if box.count() and box.is_checked() != self.cfg.follow_companies:
            box.evaluate("e => e.click()")

    def _discard(self) -> None:
        try:
            if self.page.locator(DISMISS).count():
                self.page.locator(DISMISS).first.click(timeout=3000)
                self.page.wait_for_timeout(500)
            if self.page.locator(DISCARD_CONFIRM).count():
                self.page.locator(DISCARD_CONFIRM).first.click(timeout=3000)
        except PlaywrightError:
            pass


class ListingUnavailable(Exception):
    pass


class LinkedInRateLimited(SiteStop):
    pass


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
