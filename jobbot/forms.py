"""Answer application questions and fill Playwright forms with the answers."""
from __future__ import annotations

import logging
import re

from playwright.sync_api import Locator

from .config import AnswerRule, ProfileConfig
from .models import Job

log = logging.getLogger(__name__)

PLACEHOLDER_OPTIONS = {"", "select an option", "select", "select...", "choose", "choose an option",
                       "please select", "--", "-"}
AFFIRMATIVE = {"yes", "true", "y", "i agree", "agree", "accept", "i accept"}


def normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    # Screen-reader text often repeats the label: "Question? Question?"
    m = re.fullmatch(r"(.+?)\s+\1", text)
    if m:
        text = m.group(1)
    text = re.sub(r"\s*\(?\brequired\b\)?\s*$", "", text, flags=re.I)
    return text.rstrip(" *").strip()


def choose_option(answer: str, options: list[str]) -> int | None:
    """Index of the option that best matches `answer`, ignoring placeholders."""
    a = normalize(answer).lower()
    if not a:
        return None
    candidates = [(i, normalize(o).lower()) for i, o in enumerate(options)]
    candidates = [(i, o) for i, o in candidates if o not in PLACEHOLDER_OPTIONS]
    for i, o in candidates:
        if o == a:
            return i
    for i, o in candidates:
        if o.startswith(a) or a.startswith(o):
            return i
    for i, o in candidates:
        if a in o:
            return i
    return None


class AnswerBook:
    """Static answers: the user's regex rules first, then profile fields."""

    def __init__(self, profile: ProfileConfig, rules: list[AnswerRule]):
        self._rules = [(re.compile(r.match, re.I), r.answer) for r in rules]
        full_name = f"{profile.first_name} {profile.last_name}".strip()
        for pattern, value in [
            (r"first\s*name|given\s*name", profile.first_name),
            (r"last\s*name|surname|family\s*name", profile.last_name),
            (r"full\s*name|^name$|your name", full_name),
            (r"e-?mail", profile.email),
            (r"phone|mobile", profile.phone),
            (r"\bcity\b|location", profile.city),
            (r"linkedin", profile.linkedin_url),
            (r"website|portfolio|github", profile.website),
        ]:
            if value:
                self._rules.append((re.compile(pattern, re.I), value))

    def lookup(self, question: str) -> str | None:
        q = normalize(question)
        if not q:
            return None
        for rx, answer in self._rules:
            if rx.search(q):
                return answer
        return None


class Answerer:
    """Resolve a question: rules -> profile -> cached AI answer -> Claude."""

    def __init__(self, book: AnswerBook, ai=None, tracker=None):
        self.book = book
        self.ai = ai
        self.tracker = tracker
        self.job: Job | None = None
        self.cover_letter_text: str | None = None
        self.cover_letter_path: str | None = None

    def start_job(self, job: Job, cover_letter_text: str | None = None, cover_letter_path: str | None = None):
        self.job = job
        self.cover_letter_text = cover_letter_text
        self.cover_letter_path = cover_letter_path

    def answer(self, question: str, options: list[str] | None = None) -> str | None:
        q = normalize(question)
        if self.cover_letter_text and re.search(r"cover\s*letter", q, re.I) and not options:
            return self.cover_letter_text
        found = self.book.lookup(q)
        if found is not None or self.ai is None or not q:
            return found

        from .ai import AIError
        cache_key = q + (" || " + " | ".join(normalize(o) for o in options) if options else "")
        if self.tracker and (cached := self.tracker.cached_answer(cache_key)) is not None:
            return cached
        try:
            result = self.ai.answer_question(q, options, self.job)
        except AIError as e:
            log.warning("AI could not answer %r: %s", q, e)
            return None
        if not result.confident:
            log.info("AI not confident about %r; leaving it for you", q)
            return None
        log.info("AI answered %r -> %r", q, result.answer[:80])
        # Reuse short/multiple-choice answers across jobs; long free text is job-specific.
        if self.tracker and (options or len(result.answer) <= 60):
            self.tracker.cache_answer(cache_key, result.answer)
        return result.answer


LABEL_JS = r"""
el => {
  const text = n => ((n && n.innerText) || '').trim();
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (text(l)) return text(l);
  }
  const ids = el.getAttribute('aria-labelledby');
  if (ids) {
    const t = ids.split(/\s+/).map(i => text(document.getElementById(i))).join(' ').trim();
    if (t) return t;
  }
  if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
  const wrap = el.closest('label');
  if (text(wrap)) return text(wrap);
  const fs = el.closest('fieldset');
  if (fs && text(fs.querySelector('legend'))) return text(fs.querySelector('legend'));
  return el.getAttribute('placeholder') || el.getAttribute('name') || '';
}
"""

