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
    msgs = FakeMessages(parsed=FitScore(score=130, reason="great"))
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
