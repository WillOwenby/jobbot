from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright

from .config import BrowserConfig


@contextmanager
def open_browser(cfg: BrowserConfig):
    """A visible browser with a persistent profile, so your logins survive between runs."""
    Path(cfg.profile_dir).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context: BrowserContext = pw.chromium.launch_persistent_context(
            cfg.profile_dir,
            headless=False,
            channel=cfg.channel,
            slow_mo=cfg.slow_mo_ms,
            no_viewport=True,
            args=["--start-maximized"],
        )
        try:
            yield context
        finally:
            context.close()
