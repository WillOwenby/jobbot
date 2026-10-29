"""LinkedIn Easy Apply.

LinkedIn changes its markup often. When something stops working, the selectors
below are the first place to look: open DevTools on the page and update them.
"""
from __future__ import annotations

import logging
from typing import Iterator
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..models import ApplyResult, Job, Status
from .base import MAX_FORM_STEPS, JobSite, text_of

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs/search/"
PAGE_SIZE = 25

CARD = "li[data-occludable-job-id]"
CARD_TITLE = "a.job-card-list__title--link, a.job-card-list__title, a.job-card-container__link"
CARD_COMPANY = ".artdeco-entity-lockup__subtitle, .job-card-container__primary-description"
CARD_LOCATION = ".artdeco-entity-lockup__caption, .job-card-container__metadata-item"

DESCRIPTION = "#job-details, .jobs-description__content, .jobs-description-content__text"
EASY_APPLY_BUTTON = "button.jobs-apply-button:has-text('Easy Apply'), button[aria-label*='Easy Apply']"
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
        seen: set[str] = set()
        for page_num in range(s.max_pages):
            params["start"] = page_num * PAGE_SIZE
            self.goto(f"{SEARCH_URL}?{urlencode(params)}")
            try:
                self.page.locator(CARD).first.wait_for(timeout=15000)
            except PlaywrightTimeout:
                return
            # Collect the whole page before yielding: applying navigates away.
            jobs = [j for j in self._collect_cards() if j.job_id not in seen]
            if not jobs:
                return
            seen.update(j.job_id for j in jobs)
            yield from jobs

    def _collect_cards(self) -> list[Job]:
        jobs = []
        cards = self.page.locator(CARD)
        for i in range(cards.count()):
            card = cards.nth(i)
            try:
                card.scroll_into_view_if_needed(timeout=3000)  # cards render lazily
            except PlaywrightTimeout:
                continue
            self.page.wait_for_timeout(150)
            job_id = card.get_attribute("data-occludable-job-id")
            title = text_of(card, CARD_TITLE)
            if job_id and title:
                jobs.append(Job(
                    site=self.name, job_id=job_id, title=title,
                    company=text_of(card, CARD_COMPANY), location=text_of(card, CARD_LOCATION),
                    url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                ))
        return jobs

    def open(self, job: Job) -> ApplyResult | None:
        self.goto(job.url)
        try:
            self.page.locator(DESCRIPTION).first.wait_for(timeout=15000)
            job.description = self.page.locator(DESCRIPTION).first.inner_text()
        except PlaywrightTimeout:
            log.debug("no description found for %s", job.url)
        if self.page.locator(ALREADY_APPLIED).count():
            return ApplyResult(Status.ALREADY_APPLIED)
        if not self.page.locator(EASY_APPLY_BUTTON).count():
            return ApplyResult(Status.SKIPPED, "no Easy Apply button")
        return None

    def apply(self, job: Job) -> ApplyResult:
        try:
            return self._apply(job)
        except PlaywrightError as e:
            log.exception("LinkedIn apply failed")
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
