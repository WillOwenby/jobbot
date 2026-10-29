import json
import subprocess
from types import SimpleNamespace

import pytest

from jobbot import claude_code
from jobbot.ai import AIError, AIFatalError, FitScore, ResumeAI
from jobbot.config import AIConfig
from jobbot.models import Job

JOB = Job("linkedin", "1", "RPA Manager", "Acme", "Remote", "u", "Lead RPA. " * 50)


def make_ai(cfg, monkeypatch, stdout, returncode=0, calls=None):
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: "/usr/bin/claude")

    def fake_run(cmd, **kw):
        if calls is not None:
            calls.append((cmd, kw))
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)
    ai_cfg = AIConfig(enabled=True, provider="claude-code", scoring_model="claude-haiku-5-5")
    return ResumeAI(ai_cfg, cfg.profile, "resume text")


def result(**fields):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", **fields})


def test_score_fit_through_claude_code(cfg, monkeypatch):
    calls = []
    out = result(structured_output={"score": 80, "reason": "good", "hard_exclusion": "", "concerns": []})
    ai = make_ai(cfg, monkeypatch, out, calls=calls)
    fit = ai.score_fit(JOB)
    assert isinstance(fit, FitScore) and fit.score == 80
    cmd, kw = calls[0]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--tools") + 1] == ""               # no tools on the user's machine
    assert "--json-schema" in cmd
    assert "Lead RPA." in kw["input"]                        # prompt goes over stdin, not argv
    system_file = cmd[cmd.index("--system-prompt-file") + 1]
    assert "resume text" in open(system_file, encoding="utf-8").read()


def test_cover_letter_uses_text_result(cfg, monkeypatch):
    ai = make_ai(cfg, monkeypatch, result(result="Dear Hiring Manager, ..."))
    assert ai.cover_letter(JOB) == "Dear Hiring Manager, ..."


def test_usage_limit_stops_the_run(cfg, monkeypatch):
    out = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Claude AI usage limit reached. Your limit resets at 5pm."})
    ai = make_ai(cfg, monkeypatch, out)
    with pytest.raises(AIFatalError, match="usage limit"):
        ai.score_fit(JOB)


def test_not_logged_in_stops_the_run(cfg, monkeypatch):
    ai = make_ai(cfg, monkeypatch, "", returncode=1)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(stdout="Invalid API key · Please run /login",
                                                          stderr="", returncode=1))
    with pytest.raises(AIFatalError):
        ai.score_fit(JOB)


def test_bad_shape_is_a_normal_error(cfg, monkeypatch):
    ai = make_ai(cfg, monkeypatch, result(structured_output={"score": "high"}))
    with pytest.raises(AIError) as e:
        ai.score_fit(JOB)
    assert not isinstance(e.value, AIFatalError)


def test_timeout_is_a_normal_error(cfg, monkeypatch):
    ai = make_ai(cfg, monkeypatch, "")

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 300)

    monkeypatch.setattr(claude_code.subprocess, "run", slow)
    with pytest.raises(AIError) as e:
        ai.score_fit(JOB)
    assert not isinstance(e.value, AIFatalError)


def test_missing_claude_command(cfg, monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: None)
    with pytest.raises(AIFatalError, match="Install Claude Code"):
        ResumeAI(AIConfig(enabled=True, provider="claude-code"), cfg.profile, "resume")


def test_api_key_is_hidden_from_claude(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-old")
    calls = []
    ai = make_ai(cfg, monkeypatch, result(result="Dear Hiring Manager"), calls=calls)
    ai.cover_letter(JOB)
    env = calls[0][1]["env"]
    assert "ANTHROPIC_API_KEY" not in env and "PATH" in env
