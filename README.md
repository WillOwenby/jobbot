# jobbot

A personal job-application bot, in the spirit of AIApply, that runs on your own machine. It:

1. **Searches** LinkedIn (Easy Apply jobs) and Indeed ("Easily apply" jobs) using your keywords, locations, remote preference and posting date.
2. **Filters** out jobs by title, company and description keywords.
3. **Scores fit with Claude.** Claude reads each job description against your resume and skips jobs that score below your threshold.
4. **Writes a tailored cover letter** for each job, saved as a PDF in `output/`, and uploads it or pastes it in when the form asks for one.
5. **Fills in the application**: contact details, resume upload and screening questions. Answers come from your own rules first, then your profile, then Claude, which works from your resume. Claude never makes up experience you don't have. If it isn't sure of an answer, it hands the question to you.
6. **Logs everything** to SQLite: what it applied to, what it skipped and why, and which questions blocked it.

> ⚠️ **Read this first.** LinkedIn's and Indeed's Terms of Service prohibit automated applying. People use tools like this, but your account can be restricted or banned. jobbot keeps the risk lower by using a real, visible browser that you log into yourself, applying slowly with random delays, capping applications per run, and, in `review` mode (the default), asking you before every submit. It doesn't try to evade the sites' bot detection. Use it at your own risk.

## Protecting your LinkedIn account

LinkedIn flags accounts that load many pages quickly. jobbot keeps your account out of
everything except applying:

- **Searching and reading jobs** use LinkedIn's public, logged-out pages, fetched without
  your cookies and at a few seconds per request. A `dry-run` never uses your login at all.
- **Applying** (review/auto mode) opens the job in your logged-in browser: at most
  12 job pages per run, 45–90 seconds apart.
- If LinkedIn shows a logged-out page, or says "too many requests", jobbot stops the run.

Keep runs modest: once a day, with `posted_within_days: 1` once you've caught up.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml      # then edit it
export ANTHROPIC_API_KEY=sk-ant-...     # only needed if ai.enabled
```

Put your resume next to `config.yaml`. A PDF works best, because Claude reads its text.

Log in once per site. The browser profile in `browser-profile/` keeps you logged in between runs:

```bash
python -m jobbot login linkedin
python -m jobbot login indeed
```

## Use

```bash
python -m jobbot run --mode dry-run        # see what it would apply to, with fit scores
python -m jobbot run                       # review mode: you approve each submit
python -m jobbot run --site linkedin --max 5
python -m jobbot report --csv history.csv
python -m jobbot questions                 # questions it couldn't answer; add rules for them
python -m jobbot ai-answers                # answers Claude saved for reuse; check them
python -m jobbot shortlist                 # strong matches to apply to yourself (company-site jobs)
```

**Modes**
- `review` fills in the whole application, then asks you in the terminal before submitting. If it hits a question it can't answer, it pauses so you can fill it in the browser.
- `auto` submits on its own and skips (discards) any application it can't complete.
- `dry-run` searches, filters and scores jobs, but never opens an application.

Start with `dry-run`, then use `review` for a while. Only switch to `auto` once `jobbot questions` comes back mostly empty and you trust the answers in `jobbot ai-answers`.

## How questions get answered

For each form field, jobbot tries these in order:

1. Your `answers:` rules in `config.yaml`. Each is a regex tested against the question; the first match wins.
2. Your profile fields (name, email, phone, city, LinkedIn, website).
3. An answer Claude gave earlier for the same question. Short and multiple-choice answers are cached in `jobbot.db`.
4. Claude, using your resume and the `ai.about_me` notes. Answers it isn't confident about are left for you.

If you don't like an answer, add a rule for that question. Rules always win over Claude.

## Using your Claude plan instead of API credit

Set `ai.provider: claude-code` to send every AI request through the Claude Code
command-line tool. It then counts against your Claude Pro or Max plan's usage limits,
and there's no API billing. Set it up once:

```powershell
irm https://claude.ai/install.ps1 | iex   # Windows; see claude.com/claude-code for Mac/Linux
claude                                     # log in with your Claude account, then type /exit
```

jobbot runs `claude` with all tools disabled, so the model only answers; it can't run
anything on your computer. If you hit your plan's usage limit, the run stops cleanly. Run
it again after the limit resets; nothing is lost.

## Costs

Each job that passes the keyword filters costs one Claude call to score fit. Jobs you actually apply to cost one more call for the cover letter, plus one call for each new screening question. Your resume is sent as a cached prompt prefix, so repeated calls are cheaper. Set `ai.effort: low` to spend less, or `ai.enabled: false` to run with no AI at all. Requests that a safety filter declines are retried on a fallback model automatically (`fallbacks: "default"`).

## When it breaks

Job sites change their HTML often. Every CSS selector jobbot relies on is listed at the top of `jobbot/sites/linkedin.py` and `jobbot/sites/indeed.py`. If searches return nothing or a button isn't found, open the page in Chrome DevTools and update the selector. Run with `-v` for debug logs.

Indeed often shows "verify you are human" challenges. Solve them in the browser window; the saved profile helps them stay solved.

## Project layout

```
jobbot/
  __main__.py     CLI
  runner.py       search → filter → score → cover letter → apply loop
  ai.py           Claude: fit score, screening answers, cover letters
  forms.py        generic form filler + answer resolution
  sites/          LinkedIn and Indeed adapters (search, open, apply)
  tracker.py      SQLite history + AI answer cache
  config.py       config.yaml schema
tests/            unit tests + offline browser tests against fake pages
```

Run the tests with `pytest`. The browser tests drive the real form filler and the LinkedIn Easy Apply flow against fake pages, so they need no account or network.

## Ideas for next steps

- Add more sites (Glassdoor, Wellfound, Greenhouse/Lever career pages) by subclassing `JobSite`.
- Tailor your resume for each job, not just the cover letter. Have Claude reorder or emphasize existing bullets only; never invent new ones.
- Run it on a schedule (cron) in `auto` mode with a small `--max`.
