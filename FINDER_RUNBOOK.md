# Fit Finder: scheduled run instructions

These are the steps a scheduled Claude Code session follows to refresh the Fit Finder app.
The app is a claude.ai artifact; its URL is given in the scheduled task's prompt (APP below).
Every personal detail (resume, master cover letter, experience, rules, search settings) lives
in the app's private database, never in this repository, which is public.

The ArtifactData tool may need loading first: ToolSearch `select:ArtifactData`.

## 1. Load the profile and what's already been seen

1. `ArtifactData get` APP, collection `profile`, doc `main`. Fields:
   - `resume`, `experience`, `about_me`, `job_criteria`, `facts`, `min_fit`
   - `cover_letter_master`
   - `search`: the finder settings
2. Write `search` to `/tmp/ff/settings.json`.
3. `ArtifactData get` APP, `meta/seen`. Its `keys` field is a list of job ids already judged. Write them one per line to `/tmp/ff/seen.txt`; the document may not exist yet.
4. `ArtifactData list` APP collection `jobs` (limit 1000). Add every job id to `/tmp/ff/seen.txt` too.

## 2. Find new jobs

```bash
python -m jobbot.finder --settings /tmp/ff/settings.json --seen /tmp/ff/seen.txt --out /tmp/ff/candidates.json
```

- It takes up to about 15 minutes because LinkedIn requests are spaced out on purpose. Run it in the background and wait.
- Don't add sources, raise request rates, or use logins, proxies or browser automation. Only the public sources in `jobbot/finder.py` are allowed.

## 3. Score each candidate

Read `/tmp/ff/candidates.json`. Judge every candidate against the profile yourself:
- Use the resume, the experience list, `about_me`, `facts` and `job_criteria`.
- Follow `job_criteria` exactly, including HARD EXCLUSIONS and DO NOT REJECT FOR.

For each candidate decide:
- `fit`: 0–100.
- `exclusion`: the hard exclusion the posting clearly states, or null. A missing salary or unclear travel is not an exclusion; it's a concern.
- `reason`: 2–3 plain sentences on why it fits or doesn't, in the second person ("your Epic ClinDoc work…").
- `concerns`: short list of things to check before applying.

Keep a job when `exclusion` is null and `fit >= min_fit`.

Also keep a strong job held back only by an ambiguous point, but with `flag: true`. The candidate wants those flagged, not rejected.

Be honest. Don't inflate scores, and never claim formal direct reports or certifications the candidate doesn't have.

## 4. Save results

Use `ArtifactData batch` (at most 50 writes per batch).

**Each kept job:** op `set`, collection `jobs`, doc_id = the candidate `key`. Fields:

```
title, company, location, url, source, salary, posted,
fit, reason, concerns (list), flag (bool),
found: today's date (YYYY-MM-DD),
status: "new"
```

Only create docs that don't exist yet. Never overwrite a job's `status`, `notes`, `letter` or `letter_state`.

**`meta/seen`:** `set` with `keys` = the old keys + every candidate key from this run, kept or not.
- Keep only the newest 5000.
- Pass `if_version` if the doc existed.

**`meta/run`:** `set` with:

```
at: ISO time now
added: number kept
scored: number of candidates
by_source: found_by_source from candidates.json
notes: the notes list from candidates.json
```

## 5. Write requested cover letters

`ArtifactData query` APP collection `jobs` where `letter_state == "requested"`. For each:

1. Get the posting text. Re-fetch the posting if you need it:
   - LinkedIn: `https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id>`, parsed with `jobbot.linkedin_public.parse_posting`.
   - Otherwise open the job's `url`.
2. Write a tailored cover letter.
   - Base it on `cover_letter_master`: keep its voice, structure and true facts, and tailor the middle to this job.
   - Use only experience that's in the resume or experience list.
   - Under 400 words. Sign it with `name`.
3. `update` the job with `letter` = the text and `letter_state` = `"ready"`, using `if_version` from the query. If it fails, set `letter_state` = `"failed"`.

## 6. Finish

Reply with one short summary: jobs found per source, how many were kept, the top three by fit, and any letters written.

Don't commit or push anything.
