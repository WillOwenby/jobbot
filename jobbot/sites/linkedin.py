"""LinkedIn Easy Apply.

LinkedIn changes its markup often. When something stops working, the selectors
below are the first place to look: open DevTools on the page and update them.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Iterator
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..models import ApplyResult, Job, Status
from .base import MAX_FORM_STEPS, JobSite, text_of

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs/search/"
# The plain job-listing feed LinkedIn serves to logged-out visitors. Its card
# format has been stable for years, unlike the logged-in results page.
LISTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LISTING_PAGE_SIZE = 10
PAGE_SIZE = 25

# Job cards are found through their links, which LinkedIn can't change without
# breaking its own URLs, rather than through card markup, which changes often.
JOB_LINK = "a[href*='/jobs/view/'], a[href*='currentJobId='], [data-job-id], [data-occludable-job-id]"

COLLECT_JOBS_JS = r"""
() => {
  const jobs = new Map();
  for (const a of document.querySelectorAll("a[href*='/jobs/view/'], a[href*='currentJobId=']")) {
    const m = a.href.match(/\/jobs\/view\/(?:[^\/?]*?-)?(\d{6,})/) || a.href.match(/currentJobId=(\d{6,})/);
    if (!m || jobs.has(m[1])) continue;
    const card = a.closest('li, [data-job-id], [data-occludable-job-id], .job-card-container') || a.parentElement;
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const visible = a.querySelector('[aria-hidden="true"], strong') || a;
    const title = ((visible.innerText || '').split('\n')[0] || a.getAttribute('aria-label') || lines[0] || '').trim();
    jobs.set(m[1], {id: m[1], title, lines});
  }
  // Cards that carry the job id as an attribute instead of a direct link.
  for (const card of document.querySelectorAll('[data-job-id], [data-occludable-job-id]')) {
    const id = card.getAttribute('data-job-id') || card.getAttribute('data-occludable-job-id');
    if (!/^\d{6,}$/.test(id || '') || jobs.has(id)) continue;
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const heading = card.querySelector('h3, strong, [class*="title"]');
    const title = ((heading && heading.innerText) || lines[0] || '').split('\n')[0].trim();
    jobs.set(id, {id, title, lines});
  }
  return [...jobs.values()];
}
"""

SCROLL_RESULTS_JS = r"""
() => {
  // Scroll the results list (or the window) one screen down so more cards render.
  let el = document.querySelector("a[href*='/jobs/view/'], a[href*='currentJobId=']");
  while (el && el !== document.body) {
    const style = getComputedStyle(el);
    if (el.scrollHeight > el.clientHeight + 20 && /(auto|scroll)/.test(style.overflowY)) {
      const before = el.scrollTop;
      el.scrollTop += el.clientHeight;
      return el.scrollTop > before;
    }
    el = el.parentElement;
  }
  const before = window.scrollY;
  window.scrollBy(0, window.innerHeight);
  return window.scrollY > before;
}
"""

CARD_NOISE = re.compile(
    r"^(promoted|easy apply|viewed|applied|actively (reviewing|recruiting).*|be an early applicant|"
    r"\d+ (applicants?|connections?).*|.*\bago|with verification|new|dismiss.*|save|"
    r"\$[\d,.]+k?.*|.*school alum.*|.*works? here)$",
    re.I,
)

DESCRIPTION = ("[componentkey^='JobDetails_AboutTheJob_'], #job-details, .jobs-description__content, .jobs-description-content__text, "
               ".jobs-box__html-content, [class*='jobs-description'], article")
EXPAND_DESCRIPTION = "[data-testid='expandable-text-button'], button[aria-label*='see more' i]"
TOP_CARD_COMPANY = ".job-details-jobs-unified-top-card__company-name, a[href*='/company/']"
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

    def search(self, query: str, location: str) -> Iterator[Job]:
        s = self.cfg.search
        params = {
            "keywords": query,
            "location": location,
            "f_AL": "true",  # Easy Apply only
            "f_TPR": f"r{s.posted_within_days * 86400}",
            "sortBy": "DD",
        }
        if s.remote:
            params["f_WT"] = "2"
        try:
            yield from self._search_listing(params)
        except ListingUnavailable as e:
            log.info("    listing feed unavailable (%s); reading the results page instead", e)
            yield from self._search_page(params)

    def _search_listing(self, params: dict) -> Iterator[Job]:
        seen: set[str] = set()
        for start in range(0, self.cfg.search.max_pages * PAGE_SIZE, LISTING_PAGE_SIZE):
            try:
                resp = self.context.request.get(f"{LISTING_URL}?{urlencode({**params, 'start': start})}",
                                                timeout=30000)
            except PlaywrightError as e:
                if seen:
                    return
                raise ListingUnavailable(str(e).split("\n")[0]) from None
            if not resp.ok:
                if seen or start:
                    return  # past the last page
                raise ListingUnavailable(f"HTTP {resp.status}")
            jobs = [j for j in parse_listing(resp.text()) if j.job_id not in seen]
            if start == 0 or jobs:
                log.info("    results %d-%d: %d new jobs", start + 1, start + LISTING_PAGE_SIZE, len(jobs))
            if not jobs:
                return
            seen.update(j.job_id for j in jobs)
            yield from jobs
            self.page.wait_for_timeout(1000)

    def _search_page(self, params: dict) -> Iterator[Job]:
        seen: set[str] = set()
        for page_num in range(self.cfg.search.max_pages):
            params["start"] = page_num * PAGE_SIZE
            self.goto(f"{SEARCH_URL}?{urlencode(params)}")
            try:
                self.page.locator(JOB_LINK).first.wait_for(timeout=15000)
            except PlaywrightTimeout:
                log.info("    no jobs found on results page %d", page_num + 1)
                self.save_debug("search-no-jobs")
                return
            # Collect the whole page before yielding: applying navigates away.
            jobs = [j for j in self._collect_jobs() if j.job_id not in seen]
            log.info("    results page %d: %d new jobs", page_num + 1, len(jobs))
            if self.debug:
                self.save_debug("search-results")
            if not jobs:
                return
            seen.update(j.job_id for j in jobs)
            yield from jobs

    def _collect_jobs(self) -> list[Job]:
        for _ in range(12):  # cards render lazily as the list scrolls
            if not self.page.evaluate(SCROLL_RESULTS_JS):
                break
            self.page.wait_for_timeout(400)
        jobs = []
        for raw in self.page.evaluate(COLLECT_JOBS_JS):
            title = clean_title(raw["title"])
            company, location = parse_card_lines(title, raw["lines"])
            if title:
                jobs.append(Job(
                    site=self.name, job_id=raw["id"], title=title, company=company,
                    location=location, url=f"https://www.linkedin.com/jobs/view/{raw['id']}/",
                ))
        return jobs

    def open(self, job: Job) -> ApplyResult | None:
        self.goto(job.url)
        try:
            box = self.page.locator(DESCRIPTION).first
            box.wait_for(timeout=15000)
            expand = box.locator(EXPAND_DESCRIPTION).first
            if expand.count():
                try:
                    expand.click(timeout=3000)  # the description is truncated behind "more"
                    self.page.wait_for_timeout(500)
                except PlaywrightError:
                    pass
            job.description = box.inner_text()
        except PlaywrightTimeout:
            log.info("    couldn't find the job description; using the whole page text")
            self.save_debug("job-no-description")
            main = self.page.locator("main")
            job.description = (main.first if main.count() else self.page.locator("body")).inner_text()
        if self.debug:
            self.save_debug("job-page")
        # The page title is "Job title | Company | LinkedIn" on every layout so far.
        parts = [x.strip() for x in self.page.title().split("|")]
        if len(parts) >= 3 and parts[-1] == "LinkedIn":
            job.title = job.title or parts[0]
            job.company = job.company or parts[1]
        if not job.company:
            job.company = text_of(self.page, TOP_CARD_COMPANY)
        if self.page.locator(ALREADY_APPLIED).count():
            return ApplyResult(Status.ALREADY_APPLIED)
        if not self.page.locator(EASY_APPLY_BUTTON).count():
            self.save_debug("job-no-easy-apply")
            return ApplyResult(Status.SKIPPED, "no Easy Apply button")
        return None

    def apply(self, job: Job) -> ApplyResult:
        try:
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


def clean_title(title: str) -> str:
    """Drop LinkedIn's screen-reader extras, e.g. "Title Title with verification" -> "Title"."""
    title = re.sub(r"\s*with verification\s*$", "", title.strip(), flags=re.I)
    m = re.fullmatch(r"(.+?)\s*\1", title)
    return m.group(1) if m else title


def parse_card_lines(title: str, lines: list[str]) -> tuple[str, str]:
    """Pick company and location out of a result card's text lines."""
    rest = [ln for ln in lines if ln != title and not CARD_NOISE.match(ln) and title not in ln]
    company = rest[0] if rest else ""
    location = rest[1] if len(rest) > 1 else ""
    return company, location


class ListingUnavailable(Exception):
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
