"""Keep a Google Sheet of shortlisted jobs, laid out for a Google AppSheet phone app.

jobbot owns every column except Status and Notes, which are yours: it fills them in for
new rows and never overwrites them, and it reads Status back so the database knows
what you've applied to.
"""
from __future__ import annotations

import json
import logging
from datetime import date

log = logging.getLogger(__name__)

TAB = "Jobs"
HEADERS = ["Job ID", "Found", "Fit", "Title", "Company", "Location", "Apply via", "Link",
           "Why it fits", "Concerns", "Cover letter", "Status", "Notes"]
STATUSES = ["New", "Applied", "Interviewing", "Not interested"]
STATUS_COL = HEADERS.index("Status")
COVER_COL = HEADERS.index("Cover letter")
LAST_COL = chr(ord("A") + len(HEADERS) - 1)


def _row(r, cover: str) -> list:
    how = "Easy Apply" if r["status"] == "dry_run" else "Company site"
    concerns = "\n".join(f"- {c}" for c in json.loads(r["concerns"] or "[]"))
    return [r["key"], (r["updated_at"] or "")[:10] or date.today().isoformat(), r["fit_score"],
            r["title"], r["company"], r["location"], how, r["url"], r["fit_reason"], concerns, cover]


class SheetSync:
    def __init__(self, sheets, tracker, title: str = "jobbot shortlist"):
        self.api = sheets.spreadsheets()
        self.tracker = tracker
        self.title = title

    def ensure_sheet(self) -> str:
        sheet_id = self.tracker.get_state("sheet_id")
        if sheet_id:
            try:
                self.api.get(spreadsheetId=sheet_id, fields="spreadsheetId").execute()
                return sheet_id
            except Exception as e:  # deleted, or created under another Google account
                log.warning("Saved sheet not reachable (%s); creating a new one", str(e).splitlines()[0])
        created = self.api.create(body={
            "properties": {"title": self.title},
            "sheets": [{"properties": {"title": TAB, "gridProperties": {"frozenRowCount": 1}}}],
        }, fields="spreadsheetId,sheets.properties.sheetId").execute()
        sheet_id = created["spreadsheetId"]
        tab_id = created["sheets"][0]["properties"]["sheetId"]
        self.api.values().update(spreadsheetId=sheet_id, range=f"{TAB}!A1", valueInputOption="RAW",
                                 body={"values": [HEADERS]}).execute()
        self.api.batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
            "setDataValidation": {
                "range": {"sheetId": tab_id, "startRowIndex": 1,
                          "startColumnIndex": STATUS_COL, "endColumnIndex": STATUS_COL + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": s} for s in STATUSES]},
                         "showCustomUi": True, "strict": False},
            }}]}).execute()
        self.tracker.set_state("sheet_id", sheet_id)
        log.info("Created Google Sheet '%s'", self.title)
        return sheet_id

    def sync(self, rows, cover_letter=None) -> dict:
        """Add new shortlist rows, refresh jobbot's columns on existing ones, read back Status.

        cover_letter(row) -> str is called only for rows that don't have one yet.
        """
        sheet_id = self.ensure_sheet()
        existing = self.api.values().get(spreadsheetId=sheet_id,
                                         range=f"{TAB}!A2:{LAST_COL}").execute().get("values", [])
        by_key = {}
        for i, values in enumerate(existing):
            values = values + [""] * (len(HEADERS) - len(values))
            if values[0]:
                by_key[values[0]] = (i + 2, values)  # sheet row number
                if values[STATUS_COL]:
                    self.tracker.set_user_status(values[0], values[STATUS_COL])

        updates, appends = [], []
        for r in rows:
            found = by_key.get(r["key"])
            cover = found[1][COVER_COL] if found else ""
            status = found[1][STATUS_COL] if found else ""
            if not cover and cover_letter and status not in ("Not interested",):
                cover = cover_letter(r) or ""
            data = _row(r, cover)
            if found:
                updates.append({"range": f"{TAB}!B{found[0]}:K{found[0]}", "values": [data[1:]]})
            else:
                appends.append(data + ["New", ""])
        if updates:
            self.api.values().batchUpdate(spreadsheetId=sheet_id, body={
                "valueInputOption": "RAW", "data": updates}).execute()
        if appends:
            self.api.values().append(spreadsheetId=sheet_id, range=f"{TAB}!A1", valueInputOption="RAW",
                                     insertDataOption="INSERT_ROWS", body={"values": appends}).execute()
        return {"sheet_id": sheet_id, "added": len(appends), "updated": len(updates),
                "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}"}
