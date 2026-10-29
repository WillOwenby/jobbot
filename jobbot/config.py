"""Load and validate config.yaml into typed dataclasses."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MODES = ("review", "auto", "dry-run")
SUPPORTED_SITES = ("linkedin", "indeed")


@dataclass
class SearchConfig:
    keywords: list[str]
    locations: list[str] = field(default_factory=lambda: [""])
    remote: bool = False
    posted_within_days: int = 7
    max_pages: int = 5


@dataclass
class FilterConfig:
    title_include: list[str] = field(default_factory=list)
    title_exclude: list[str] = field(default_factory=list)
    company_exclude: list[str] = field(default_factory=list)
    description_exclude: list[str] = field(default_factory=list)


@dataclass
class ProfileConfig:
    first_name: str
    last_name: str
    email: str
    phone: str
    resume_path: str
    city: str = ""
    cover_letter_path: str = ""
    linkedin_url: str = ""
    website: str = ""


@dataclass
class AnswerRule:
    match: str
    answer: str


@dataclass
class AIConfig:
    enabled: bool = False
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    scoring_model: str = ""   # model for job-fit scoring; empty = same as `model`
    scoring_effort: str = ""  # effort for job-fit scoring; empty = same as `effort`
    min_fit_score: int = 60
    answer_questions: bool = True
    cover_letters: bool = True
    cover_letter_example: str = ""
    about_me: str = ""
    job_criteria: str = ""


@dataclass
class LimitsConfig:
    max_applications_per_site: int = 20
    min_delay_seconds: float = 4
    max_delay_seconds: float = 10


@dataclass
class BrowserConfig:
    profile_dir: str = "browser-profile"
    channel: str | None = None
    slow_mo_ms: int = 50


@dataclass
class Config:
    search: SearchConfig
    profile: ProfileConfig
    sites: list[str] = field(default_factory=lambda: ["linkedin"])
    mode: str = "review"
    filters: FilterConfig = field(default_factory=FilterConfig)
    answers: list[AnswerRule] = field(default_factory=list)
    ai: AIConfig = field(default_factory=AIConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    database: str = "jobbot.db"
    output_dir: str = "output"
    follow_companies: bool = False
    indeed_domain: str = "www.indeed.com"


def _build(cls, data, section):
    if not isinstance(data, dict):
        raise ValueError(f"config section '{section}' must be a mapping")
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"unknown key(s) in '{section}': {', '.join(sorted(unknown))}")
    try:
        return cls(**data)
    except TypeError as e:
        raise ValueError(f"config section '{section}': {e}") from None


def _answer_text(value) -> str:
    # YAML turns bare yes/no into booleans; forms want the words back.
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    data = yaml.safe_load(path.read_text()) or {}
    base = path.resolve().parent

    sections = {
        "search": SearchConfig, "filters": FilterConfig, "profile": ProfileConfig,
        "ai": AIConfig, "limits": LimitsConfig, "browser": BrowserConfig,
    }
    kwargs = dict(data)
    for key, cls in sections.items():
        if key in kwargs:
            kwargs[key] = _build(cls, kwargs[key], key)
    kwargs["answers"] = [
        AnswerRule(match=str(r["match"]), answer=_answer_text(r["answer"]))
        for r in (data.get("answers") or [])
    ]
    cfg = _build(Config, kwargs, "top level")

    if cfg.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got '{cfg.mode}'")
    for site in cfg.sites:
        if site not in SUPPORTED_SITES:
            raise ValueError(f"unsupported site '{site}' (supported: {SUPPORTED_SITES})")

    def resolve(p: str) -> str:
        return str((base / p).resolve()) if p else ""

    cfg.profile.resume_path = resolve(cfg.profile.resume_path)
    cfg.profile.cover_letter_path = resolve(cfg.profile.cover_letter_path)
    cfg.browser.profile_dir = resolve(cfg.browser.profile_dir)
    cfg.database = resolve(cfg.database)
    cfg.output_dir = resolve(cfg.output_dir)
    cfg.ai.cover_letter_example = resolve(cfg.ai.cover_letter_example)
    if not Path(cfg.profile.resume_path).is_file():
        raise ValueError(f"resume not found: {cfg.profile.resume_path}")
    return cfg
