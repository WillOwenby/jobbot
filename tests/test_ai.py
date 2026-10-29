from types import SimpleNamespace

import pytest

from jobbot.ai import AIError, FitScore, ResumeAI, ScreeningAnswer, write_cover_letter_pdf
from jobbot.config import AIConfig
from jobbot.forms import AnswerBook, Answerer
from jobbot.models import Job
from jobbot.tracker import Tracker

JOB = Job("linkedin", "42", "Python Developer", "Acme", "Remote", "https://x", "Django, Postgres")


class FakeMessages:
    def __init__(self, parsed=None, text="", stop_reason="end_turn"):
        self.parsed, self.text, self.stop_reason = parsed, text, stop_reason
        self.calls = []

    def _resp(self):
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.parsed,
                               content=[SimpleNamespace(type="text", text=self.text)])

    def parse(self, **kw):
        self.calls.append(kw)
        return self._resp()

    def create(self, **kw):
        self.calls.append(kw)
        return self._resp()


def make_ai(cfg, messages):
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return ResumeAI(AIConfig(enabled=True), cfg.profile, "4 years Python", client=client)


def test_score_fit_request_shape_and_clamp(cfg):
    msgs = FakeMessages(parsed=FitScore(score=130, reason="great", hard_exclusion="", concerns=[]))
    result = make_ai(cfg, msgs).score_fit(JOB)
    assert result.score == 100
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_format"] is FitScore
    assert call["fallbacks"] == "default"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "4 years Python" in call["system"][0]["text"]
    assert "Django, Postgres" in call["messages"][0]["content"]


def test_refusal_raises(cfg):
    ai = make_ai(cfg, FakeMessages(stop_reason="refusal"))
    with pytest.raises(AIError):
        ai.cover_letter(JOB)


def test_answerer_uses_ai_only_as_fallback_and_caches(cfg, tmp_path):
    msgs = FakeMessages(parsed=ScreeningAnswer(answer="Bachelor's Degree", confident=True))
    tracker = Tracker(str(tmp_path / "t.db"))
    answerer = Answerer(AnswerBook(cfg.profile, cfg.answers), make_ai(cfg, msgs), tracker)
    answerer.start_job(JOB)

    assert answerer.answer("Years of Python?") == "4"           # rule, no API call
    assert msgs.calls == []
    opts = ["High school", "Bachelor's Degree"]
    assert answerer.answer("Highest degree?", opts) == "Bachelor's Degree"
    assert answerer.answer("Highest degree?", opts) == "Bachelor's Degree"
    assert len(msgs.calls) == 1                                  # second one came from cache


def test_answerer_skips_unconfident_ai(cfg):
    msgs = FakeMessages(parsed=ScreeningAnswer(answer="Yes", confident=False))
    answerer = Answerer(AnswerBook(cfg.profile, []), make_ai(cfg, msgs))
    assert answerer.answer("Do you hold a CPA license?", ["Yes", "No"]) is None


def test_cover_letter_pdf(tmp_path):
    path = write_cover_letter_pdf("Dear Hiring Manager, “thanks” — Jane", JOB, str(tmp_path))
    assert path.endswith(".pdf")
    assert open(path, "rb").read(4) == b"%PDF"


def test_fit_prompt_includes_criteria(cfg):
    msgs = FakeMessages(parsed=FitScore(score=80, reason="ok", hard_exclusion="", concerns=[]))
    ai = make_ai(cfg, msgs)
    ai.cfg.job_criteria = "Minimum base salary $154,000"
    ai.score_fit(JOB)
    assert "Minimum base salary $154,000" in msgs.calls[0]["messages"][0]["content"]


LONG_DESCRIPTION = "Lead a remote team building Epic and RPA automation. " * 10


def test_unreadable_description_is_not_passed_through(cfg, tmp_path):
    fit = FitScore(score=99, reason="", hard_exclusion="", concerns=[])
    for mode in ("dry-run", "review", "auto"):
        runner, site = _runner(cfg, tmp_path, mode, fit)
        job = Job(**{**JOB.__dict__, "concerns": [], "description": "About the job"})
        result = runner._process(site, job)
        assert result.status.value == "failed", mode  # failed, so the next run retries it
        assert "wasn't scored" in result.detail


