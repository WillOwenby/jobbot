"""Run prompts through the local Claude Code CLI, so they count against a Claude
Pro/Max plan's usage limits instead of API credit."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

TIMEOUT_SECONDS = 300

# Phrases in a failed result that mean every following call would fail too.
FATAL_MARKERS = ("usage limit", "limit reached", "hit your limit", "/login", "not logged in",
                 "invalid api key", "authentication", "credit balance")


class ClaudeCodeError(RuntimeError):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class ClaudeCodeClient:
    def __init__(self, system_prompt: str, claude_path: str = ""):
        self.exe = claude_path or shutil.which("claude")
        if not self.exe:
            raise ClaudeCodeError(
                "the 'claude' command wasn't found. Install Claude Code "
                "(https://claude.com/claude-code), run 'claude' once to log in, then try again",
                fatal=True)
        # Long system prompts go in a file: Windows caps command lines at ~32k characters.
        self._workdir = Path(tempfile.mkdtemp(prefix="jobbot-claude-"))
        self._system_file = self._workdir / "system.txt"
        self._system_file.write_text(system_prompt, encoding="utf-8")

    def ask(self, prompt: str, model: str, effort: str, schema: dict | None = None):
        """Return an object shaped like an API response: .parsed_output and .content[].text."""
        cmd = [
            self.exe, "-p",
            "--output-format", "json",
            "--model", model,
            "--effort", effort,
            "--system-prompt-file", str(self._system_file),
            "--tools", "",               # answer only; never run tools on this machine
            "--strict-mcp-config",       # and don't load the user's MCP servers
            "--no-session-persistence",
        ]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  timeout=TIMEOUT_SECONDS, cwd=self._workdir)
        except subprocess.TimeoutExpired:
            raise ClaudeCodeError(f"Claude Code took longer than {TIMEOUT_SECONDS}s") from None
        except OSError as e:
            raise ClaudeCodeError(f"couldn't start Claude Code: {e}", fatal=True) from None

        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise ClaudeCodeError(f"Claude Code failed (exit {proc.returncode}): {detail}",
                                  fatal=_looks_fatal(detail)) from None

        if out.get("is_error") or out.get("subtype") != "success":
            detail = str(out.get("result") or out.get("subtype") or "unknown error")[:300]
            raise ClaudeCodeError(f"Claude Code error: {detail}", fatal=_looks_fatal(detail))

        text = out.get("result") or ""
        return SimpleNamespace(
            parsed_output=out.get("structured_output"),
            content=[SimpleNamespace(type="text", text=text)],
        )


def _looks_fatal(detail: str) -> bool:
    d = detail.lower()
    return any(m in d for m in FATAL_MARKERS)