GROUP_LABEL_JS = r"""
g => {
  const text = n => ((n && n.innerText) || '').trim();
  const legend = g.querySelector('legend');
  if (text(legend)) return text(legend);
  const ids = g.getAttribute('aria-labelledby');
  if (ids) return ids.split(/\s+/).map(i => text(document.getElementById(i))).join(' ');
  return g.getAttribute('aria-label') || text(g).split('\n')[0];
}
"""

REQUIRED_JS = "el => el.required || el.getAttribute('aria-required') === 'true'"

TEXT_FIELDS = (
    "input:visible:not([type=hidden]):not([type=radio]):not([type=checkbox])"
    ":not([type=file]):not([type=submit]):not([type=button]), textarea:visible"
)


class FormFiller:
    def __init__(self, answerer: Answerer, profile: ProfileConfig):
        self.answerer = answerer
        self.profile = profile

    def fill(self, root: Locator) -> list[str]:
        """Fill every empty field under `root`. Returns required questions left unanswered."""
        unanswered: list[str] = []
        self._fill_files(root)
        self._fill_text(root, unanswered)
        self._fill_selects(root, unanswered)
        self._fill_groups(root, "radio", unanswered)
        self._fill_groups(root, "checkbox", unanswered)
        return unanswered

    @staticmethod
    def _label(el: Locator) -> str:
        return el.evaluate(LABEL_JS)

    @staticmethod
    def _required(el: Locator, raw_label: str) -> bool:
        return el.evaluate(REQUIRED_JS) or bool(re.search(r"\*|\brequired\b", raw_label, re.I))

    def _fill_files(self, root: Locator) -> None:
        for inp in root.locator("input[type=file]").all():
            if inp.evaluate("e => e.files.length"):
                continue
            hint = " ".join(filter(None, [self._label(inp), inp.get_attribute("name"), inp.get_attribute("id")]))
            if re.search(r"cover", hint, re.I):
                path = self.answerer.cover_letter_path or self.profile.cover_letter_path
            else:
                path = self.profile.resume_path
            if path:
                inp.set_input_files(path)

    def _fill_text(self, root: Locator, unanswered: list[str]) -> None:
        for el in root.locator(TEXT_FIELDS).all():
            if el.input_value().strip():
                continue
            raw = self._label(el)
            answer = self.answerer.answer(raw)
            if answer is None:
                if self._required(el, raw):
                    unanswered.append(normalize(raw))
                continue
            el.fill(answer)
            if el.get_attribute("role") == "combobox" or el.get_attribute("aria-autocomplete"):
                # Typeahead (e.g. city): wait for suggestions and take the first.
                el.page.wait_for_timeout(1200)
                el.press("ArrowDown")
                el.press("Enter")

    def _fill_selects(self, root: Locator, unanswered: list[str]) -> None:
        for sel in root.locator("select:visible").all():
            current = sel.evaluate("s => s.selectedIndex >= 0 ? s.options[s.selectedIndex].text : ''")
            if normalize(current).lower() not in PLACEHOLDER_OPTIONS:
                continue
            raw = self._label(sel)
            options = sel.locator("option").all_inner_texts()
            real_options = [o for o in options if normalize(o).lower() not in PLACEHOLDER_OPTIONS]
            answer = self.answerer.answer(raw, real_options)
            idx = choose_option(answer, options) if answer else None
            if idx is not None:
                sel.select_option(index=idx)
            elif self._required(sel, raw):
                unanswered.append(normalize(raw))

    def _fill_groups(self, root: Locator, kind: str, unanswered: list[str]) -> None:
        selector = f"fieldset:has(input[type={kind}])"
        if kind == "radio":
            selector += ", [role=radiogroup]"
        for group in root.locator(selector).all():
            if not group.is_visible() or group.locator(f"input[type={kind}]:checked").count():
                continue
            inputs = group.locator(f"input[type={kind}]")
            raw = group.evaluate(GROUP_LABEL_JS)
            labels = [normalize(i.evaluate(LABEL_JS)) for i in inputs.all()]
            answer = self.answerer.answer(raw, labels)
            idx = choose_option(answer, labels) if answer else None
            if idx is None and kind == "checkbox" and len(labels) == 1 and answer \
                    and normalize(answer).lower() in AFFIRMATIVE:
                idx = 0
            if idx is not None:
                # A native click() fires the events React-style forms listen for,
                # even when the real input is visually hidden behind a styled label.
                inputs.nth(idx).evaluate("e => e.click()")
            elif self._required(inputs.first, raw):
                unanswered.append(normalize(raw))
