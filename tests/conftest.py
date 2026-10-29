import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobbot.config import AIConfig, AnswerRule, Config, ProfileConfig, SearchConfig  # noqa: E402

CHROMIUM = os.environ.get("JOBBOT_TEST_CHROMIUM", "/opt/pw-browsers/chromium")


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        kwargs = {"executable_path": CHROMIUM} if Path(CHROMIUM).exists() else {}
        b = pw.chromium.launch(**kwargs)
        yield b
        b.close()


@pytest.fixture
def context(browser):
    ctx = browser.new_context()
    yield ctx
    ctx.close()


@pytest.fixture
def page(context):
    return context.new_page()


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "resume.txt"
    p.write_text("Jane Doe. 4 years Python, Django, Postgres. BS Computer Science.")
    return p


@pytest.fixture
def cfg(resume):
    return Config(
        search=SearchConfig(keywords=["python"]),
        profile=ProfileConfig(first_name="Jane", last_name="Doe", email="jane@example.com",
                              phone="5125550100", resume_path=str(resume), city="Austin"),
        answers=[AnswerRule(r"years.*python", "4"), AnswerRule(r"sponsorship", "No")],
        ai=AIConfig(enabled=False),
    )
