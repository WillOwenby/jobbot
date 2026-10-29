"""Drive the real form-filling and LinkedIn Easy Apply code against fake pages."""
from jobbot.forms import AnswerBook, Answerer, FormFiller
from jobbot.models import Job, Status
from jobbot.prompter import Prompter
from jobbot.sites.linkedin import LinkedIn

FORM = """
<form id="f">
  <label for="fn">First name</label><input id="fn" required>
  <label for="em">Email address</label><input id="em" type="email">
  <label for="yrs">How many years of Python experience do you have?</label>
  <input id="yrs" type="number" aria-required="true">
  <label for="deg">Highest degree *</label>
  <select id="deg"><option>Select an option</option><option>Bachelor's Degree</option></select>
  <fieldset><legend>Will you require sponsorship?</legend>
    <input type="radio" name="sp" id="sp1" value="y"><label for="sp1">Yes</label>
    <input type="radio" name="sp" id="sp2" value="n"><label for="sp2">No</label>
  </fieldset>
  <label for="color">Favorite color *</label><input id="color">
  <label for="opt">Anything else?</label><textarea id="opt"></textarea>
</form>
"""


class FixedAI:
    """Stands in for ResumeAI: knows only the degree question."""

    def answer_question(self, question, options, job):
        from jobbot.ai import ScreeningAnswer
        if "degree" in question.lower():
            return ScreeningAnswer(answer="Bachelor's Degree", confident=True)
        return ScreeningAnswer(answer="?", confident=False)


def make_filler(cfg, ai=None):
    return FormFiller(Answerer(AnswerBook(cfg.profile, cfg.answers), ai), cfg.profile)


def test_form_filler(page, cfg):
    page.set_content(FORM)
    unanswered = make_filler(cfg, FixedAI()).fill(page.locator("#f"))

    assert page.input_value("#fn") == "Jane"
    assert page.input_value("#em") == "jane@example.com"
    assert page.input_value("#yrs") == "4"
    assert page.eval_on_selector("#deg", "s => s.value") == "Bachelor's Degree"
    assert page.is_checked("#sp2")
    assert page.input_value("#opt") == ""                  # optional, unknown: left alone
    assert unanswered == ["Favorite color"]                # required, unknown: reported


LINKEDIN_JOB = """
<html><body>
<div id="job-details">We want Python and Django.</div>
<button class="jobs-apply-button" onclick="openModal()">Easy Apply</button>
<div id="root"></div>
<script>
const steps = [
  `<label for="ph">Mobile phone number</label><input id="ph" required>
   <button aria-label="Continue to next step" onclick="next(['ph'])">Next</button>`,
  `<label for="yrs">Years of Python?</label><input id="yrs" required>
   <fieldset><legend>Require sponsorship?</legend>
     <input type="radio" name="s" id="s1"><label for="s1">Yes</label>
     <input type="radio" name="s" id="s2"><label for="s2">No</label></fieldset>
   EXTRA
   <button aria-label="Review your application" onclick="next(['yrs'])">Review</button>`,
  `<input type="checkbox" id="follow-company-checkbox" checked>
   <label for="follow-company-checkbox">Follow</label>
   <button aria-label="Submit application" onclick="submitApp()">Submit</button>`,
];
let step = 0;
window.extra = '';
function render() {
  document.getElementById('root').innerHTML =
    `<div class="jobs-easy-apply-modal" role="dialog">
       <button aria-label="Dismiss" onclick="closeModal()">x</button>
       <div id="body">${steps[step].replace('EXTRA', window.extra)}</div></div>`;
}
function openModal() { step = 0; render(); }
function closeModal() { document.getElementById('root').innerHTML = ''; window.discarded = true; }
function next(ids) {
  const missing = ids.concat(step === 1 && window.extra ? ['color'] : []).filter(i => !document.getElementById(i).value);
  if (missing.length) {
    document.getElementById('body').insertAdjacentHTML('beforeend',
      '<div class="artdeco-inline-feedback--error">Please enter a valid answer</div>');
    return;
  }
  step++; render();
}
function submitApp() {
  window.submitted = { follow: document.getElementById('follow-company-checkbox').checked };
  document.getElementById('root').innerHTML =
    '<div role="dialog"><p>Application sent</p><button aria-label="Dismiss">x</button></div>';
}
</script></body></html>
"""


