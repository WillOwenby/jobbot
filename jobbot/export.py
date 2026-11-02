"""Write the shortlist to a file: a browser page with clickable links, or a CSV for Excel."""
from __future__ import annotations

import csv
import html
import json
from datetime import date


def _how(row) -> str:
    return "Easy Apply" if row["status"] == "dry_run" else "Company site"


def write_csv(rows, path: str) -> None:
    # utf-8-sig so Excel shows accented and typographic characters correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Fit", "Title", "Company", "Location", "Apply via", "Link", "Why it fits",
                    "Concerns", "Found", "Applied?"])
        for r in rows:
            w.writerow([r["fit_score"], r["title"], r["company"], r["location"], _how(r), r["url"],
                        r["fit_reason"], "\n".join(json.loads(r["concerns"] or "[]")),
                        (r["updated_at"] or "")[:10], ""])


def write_html(rows, path: str) -> None:
    e = html.escape
    cards = []
    for r in rows:
        concerns = "".join(f"<li>{e(c)}</li>" for c in json.loads(r["concerns"] or "[]"))
        cards.append(f"""
<article>
  <div class="head"><span class="fit">{e(str(r['fit_score']))}</span>
    <div><h2><a href="{e(r['url'])}" target="_blank" rel="noopener">{e(r['title'])}</a></h2>
    <p class="meta">{e(r['company'])} · {e(r['location'] or '')} · <b>{_how(r)}</b></p></div></div>
  <p>{e(r['fit_reason'] or '')}</p>
  {f'<details><summary>Concerns to check</summary><ul>{concerns}</ul></details>' if concerns else ''}
</article>""")
    body = "".join(cards) or "<p>No matches yet. Run a dry run first.</p>"
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job shortlist</title>
<style>
  :root {{ --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#5d6675; --accent:#0a66c2; --line:#e3e6ea; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --card:#1d2128; --text:#e8eaee;
    --muted:#a2a9b5; --accent:#70b5f9; --line:#2d323b; }} }}
  body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--text);
         font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width:860px; margin:0 auto; }}
  h1 {{ margin:0 0 4px; font-size:24px; }} .sub {{ color:var(--muted); margin:0 0 20px; }}
  article {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin:0 0 12px; }}
  .head {{ display:flex; gap:14px; align-items:flex-start; }}
  .fit {{ flex:none; min-width:44px; text-align:center; font-weight:700; font-size:18px; padding:6px 0;
          border-radius:8px; background:var(--accent); color:var(--card); }}
  h2 {{ margin:0; font-size:17px; }} a {{ color:var(--accent); }}
  .meta {{ margin:2px 0 0; color:var(--muted); }} details {{ color:var(--muted); }} summary {{ cursor:pointer; }}
</style></head>
<body><main><h1>Job shortlist</h1>
<p class="sub">{len(rows)} matches, best fit first · generated {date.today():%B %d, %Y}</p>
{body}
</main></body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
