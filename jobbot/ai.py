"""Claude-powered pieces: job-fit scoring, screening-question answers, cover letters."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import anthropic
from pydantic import BaseModel

from .config import AIConfig, ProfileConfig
from .models import Job

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You help one job seeker apply to jobs. You act on their behalf, so everything \
you write must be true to the candidate information below. Never invent \
employers, degrees, certifications, skills, or years of experience. If the \
candidate information doesn't support an answer, say so rather than guessing.

<candidate>
Name: {name}
Email: {email}
Phone: {phone}
Location: {city}
LinkedIn: {linkedin}
Website: {website}

Notes from the candidate (preferences, work authorization, salary, etc.):
{about_me}
</candidate>

<resume>
{resume}
</resume>
"""


class AIError(RuntimeError):
    pass


class FitScore(BaseModel):
    score: int
    reason: str
    hard_exclusion: str  # empty unless the posting clearly breaks a hard exclusion
    concerns: list[str]  # ambiguous conflicts for the candidate to review


class ScreeningAnswer(BaseModel):
    answer: str
    confident: bool


def read_resume_text(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(p).pages)
    elif p.suffix.lower() in (".txt", ".md"):
        text = p.read_text()
    else:
        raise ValueError(f"AI features need a .pdf, .txt or .md resume (got {p.suffix})")
    if not text.strip():
        raise ValueError(f"no text could be extracted from {p}; is it a scanned image?")
    return text


class ResumeAI:
    def __init__(self, cfg: AIConfig, profile: ProfileConfig, resume_text: str, client=None):
        self.cfg = cfg
        self.client = client or anthropic.Anthropic()
        system = SYSTEM_PROMPT.format(
            name=f"{profile.first_name} {profile.last_name}", email=profile.email,
            phone=profile.phone, city=profile.city or "(not given)",
            linkedin=profile.linkedin_url or "(not given)", website=profile.website or "(not given)",
            about_me=cfg.about_me.strip() or "(none)", resume=resume_text.strip(),
        )
        # The resume is the same on every call, so cache it.
        self.system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _request(self, prompt: str, output_format=None):
        kwargs = dict(
            model=self.cfg.model,
            max_tokens=16000,
            system=self.system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self.cfg.effort},
            # If a request is declined by a safety classifier, retry it on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        try:
            if output_format is not None:
                resp = self.client.beta.messages.parse(output_format=output_format, **kwargs)
            else:
                resp = self.client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise AIError("Anthropic authentication failed; set ANTHROPIC_API_KEY") from e
        except anthropic.RateLimitError as e:
            raise AIError("Anthropic rate limit hit") from e
        except anthropic.APIStatusError as e:
            raise AIError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AIError("could not reach the Anthropic API") from e

        if resp.stop_reason == "refusal":
            raise AIError("the model declined this request")
        if resp.stop_reason == "max_tokens":
            raise AIError("the response was cut off (max_tokens)")
        return resp

    @staticmethod
    def _text(resp) -> str:
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def score_fit(self, job: Job) -> FitScore:
        criteria = self.cfg.job_criteria.strip() or "(none given; use the candidate's notes)"
        prompt = f"""\
Evaluate this job for the candidate against their resume and job-search criteria.

<criteria>
{criteria}
</criteria>

- score: 0 (no fit) to 100 (excellent fit), weighing the criteria's priorities, \
the candidate's actual background, seniority, compensation and remote fit.
- reason: one or two sentences.
- hard_exclusion: only when the posting clearly and unambiguously breaks one of \
the hard exclusions, name it briefly (e.g. "salary range max $140k"). Otherwise "". \
Never exclude on a single ambiguous keyword or missing information.
- concerns: possible conflicts the candidate should check before applying \
(e.g. salary not listed, travel amount unclear, may be hands-on coding). \
Empty if none.

<job>
Title: {job.title}
Company: {job.company}
Location: {job.location}

{job.description[:20000]}
</job>"""
        result = self._request(prompt, FitScore).parsed_output
        result.score = max(0, min(100, result.score))
        return result

    def answer_question(self, question: str, options: list[str] | None, job: Job | None) -> ScreeningAnswer:
        if options:
            shape = ("It is multiple choice. Your answer must be exactly one of these options, "
                     "copied verbatim:\n" + "\n".join(f"- {o}" for o in options))
        else:
            shape = ("It is a free-text field. Answer the way the candidate would type it: a number "
                     "for numeric questions (e.g. years of experience), otherwise a short, direct answer.")
        context = f"\nThe question is part of an application for {job.title} at {job.company}." if job else ""
        prompt = f"""\
Answer this job-application screening question for the candidate.{context}

<question>{question}</question>

{shape}

Set confident to false if the candidate information doesn't clearly support the \
answer (for example, a skill or credential that isn't mentioned); a person will \
answer those instead."""
        return self._request(prompt, ScreeningAnswer).parsed_output

    def cover_letter(self, job: Job) -> str:
        prompt = f"""\
Write a cover letter from the candidate for the job below. Keep it under 300 words, \
specific to this role and company, and grounded only in the resume. Start with \
"Dear Hiring Manager," and end with the candidate's name. Output only the letter.

<job>
Title: {job.title}
Company: {job.company}

{job.description[:20000]}
</job>"""
        return self._text(self._request(prompt))


def write_cover_letter_pdf(text: str, job: Job, out_dir: str) -> str:
    from fpdf import FPDF

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{job.company}-{job.title}").strip("-")[:80]
    path = Path(out_dir) / f"cover-letter-{slug}-{job.job_id}.pdf"
    pdf = FPDF(format="Letter")
    pdf.set_margins(25, 25, 25)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    # Core PDF fonts are Latin-1 only; swap typographic characters for plain ones.
    clean = (text.replace("’", "'").replace("‘", "'").replace("“", '"')
             .replace("”", '"').replace("—", "-").replace("–", "-"))
    pdf.multi_cell(0, 6, clean.encode("latin-1", "replace").decode("latin-1"))
    pdf.output(str(path))
    return str(path)
