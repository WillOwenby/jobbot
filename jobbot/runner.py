from __future__ import annotations

import logging
import random
import time
from collections import Counter

from playwright.sync_api import Error as PlaywrightError

from .browser import open_browser
from .config import Config
from .filters import reject_reason
from .forms import AnswerBook, Answerer, FormFiller
from .models import ApplyResult, Job, Status
from .prompter import Prompter
from .sites import SITES
from .sites.base import JobSite
from .tracker import Tracker

MIN_DESCRIPTION = 200  # characters; anything shorter isn't a real job posting

log = logging.getLogger(__name__)


def build_ai(cfg: Config):
    if not cfg.ai.enabled:
        return None
    from .ai import ResumeAI, read_document_text
    example = read_document_text(cfg.ai.cover_letter_example) if cfg.ai.cover_letter_example else ""
    return ResumeAI(cfg.ai, cfg.profile, read_document_text(cfg.profile.resume_path),
                    example_letter=example)


class Runner:
    def __init__(self, cfg: Config, mode: str, max_applications: int | None = None, recheck: bool = False,
                 debug: bool = False):
        self.cfg = cfg
        self.recheck = recheck
        self.debug = debug
        self.mode = mode
        self.limit = max_applications or cfg.limits.max_applications_per_site
        self.tracker = Tracker(cfg.database)
        self.ai = build_ai(cfg)
        question_ai = self.ai if cfg.ai.answer_questions else None
        self.answerer = Answerer(AnswerBook(cfg.profile, cfg.answers), question_ai, self.tracker)
        self.filler = FormFiller(self.answerer, cfg.profile)
        self.prompter = Prompter(mode)
        self.stats: Counter[str] = Counter()

    def run(self, sites: list[str]) -> Counter:
        with open_browser(self.cfg.browser) as context:
            for name in sites:
                site = SITES[name](context, self.cfg, self.filler, self.prompter)
                site.debug = self.debug
                site.ensure_logged_in()
                self._run_site(site)
        return self.stats

    def _run_site(self, site: JobSite) -> None:
        applied = 0
        for query in self.cfg.search.keywords:
            for location in self.cfg.search.locations:
                log.info("[%s] searching '%s' in '%s'", site.name, query, location or "anywhere")
                for job in site.search(query, location):
                    if applied >= self.limit:
                        log.info("[%s] reached limit of %d applications", site.name, self.limit)
                        return
                    if self.tracker.is_done(job, self.recheck):
                        continue
                    try:
                        result = self._process(site, job)
                    except PlaywrightError as e:
                        # One broken page shouldn't end the run.
                        result = ApplyResult(Status.FAILED, f"browser error: {str(e).splitlines()[0]}")
                    self._log(job, result)
                    if result.status in (Status.APPLIED, Status.DRY_RUN):
                        applied += 1
                    if result.status not in (Status.SKIPPED, Status.ALREADY_APPLIED):
                        time.sleep(random.uniform(self.cfg.limits.min_delay_seconds,
                                                  self.cfg.limits.max_delay_seconds))

    def _process(self, site: JobSite, job: Job) -> ApplyResult:
        if reason := reject_reason(job, self.cfg.filters):
            return ApplyResult(Status.SKIPPED, reason)
        if early := site.open(job):
            return early
        if reason := reject_reason(job, self.cfg.filters):  # now with the description
            return ApplyResult(Status.SKIPPED, reason)

        if self.ai:
            from .ai import AIError, AIFatalError
            if len(job.description.strip()) < MIN_DESCRIPTION:
                # Never pass a job through unscored. FAILED (not FLAGGED) so the next run retries it.
                return ApplyResult(Status.FAILED, "couldn't read the job description, so it wasn't scored")
            try:
                fit = self.ai.score_fit(job)
                job.fit_score, job.fit_reason, job.concerns = fit.score, fit.reason, fit.concerns
                log.info("    fit %d/100: %s", fit.score, fit.reason)
                for concern in fit.concerns:
                    log.info("    concern: %s", concern)
                if fit.hard_exclusion:
                    return ApplyResult(Status.SKIPPED, f"excluded: {fit.hard_exclusion}")
                if fit.score < self.cfg.ai.min_fit_score:
                    return ApplyResult(Status.SKIPPED, f"fit score {fit.score} < {self.cfg.ai.min_fit_score}")
            except AIFatalError:
                raise
            except AIError as e:
                log.warning("    could not score fit: %s", e)
                return ApplyResult(Status.FAILED, f"not scored: {e}")

        if self.mode == "dry-run":
            return ApplyResult(Status.DRY_RUN, "would apply" + (" (has concerns)" if job.concerns else ""))
        if self.mode == "auto" and job.concerns:
            # Never auto-submit a job with open questions; leave it for a person.
            return ApplyResult(Status.FLAGGED, "; ".join(job.concerns))

        self.answerer.start_job(job, *self._cover_letter(job))
        return site.apply(job)

    def _cover_letter(self, job: Job) -> tuple[str | None, str | None]:
        if not (self.ai and self.cfg.ai.cover_letters and job.description):
            return None, None
        from .ai import AIError, write_cover_letter_pdf
        try:
            text = self.ai.cover_letter(job)
        except AIError as e:
            log.warning("    could not write cover letter: %s", e)
            return None, None
        path = write_cover_letter_pdf(text, job, self.cfg.output_dir, self.cfg.profile)
        log.info("    cover letter: %s", path)
        return text, path

    def _log(self, job: Job, result: ApplyResult) -> None:
        self.tracker.record(job, result)
        self.stats[result.status.value] += 1
        detail = f" ({result.detail})" if result.detail else ""
        log.info("  %-15s %s @ %s%s", result.status.value.upper(), job.title, job.company, detail)
