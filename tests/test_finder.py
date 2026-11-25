import json
from pathlib import Path

from jobbot.finder import Finder, Settings, _remote_us, doc_id, money

FIX = Path(__file__).parent / "fixtures"


def fake_fetch(routes):
    calls = []

    def fetch(url):
        calls.append(url)
        for frag, resp in routes.items():
            if frag in url:
                return resp() if callable(resp) else resp
        return 404, ""
    fetch.calls = calls
    return fetch


def settings(**kw):
    base = dict(keywords=["Epic Applications Manager"], title_exclude=["Sales Director"], sources=["boards"],
                company_boards=["greenhouse:acme", "lever:beta", "ashby:gamma"], days=7)
    base.update(kw)
    return Settings(**base)


LONG = "Lead our Epic clinical applications team. " * 10


def test_boards_keep_remote_us_leadership_jobs_only():
    gh = {"jobs": [
        {"id": 1, "title": "Director, Clinical Applications", "location": {"name": "Remote - US"},
         "absolute_url": "https://x/1", "content": "&lt;p&gt;" + LONG + "&lt;/p&gt;", "company_name": "Acme"},
        {"id": 2, "title": "Director, Clinical Applications", "location": {"name": "Boston, MA"},
         "absolute_url": "https://x/2", "content": LONG},
        {"id": 3, "title": "Senior Software Engineer", "location": {"name": "Remote"},
         "absolute_url": "https://x/3", "content": LONG},
        {"id": 4, "title": "Sales Director, Health", "location": {"name": "Remote"},
         "absolute_url": "https://x/4", "content": LONG},
    ]}
    lever = [{"id": "a", "text": "Epic Program Manager", "workplaceType": "remote", "country": "US",
              "categories": {"location": "Remote"}, "hostedUrl": "https://l/a", "description": LONG,
              "salaryRange": {"min": 150000, "max": 180000, "interval": "per-year-salary"}},
             {"id": "b", "text": "Epic Program Manager", "workplaceType": "remote", "country": "CA",
              "categories": {"location": "Remote"}, "hostedUrl": "https://l/b", "description": LONG}]
    ashby = {"jobs": [{"id": "z", "title": "Head of Integrations (HL7/FHIR)", "isRemote": True,
                       "location": "Remote", "jobUrl": "https://a/z", "descriptionPlain": LONG}]}
    f = Finder(settings(), seen={"ashby:gamma:old"}, fetch=fake_fetch({
        "greenhouse": (200, json.dumps(gh)), "lever": (200, json.dumps(lever)), "ashby": (200, json.dumps(ashby))}),
        sleep=lambda s: None)
    got = {c.key: c for c in f.run()}
    assert set(got) == {"greenhouse:acme:1", "lever:beta:a", "ashby:gamma:z"}
    assert got["greenhouse:acme:1"].description.startswith("Lead our Epic")
    assert got["lever:beta:a"].salary.startswith("$150k–$180k")


def test_seen_jobs_and_duplicate_titles_are_skipped():
    ashby = {"jobs": [
        {"id": "z", "title": "Director of Epic Applications", "isRemote": True, "jobUrl": "https://a/z", "descriptionPlain": LONG},
        {"id": "y", "title": "Director of Epic Applications", "isRemote": True, "jobUrl": "https://a/y", "descriptionPlain": LONG},
        {"id": "w", "title": "Manager, Clinical Applications", "isRemote": True, "jobUrl": "https://a/w", "descriptionPlain": LONG},
    ]}
    f = Finder(settings(company_boards=["ashby:gamma"]), seen={"ashby:gamma:w"},
               fetch=fake_fetch({"ashby": (200, json.dumps(ashby))}), sleep=lambda s: None)
    assert [c.key for c in f.run()] == ["ashby:gamma:z"]


def test_linkedin_search_then_descriptions_and_backoff():
    listing = (FIX / "linkedin_listing.html").read_text()
    posting = (FIX / "linkedin_posting.html").read_text()
    hits = {"n": 0}

    def search():
        hits["n"] += 1
        return (429, "") if hits["n"] == 1 else (200, listing)
    sleeps = []
    f = Finder(settings(sources=["linkedin"], title_exclude=[], max_candidates=2), seen=set(),
               fetch=fake_fetch({"seeMoreJobPostings": search, "jobPosting/": (200, posting)}),
               sleep=sleeps.append)
    out = f.run()
    assert out and all(c.source == "linkedin" and len(c.description) > 200 for c in out)
    assert len(out) <= 2
    assert 30 in sleeps  # backed off after the 429 instead of stopping


def test_helpers():
    assert doc_id("himalayas:acme/jobs/epic lead") == "himalayas:acme-jobs-epic-lead"
    assert money(150000, 180000) == "$150k–$180k"
    assert money(None, None) == ""
    assert _remote_us("Remote - US") and _remote_us("Remote") and not _remote_us("Remote - Canada")
    assert not _remote_us("Chicago, IL")
