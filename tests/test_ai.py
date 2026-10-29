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
    job = Job(**{**JOB.__dict__, "concerns": []})
    result = runner._process(site, job)
    assert result.detail == "excluded: salary max $140k"


def test_concerns_flag_instead_of_auto_submit(cfg, tmp_path):
    fit = FitScore(score=90, reason="", hard_exclusion="", concerns=["salary not listed"])
    runner, site = _runner(cfg, tmp_path, "auto", fit)
    job = Job(**{**JOB.__dict__, "concerns": []})
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
