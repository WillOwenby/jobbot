"""Indeed ("Easily apply" / Indeed Apply) jobs.

Indeed uses aggressive bot protection. If you get a "verify you are human"
page, solve it in the browser window; the persistent profile helps it stick.
As with LinkedIn, update the selectors below when Indeed changes its markup.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..models import ApplyResult, Job, Status
from .base import MAX_FORM_STEPS, JobSite, text_of

log = logging.getLogger(__name__)

PAGE_SIZE = 10
REMOTE_FILTER = "032b3046-06a3-4876-8dfd-474eb5e7ed11"  # Indeed's "Remote" attribute id

CARD = "div.job_seen_beacon, li div.cardOutline"
CARD_LINK = "a[data-jk]"
CARD_TITLE = "h2.jobTitle, a[data-jk]"
CARD_COMPANY = "[data-testid='company-name']"
CARD_LOCATION = "[data-testid='text-location']"
EASILY_APPLY = "text=/easily apply/i"

DESCRIPTION = "#jobDescriptionText"
APPLY_BUTTON = "#indeedApplyButton, button[id*='indeedApplyButton'], button:has-text('Apply now')"
SUBMIT = "button:has-text('Submit your application'), button:has-text('Submit application')"
CONTINUE = "button:has-text('Continue'), button:has-text('Review your application')"
SUCCESS = "text=/application has been submitted|you.ve applied|application submitted/i"


def _posted_within(days: int) -> int:
    # Indeed only accepts these values for "fromage".
    return min((1, 3, 7, 14), key=lambda d: (d < days, abs(d - days)))


class Indeed(JobSite):
    name = "indeed"

    @property
    def base(self) -> str:
        return f"https://{self.cfg.indeed_domain}"

    @property
    def login_url(self) -> str:
        return "https://secure.indeed.com/account/login"

    def is_logged_in(self) -> bool:
        self.page.goto("https://profile.indeed.com/")
        self.page.wait_for_load_state("domcontentloaded")
        return not re.search(r"secure\.indeed\.com/(auth|account/login)", self.page.url)

    def search(self, query: str, location: str) -> Iterator[Job]:
        s = self.cfg.search
        params = {"q": query, "l": location, "fromage": _posted_within(s.posted_within_days), "sort": "date"}
        if s.remote:
            params["remotejob"] = REMOTE_FILTER
        seen: set[str] = set()
        for page_num in range(s.max_pages):
            params["start"] = page_num * PAGE_SIZE
            self.page.goto(f"{self.base}/jobs?{urlencode(params)}")
            try:
                self.page.locator(CARD).first.wait_for(timeout=20000)
            except PlaywrightTimeout:
                return
            jobs = [j for j in self._collect_cards() if j.job_id not in seen]
            if not jobs:
                return
            seen.update(j.job_id for j in jobs)
            yield from jobs

    def _collect_cards(self) -> list[Job]:
        jobs = []
        for card in self.page.locator(CARD).all():
            link = card.locator(CARD_LINK).first
            if not link.count() or not card.locator(EASILY_APPLY).count():
                continue  # only jobs you can apply to on Indeed itself
            jk = link.get_attribute("data-jk")
            title = text_of(card, CARD_TITLE)
            if jk and title:
                jobs.append(Job(
                    site=self.name, job_id=jk, title=title,
                    company=text_of(card, CARD_COMPANY), location=text_of(card, CARD_LOCATION),
                    url=f"{self.base}/viewjob?jk={jk}",
                ))
        return jobs

    def open(self, job: Job) -> ApplyResult | None:
        self.page.goto(job.url)
        try:
            self.page.locator(DESCRIPTION).first.wait_for(timeout=15000)
            job.description = self.page.locator(DESCRIPTION).first.inner_text()
        except PlaywrightTimeout:
            log.debug("no description found for %s", job.url)
        button = self.page.locator(APPLY_BUTTON).first
        if not button.count():
            return ApplyResult(Status.SKIPPED, "no Indeed Apply button")
        if re.search(r"\bapplied\b", button.inner_text(), re.I):
            return ApplyResult(Status.ALREADY_APPLIED)
        return None

    def apply(self, job: Job) -> ApplyResult:
        form_page = self.page
        try:
            try:
                with self.context.expect_page(timeout=8000) as new_tab:
                    self.page.locator(APPLY_BUTTON).first.click()
                form_page = new_tab.value
            except PlaywrightTimeout:
                pass  # the form opened in the same tab
            form_page.wait_for_load_state("domcontentloaded")
            return self._apply(job, form_page)
        except PlaywrightError as e:
            log.exception("Indeed apply failed")
            return ApplyResult(Status.FAILED, str(e).split("\n")[0])
        finally:
            if form_page is not self.page and not form_page.is_closed():
                form_page.close()

    def _apply(self, job: Job, page) -> ApplyResult:
        for _ in range(MAX_FORM_STEPS):
            page.wait_for_timeout(1500)
            if page.locator(SUCCESS).count():
                return ApplyResult(Status.APPLIED)
            root = page.locator("main").first if page.locator("main").count() else page.locator("body")
            unanswered = self.filler.fill(root)
            if unanswered and not self.prompter.resolve_manually(job, unanswered):
                return ApplyResult(Status.NEEDS_INPUT, "unanswered questions", unanswered)

            submit = page.locator(SUBMIT)
            if submit.count():
                if not self.prompter.confirm_submit(job):
                    return ApplyResult(Status.SKIPPED, "declined at review")
                submit.first.click()
                try:
                    page.locator(SUCCESS).first.wait_for(timeout=20000)
                    return ApplyResult(Status.APPLIED)
                except PlaywrightTimeout:
                    return ApplyResult(Status.FAILED, "submitted but no confirmation seen; check Indeed")

            cont = page.locator(CONTINUE)
            if not cont.count():
                return ApplyResult(Status.FAILED, "no Continue/Submit button found")
            url_before = page.url
            cont.first.click()
            page.wait_for_timeout(2000)
            if page.url == url_before and not page.locator(SUBMIT).count():
                questions = unanswered or ["(form did not advance; check the browser for errors)"]
                if not self.prompter.resolve_manually(job, questions):
                    return ApplyResult(Status.NEEDS_INPUT, "form did not advance", questions)

        return ApplyResult(Status.FAILED, f"gave up after {MAX_FORM_STEPS} steps")
