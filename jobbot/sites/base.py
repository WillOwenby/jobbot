from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from playwright.sync_api import BrowserContext, Locator

from ..config import Config
from ..forms import FormFiller
from ..models import ApplyResult, Job
from ..prompter import Prompter

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

    def ensure_logged_in(self) -> None:
        if self.is_logged_in():
            return
        self.page.goto(self.login_url)
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
