"""jobbot command line.

    python -m jobbot login linkedin          # log in once; the session is saved
    python -m jobbot run                     # search and apply using config.yaml
    python -m jobbot run --mode dry-run      # search, filter and score, but don't apply
    python -m jobbot report --csv out.csv    # what was applied to / skipped and why
    python -m jobbot questions               # questions you should add answers for
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import MODES, SUPPORTED_SITES, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobbot", description="Search job sites and apply for you.")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="open a browser to log in to a site")
    p_login.add_argument("site", choices=SUPPORTED_SITES)

    p_run = sub.add_parser("run", help="search and apply")
    p_run.add_argument("--site", action="append", choices=SUPPORTED_SITES,
                       help="site to run (repeatable; default: sites in config)")
    p_run.add_argument("--mode", choices=MODES, help="override the mode in config")
    p_run.add_argument("--max", type=int, help="max applications per site this run")
    p_run.add_argument("--recheck", action="store_true",
                       help="re-evaluate jobs skipped or flagged on earlier runs")
    p_run.add_argument("--debug", action="store_true",
                       help="save screenshots + HTML of the first results page and job page to output/debug")

    p_report = sub.add_parser("report", help="show application history")
    p_report.add_argument("--csv", help="also export to this CSV file")

    sub.add_parser("questions", help="list questions that blocked applications")
    sub.add_parser("shortlist", help="good matches to apply to yourself on the company's site")
    sub.add_parser("ai-answers", help="list answers the AI saved for reuse")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.command == "login":
        from .browser import open_browser
        from .prompter import Prompter
        from .sites import SITES
        with open_browser(cfg.browser) as context:
            site = SITES[args.site](context, cfg, filler=None, prompter=Prompter("review"))
            site.goto(site.login_url)
            input(f"Log in to {args.site} in the browser, then press Enter to save the session... ")
            print("Logged in." if site.is_logged_in() else "Still not logged in; try again.")
        return 0

    if args.command == "run":
        from .ai import AIFatalError
        from .sites.base import SiteLoggedOut
        from .runner import Runner
        mode = args.mode or cfg.mode
        runner = Runner(cfg, mode, args.max, args.recheck, args.debug)
        try:
            stats = runner.run(args.site or cfg.sites)
        except KeyboardInterrupt:
            print("\nStopped.")
            stats = runner.stats
        except (AIFatalError, SiteLoggedOut) as e:
            print(f"\nStopped: {e}")
            stats = runner.stats
        print("\nSummary: " + (", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "nothing processed"))
        return 0

    from .tracker import Tracker
    tracker = Tracker(cfg.database)

    if args.command == "report":
        rows = tracker.rows()
        for r in rows:
            fit = f"{r['fit_score']:>3}" if r["fit_score"] is not None else "  -"
            print(f"{r['updated_at']}  {r['status']:<15} fit {fit}  {r['title']} @ {r['company']}"
                  + (f"  ({r['detail']})" if r["detail"] else ""))
            if r["status"] == "flagged":
                print(f"{'':>38}{r['url']}")
        if args.csv:
            print(f"Wrote {tracker.export_csv(args.csv)} rows to {args.csv}")
        return 0

    if args.command == "shortlist":
        rows = tracker.shortlist()
        if not rows:
            print("No apply-yourself jobs yet (turn on search.include_external_jobs).")
        for r in rows:
            print(f"\n[{r['fit_score']}/100] {r['title']} @ {r['company']}  ({r['location']})")
            print(f"  {r['url']}")
            print(f"  {r['fit_reason']}")
            for c in json.loads(r["concerns"] or "[]"):
                print(f"  ! {c}")
        return 0

    if args.command == "questions":
        questions = tracker.unanswered_questions()
        if not questions:
            print("No blocked questions recorded.")
        for q, n in questions:
            print(f"{n:>3}x  {q}")
        return 0

    if args.command == "ai-answers":
        for q, a in tracker.ai_answers():
            print(f"Q: {q}\nA: {a}\n")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
