from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Locator
from playwright.sync_api import Error as PlaywrightError

from ..config import Config
from ..forms import FormFiller
from ..models import ApplyResult, Job
from ..prompter import Prompter

log = logging.getLogger(__name__)

MAX_FORM_STEPS = 15


class JobSite(ABC):
    name: str
    login_url: str

    def __init__(self, context: BrowserContext, cfg: Config, filler: FormFiller, prompter: Prompter):
        self.context = context
        self.cfg = cfg
        self.filler = filler
        self.prompter = prompter
        self.page = context.pages[0] if context.pages else context.new_page()
        self._debug_saved: set[str] = set()
        self.debug = False  # when True, also snapshot pages that worked

    def save_debug(self, label: str) -> None:
        """Save a screenshot and the HTML of the current page (once per label per run)."""
        if label in self._debug_saved:
            return
        self._debug_saved.add(label)
        out = Path(self.cfg.output_dir) / "debug"
        out.mkdir(parents=True, exist_ok=True)
        stem = out / f"{self.name}-{label}-{datetime.now():%Y%m%d-%H%M%S}"
        try:
            self.page.screenshot(path=f"{stem}.png", full_page=True)
            Path(f"{stem}.html").write_text(self.page.content(), encoding="utf-8")
            log.warning("    saved debug snapshot: %s.png (and .html)", stem)
        except PlaywrightError as e:
            log.warning("    could not save debug snapshot: %s", e)

    def goto(self, url: str, attempts: int = 3) -> None:
        """page.goto that tolerates the site redirecting mid-load (e.g. LinkedIn right after login)."""
        for attempt in range(attempts):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                return
            except PlaywrightError as e:
                retryable = "interrupted by another navigation" in str(e) or "ERR_ABORTED" in str(e)
                if not retryable or attempt == attempts - 1:
                    raise
                self.page.wait_for_load_state("domcontentloaded")

    def ensure_logged_in(self) -> None:
        if self.is_logged_in():
            return
        self.goto(self.login_url)
        self.prompter.wait_for_login(self.name)
        if not self.is_logged_in():
            raise RuntimeError(f"still not logged in to {self.name}")

    @abstractmethod
    def is_logged_in(self) -> bool: ...

    @abstractmethod
    def search(self, query: str, location: str) -> Iterator[Job]:
        """Yield jobs from search results (title/company/location only, no description)."""

    @abstractmethod
    def open(self, job: Job) -> ApplyResult | None:
        """Open the job page and fill in job.description. Returns a result if we can't apply."""

    @abstractmethod
    def apply(self, job: Job) -> ApplyResult:
        """Apply to the job opened by the last open() call."""


def text_of(root: Locator, selector: str, timeout: float = 2000) -> str:
    loc = root.locator(selector).first
    try:
        if loc.count():
            return loc.inner_text(timeout=timeout).strip().split("\n")[0].strip()
    except Exception:
        pass
    return ""