def linkedin(context, cfg, extra_html=""):
    html = LINKEDIN_JOB.replace("window.extra = '';", f"window.extra = `{extra_html}`;")
    context.route("https://www.linkedin.com/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    return LinkedIn(context, cfg, make_filler(cfg), Prompter("auto"))


JOB = Job("linkedin", "123", "Python Developer", "Acme", "Remote", "https://www.linkedin.com/jobs/view/123/")


def test_linkedin_easy_apply_end_to_end(context, cfg):
    site = linkedin(context, cfg)
    assert site.open(JOB) is None
    assert "Django" in JOB.description

    result = site.apply(JOB)

    assert result.status == Status.APPLIED, result
    assert site.page.evaluate("window.submitted") == {"follow": False}


def test_linkedin_unknown_required_question_discards_in_auto_mode(context, cfg):
    extra = '<label for="color">Favorite color *</label><input id="color" required>'
    site = linkedin(context, cfg, extra)
    site.open(JOB)

    result = site.apply(JOB)

    assert result.status == Status.NEEDS_INPUT
    assert result.questions == ["Favorite color"]
    assert site.page.evaluate("window.discarded") is True
    assert not site.page.evaluate("window.submitted")


def test_goto_retries_when_site_redirects_mid_load(context, cfg):
    from playwright.sync_api import Error as PlaywrightError

    site = LinkedIn(context, cfg, None, Prompter("auto"))
    calls = []

    class FlakyPage:
        url = "https://www.linkedin.com/feed/"

        def goto(self, url, **kw):
            calls.append(url)
            if len(calls) == 1:
                raise PlaywrightError('Page.goto: Navigation to "x" is interrupted by another navigation to "x"')

        def wait_for_load_state(self, *a, **kw):
            pass

    site.page = FlakyPage()
    assert site.is_logged_in()
    assert len(calls) == 2


LINK_LAYOUT = """
<ul>
  <li><div><a href="https://www.linkedin.com/jobs/view/4012345678/?refId=abc">
        <span>Clinical Applications Manager</span><span>Clinical Applications Manager with verification</span></a>
      <div>Trinity Health</div><div>United States (Remote)</div><div>$150K/yr - $180K/yr</div>
      <div>Promoted</div><div>Easy Apply</div></div></li>
  <li><div><a href="/jobs/view/director-of-healthcare-it-at-acme-4087654321?trk=x">Director of Healthcare IT</a>
      <div>Acme Health</div><div>Michigan, United States (Remote)</div><div>2 days ago</div></div></li>
</ul>
"""

ATTRIBUTE_LAYOUT = """
<div style="height:300px; overflow-y:auto" id="list">
  <div style="height:2000px">
    <div data-job-id="4011112222"><h3>RPA Manager</h3><p>Corewell Health</p><p>United States (Remote)</p></div>
    <div data-job-id="not-a-job"><h3>Ad</h3></div>
  </div>
</div>
"""


def search_jobs(context, cfg, html):
    context.route("https://www.linkedin.com/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    cfg.search.max_pages = 1
    site = LinkedIn(context, cfg, None, Prompter("auto"))
    return list(site.search("healthcare it manager", "United States"))


def test_linkedin_search_finds_jobs_by_link(context, cfg):
    jobs = search_jobs(context, cfg, LINK_LAYOUT)
    assert [(j.job_id, j.title, j.company, j.location) for j in jobs] == [
        ("4012345678", "Clinical Applications Manager", "Trinity Health", "United States (Remote)"),
        ("4087654321", "Director of Healthcare IT", "Acme Health", "Michigan, United States (Remote)"),
    ]
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4012345678/"


def test_linkedin_search_finds_jobs_by_attribute(context, cfg):
    jobs = search_jobs(context, cfg, ATTRIBUTE_LAYOUT)
    assert [(j.job_id, j.title, j.company) for j in jobs] == [("4011112222", "RPA Manager", "Corewell Health")]


def test_linkedin_search_saves_debug_snapshot_when_empty(context, cfg, tmp_path):
    cfg.output_dir = str(tmp_path)
    assert search_jobs(context, cfg, "<p>No matching jobs found.</p>") == []
    snapshots = sorted(p.suffix for p in (tmp_path / "debug").iterdir())
    assert snapshots == [".html", ".png"]