def test_ai_error_is_retried_next_run(cfg, tmp_path):
    runner, site = _runner(cfg, tmp_path, "review", None)

    def boom(job):
        raise AIError("rate limited")

    runner.ai = SimpleNamespace(score_fit=boom)
    job = Job(**{**JOB.__dict__, "concerns": [], "description": LONG_DESCRIPTION})
    result = runner._process(site, job)
    assert (result.status.value, result.detail) == ("failed", "not scored: rate limited")
    runner.tracker.record(job, result)
    assert not runner.tracker.is_done(job)


def test_fatal_ai_error_stops_the_run(cfg, tmp_path):
    import pytest
    from jobbot.ai import AIFatalError
    runner, site = _runner(cfg, tmp_path, "dry-run", None)

    def no_credit(job):
        raise AIFatalError("Anthropic credit balance is too low")

    runner.ai = SimpleNamespace(score_fit=no_credit)
    job = Job(**{**JOB.__dict__, "concerns": [], "description": LONG_DESCRIPTION})
    with pytest.raises(AIFatalError):
        runner._process(site, job)


def test_old_unscored_flags_are_rescored(cfg, tmp_path):
    from jobbot.models import ApplyResult, Status
    tracker = Tracker(str(tmp_path / "t.db"))
    job = Job(**{**JOB.__dict__, "concerns": []})
    tracker.record(job, ApplyResult(Status.FLAGGED, "not scored: Anthropic API error 400: credit balance"))
    assert not tracker.is_done(job)
    tracker.record(job, ApplyResult(Status.FLAGGED, "salary not listed"))
    assert tracker.is_done(job)


def _runner(cfg, tmp_path, mode, fit):
    from jobbot.runner import Runner
    cfg.database = str(tmp_path / "t.db")
    runner = Runner(cfg, mode)
    runner.ai = SimpleNamespace(score_fit=lambda job: fit)
    site = SimpleNamespace(open=lambda job: None,
                           apply=lambda job: (_ for _ in ()).throw(AssertionError("must not apply")))
    return runner, site


def test_hard_exclusion_skips(cfg, tmp_path):
    fit = FitScore(score=90, reason="", hard_exclusion="salary max $140k", concerns=[])
    runner, site = _runner(cfg, tmp_path, "auto", fit)
    job = Job(**{**JOB.__dict__, "concerns": [], "description": LONG_DESCRIPTION})
    result = runner._process(site, job)
    assert result.detail == "excluded: salary max $140k"


def test_concerns_flag_instead_of_auto_submit(cfg, tmp_path):
    fit = FitScore(score=90, reason="", hard_exclusion="", concerns=["salary not listed"])
    runner, site = _runner(cfg, tmp_path, "auto", fit)
    job = Job(**{**JOB.__dict__, "concerns": [], "description": LONG_DESCRIPTION})
    result = runner._process(site, job)
    assert result.status.value == "flagged"
    runner.tracker.record(job, result)
    assert runner.tracker.is_done(job)                 # not re-scored next run
    assert not runner.tracker.is_done(job, recheck=True)


def test_read_docx(tmp_path):
    import zipfile
    from jobbot.ai import read_document_text
    doc = tmp_path / "letter.docx"
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document><w:body><w:p><w:r><w:t>Dear team&apos;s</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t xml:space="preserve">Second </w:t></w:r><w:r><w:t>para</w:t></w:r></w:p>'
                   '</w:body></w:document>')
    assert read_document_text(str(doc)) == "Dear team's\nSecond para"


def test_cover_letter_uses_master_letter(cfg):
    msgs = FakeMessages(text="Dear Hiring Manager, ...")
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    ai = ResumeAI(AIConfig(enabled=True), cfg.profile, "resume", client=client,
                  example_letter="My master letter text")
    assert ai.cover_letter(JOB) == "Dear Hiring Manager, ..."
    assert "My master letter text" in msgs.calls[0]["messages"][0]["content"]


