"""One Google sign-in for jobbot, limited to:

- gmail.readonly: read email (used only to fetch job-alert emails); can't send, delete or change
- drive.file:     create and edit only the files jobbot itself creates (the shortlist sheet)

The sign-in is saved in google-token.json next to config.yaml. Revoke it any time at
myaccount.google.com > Security > Third-party connections.
"""
from __future__ import annotations

from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


class GoogleNotConnected(RuntimeError):
    pass


def credentials(client_secret_file: str, token_file: str, interactive: bool = False):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token = Path(token_file)
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError:
            creds = None  # revoked or expired for good; sign in again
    if not interactive:
        raise GoogleNotConnected("Not signed in to Google. Run: python -m jobbot google-login")
    secret = Path(client_secret_file)
    if not secret.exists():
        raise GoogleNotConnected(f"{secret.name} not found. Follow GOOGLE_SETUP.md to create it.")

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True,
                                  authorization_prompt_message="Opening Google sign-in in your browser...")
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(name: str, version: str, creds):
    from googleapiclient.discovery import build
    return build(name, version, credentials=creds, cache_discovery=False)
