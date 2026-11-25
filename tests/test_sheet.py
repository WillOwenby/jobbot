from jobbot.models import ApplyResult, Job, Status
from jobbot.sheet import HEADERS, STATUS_COL, SheetSync
from jobbot.tracker import Tracker


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeSheets:
    """Just enough of the Sheets v4 API: one spreadsheet held as a list of rows."""

    def __init__(self):
        self.books = {}
        self.calls = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def create(self, body, fields):
        sid = f"sheet{len(self.books) + 1}"
        self.books[sid] = []
        return _Call(lambda: {"spreadsheetId": sid, "sheets": [{"properties": {"sheetId": 0}}]})

    def get(self, spreadsheetId, range=None, fields=None):
        def run():
            if spreadsheetId not in self.books:
                raise RuntimeError("404 not found")
            return {"spreadsheetId": spreadsheetId} if range is None else {"values": self.books[spreadsheetId][1:]}
        return _Call(run)

    def batchUpdate(self, spreadsheetId, body):
        self.calls.append(("batchUpdate", body))
        def run():
            for d in body.get("data", []):  # values().batchUpdate: "Jobs!B3:K3"
                row = int(d["range"].split("!B")[1].split(":")[0]) - 1
                self.books[spreadsheetId][row][1:11] = d["values"][0]
            return {}
        return _Call(run)

    def update(self, spreadsheetId, range, valueInputOption, body):
        return _Call(lambda: self.books[spreadsheetId].append(list(body["values"][0])) or {})

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        return _Call(lambda: self.books[spreadsheetId].extend(list(v) for v in body["values"]) or {})


def _add(tracker, job_id, status=Status.DRY_RUN, fit=80):
    job = Job("linkedin", job_id, f"Director {job_id}", "Acme", "Remote", f"https://x/{job_id}",
              description="A long description " * 20, fit_score=fit, fit_reason="good fit",
              concerns=["travel unclear"])
    tracker.record(job, ApplyResult(status, ""))
    return job


def test_creates_sheet_adds_rows_and_writes_letters(tmp_path):
    tracker = Tracker(str(tmp_path / "db"))
    _add(tracker, "1")
    _add(tracker, "2", Status.APPLY_YOURSELF, 70)
    api = FakeSheets()
    written = []
    result = SheetSync(api, tracker).sync(tracker.shortlist(),
                                          lambda r: written.append(r["key"]) or f"Letter for {r['key']}")
    rows = api.books[result["sheet_id"]]
    assert rows[0] == HEADERS
    assert [r[0] for r in rows[1:]] == ["linkedin:1", "linkedin:2"]
    assert rows[1][6] == "Easy Apply" and rows[2][6] == "Company site"
    assert rows[1][9] == "- travel unclear"
    assert rows[1][STATUS_COL] == "New"
    assert rows[1][10] == "Letter for linkedin:1"
    assert result["added"] == 2 and tracker.get_state("sheet_id") == result["sheet_id"]
    assert tracker.description("linkedin:1").startswith("A long description")


def test_second_sync_keeps_your_status_notes_and_letter(tmp_path):
    tracker = Tracker(str(tmp_path / "db"))
    job = _add(tracker, "1")
    api = FakeSheets()
    sync = SheetSync(api, tracker)
    sid = sync.sync(tracker.shortlist(), lambda r: "first letter")["sheet_id"]
    row = api.books[sid][1]
    row[STATUS_COL], row[STATUS_COL + 1] = "Applied", "emailed recruiter"
    _add(tracker, "1", fit=90)  # re-scored
    result = sync.sync(tracker.shortlist(), lambda r: "should not be called")
    row = api.books[sid][1]
    assert result == {**result, "added": 0, "updated": 1}
    assert row[2] == 90 and row[10] == "first letter"
    assert row[STATUS_COL:] == ["Applied", "emailed recruiter"]
    # Status flows back: jobbot won't apply to it again.
    assert tracker.user_status("linkedin:1") == "Applied"
    assert tracker.is_done(job, dry_run=True)


def test_recreates_sheet_if_deleted(tmp_path):
    tracker = Tracker(str(tmp_path / "db"))
    _add(tracker, "1")
    tracker.set_state("sheet_id", "gone")
    api = FakeSheets()
    result = SheetSync(api, tracker).sync(tracker.shortlist())
    assert result["sheet_id"] != "gone" and result["added"] == 1


def test_no_letter_for_not_interested(tmp_path):
    tracker = Tracker(str(tmp_path / "db"))
    _add(tracker, "1")
    api = FakeSheets()
    sync = SheetSync(api, tracker)
    sid = sync.sync(tracker.shortlist())["sheet_id"]
    api.books[sid][1][STATUS_COL] = "Not interested"
    calls = []
    sync.sync(tracker.shortlist(), lambda r: calls.append(r) or "x")
    assert calls == []