def test_scoring_can_use_its_own_model_and_effort(cfg):
    msgs = FakeMessages(parsed=FitScore(score=50, reason="", hard_exclusion="", concerns=[]))
    ai = make_ai(cfg, msgs)
    ai.cfg.scoring_model, ai.cfg.scoring_effort = "claude-haiku-5-5", "low"
    ai.score_fit(JOB)
    assert msgs.calls[0]["model"] == "claude-haiku-5-5"
    assert msgs.calls[0]["output_config"] == {"effort": "low"}
    msgs.text = "Dear Hiring Manager"
    ai.cover_letter(JOB)
    assert msgs.calls[1]["model"] == "claude-opus-5-5"     # letters keep the main model
    assert msgs.calls[1]["output_config"] == {"effort": "medium"}


def test_external_job_goes_to_shortlist_only_if_it_passes(cfg, tmp_path, capsys):
    from jobbot.__main__ import main
    from jobbot.models import Status
    good = FitScore(score=80, reason="Strong Epic leadership fit", hard_exclusion="", concerns=["salary not listed"])
    for mode in ("dry-run", "review", "auto"):
        runner, site = _runner(cfg, tmp_path, mode, good)
        job = Job(**{**JOB.__dict__, "concerns": [], "description": LONG_DESCRIPTION, "external": True})
        result = runner._process(site, job)          # site.apply would raise: never called
        assert result.status == Status.APPLY_YOURSELF, mode
    runner.tracker.record(job, result)
    assert runner.tracker.is_done(job)

    low = FitScore(score=40, reason="", hard_exclusion="", concerns=[])
    runner, site = _runner(cfg, tmp_path, "review", low)
    job2 = Job(**{**JOB.__dict__, "job_id": "2", "concerns": [], "description": LONG_DESCRIPTION, "external": True})
    assert runner._process(site, job2).status == Status.SKIPPED

    rows = runner.tracker.shortlist()
    assert [(r["job_id"], r["fit_score"]) for r in rows] == [("42", 80)]


def test_shortlist_command(cfg, tmp_path, capsys, monkeypatch):
    from jobbot import __main__ as cli
    from jobbot.models import ApplyResult, Status
    cfg.database = str(tmp_path / "t.db")
    t = Tracker(cfg.database)
    job = Job(**{**JOB.__dict__, "concerns": ["salary not listed"], "fit_score": 77, "fit_reason": "Good fit"})
    t.record(job, ApplyResult(Status.APPLY_YOURSELF, "fit 77"))
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    assert cli.main(["shortlist"]) == 0
    out = capsys.readouterr().out
    assert "[77/100] Python Developer @ Acme" in out and "https://x" in out and "! salary not listed" in out


def test_duplicate_listing_is_not_scored_twice(cfg, tmp_path):
    from jobbot.models import ApplyResult, Status
    runner, site = _runner(cfg, tmp_path, "dry-run", None)
    first = Job(**{**JOB.__dict__, "job_id": "100", "title": "Group Product Manager", "company": "Coinbase"})
    runner.tracker.record(first, ApplyResult(Status.SKIPPED, "fit score 15 < 65"))
    opened = []
    site.open = lambda job: opened.append(job)
    runner.ai = SimpleNamespace(score_fit=lambda job: (_ for _ in ()).throw(AssertionError("no AI call")))
    copy = Job(**{**JOB.__dict__, "job_id": "200", "title": "Group Product Manager ", "company": "coinbase"})
    result = runner._process(site, copy)
    assert result.status == Status.SKIPPED and "same job as another listing" in result.detail
    assert opened == []                                # not even opened

    # A listing whose earlier copy failed to score is still tried.
    runner.tracker.record(first, ApplyResult(Status.FAILED, "not scored: timeout"))
    assert runner.tracker.judged_duplicate(copy) is None
