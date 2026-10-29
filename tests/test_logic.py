import textwrap

import pytest

from jobbot.config import FilterConfig, load_config
from jobbot.filters import reject_reason
from jobbot.forms import AnswerBook, choose_option, normalize
from jobbot.models import Job
from jobbot.sites.indeed import _posted_within


def job(title="Python Developer", company="Acme", description=""):
    return Job("linkedin", "1", title, company, "Remote", "https://x", description)


def test_normalize_strips_markers_and_repeats():
    assert normalize("  Phone number *  ") == "Phone number"
    assert normalize("Do you need sponsorship? Do you need sponsorship?") == "Do you need sponsorship?"
    assert normalize("Years of Python (Required)") == "Years of Python"


def test_choose_option():
    opts = ["Select an option", "Yes", "No"]
    assert choose_option("yes", opts) == 1
    assert choose_option("No", opts) == 2
    assert choose_option("Bachelor's", ["Select", "High school", "Bachelor's Degree", "Master's Degree"]) == 2
    assert choose_option("Select an option", opts) is None
    assert choose_option("Maybe", opts) is None
    travel = ["Select an option", "None", "Up to 10%", "Up to 25%"]
    assert choose_option("Yes, up to 10%", travel) == 2
    assert choose_option("Yes, up to 10%", ["Yes", "No"]) == 0


def test_answer_book_rules_beat_profile(cfg):
    book = AnswerBook(cfg.profile, cfg.answers)
    assert book.lookup("How many years of experience do you have with Python?*") == "4"
    assert book.lookup("First name") == "Jane"
    assert book.lookup("Mobile phone number") == "5125550100"
    assert book.lookup("Favorite color") is None


def test_filters_whole_words():
    f = FilterConfig(title_exclude=["intern", "senior"], company_exclude=["Evil Corp"],
                     description_exclude=["C++"])
    assert reject_reason(job("Senior Python Developer"), f) == "title contains 'senior'"
    assert reject_reason(job("International Python Developer"), f) is None  # not "intern"
    assert reject_reason(job(company="Evil Corp LLC"), f)
    assert reject_reason(job(description="Must know C++ well"), f)
    assert reject_reason(job(), FilterConfig(title_include=["golang"])) is not None


def test_indeed_posted_within():
    assert _posted_within(1) == 1
    assert _posted_within(5) == 7
    assert _posted_within(30) == 14


def test_load_config(tmp_path):
    (tmp_path / "cv.pdf").write_bytes(b"%PDF")
    (tmp_path / "config.yaml").write_text(textwrap.dedent("""
        sites: [linkedin]
        search: {keywords: [python]}
        profile: {first_name: A, last_name: B, email: a@b.c, phone: "1", resume_path: cv.pdf}
        answers:
          - {match: sponsorship, answer: no}
    """))
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.profile.resume_path == str(tmp_path / "cv.pdf")
    assert cfg.answers[0].answer == "No"  # YAML bool turned back into text
    assert cfg.mode == "review"


def test_load_config_rejects_typos(tmp_path):
    (tmp_path / "cv.pdf").write_bytes(b"%PDF")
    (tmp_path / "config.yaml").write_text(
        "search: {keywords: [x], remtoe: true}\n"
        "profile: {first_name: A, last_name: B, email: a, phone: '1', resume_path: cv.pdf}\n")
    with pytest.raises(ValueError, match="remtoe"):
        load_config(tmp_path / "config.yaml")


def test_example_config_is_valid(tmp_path):
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    (tmp_path / "config.yaml").write_text(example.read_text())
    (tmp_path / "resume.pdf").write_bytes(b"%PDF")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.sites == ["linkedin", "indeed"]
    assert cfg.ai.enabled
