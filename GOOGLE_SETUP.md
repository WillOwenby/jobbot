# Phone app setup (Google Sheet + AppSheet)

jobbot copies your shortlist to a Google Sheet. A free Google AppSheet app turns that
sheet into a phone app, so you can review matches, open links, copy cover letters
and mark jobs as Applied while your PC is off.

You do this once. It takes about 20 minutes.

## What jobbot can access

jobbot asks Google for only two permissions:

| Permission | What it allows | What it can't do |
|---|---|---|
| Gmail, read only | Read your job-alert emails (used later) | Send, delete or change email |
| Drive, files it created | Create and edit the "jobbot shortlist" sheet | See any of your other files |

- The sign-in is saved in `google-token.json` on your PC only. It is never in git.
- You can cut access off at any time: myaccount.google.com → Security → Third-party connections → jobbot → Remove access.

## Part 1: Google Cloud project (about 10 minutes, free)

1. Go to https://console.cloud.google.com and sign in with the Gmail account that gets your job alerts.
2. At the top, click the project picker, then **New project**. Name it `jobbot` and click **Create**. Then select it.
3. Turn on the two APIs:
   - Search the top bar for **Gmail API**, open it and click **Enable**.
   - Search for **Google Sheets API** and click **Enable**.
4. Set up the consent screen. Go to **APIs & Services → OAuth consent screen** (it may be called **Google Auth Platform**) and click **Get started**.
   - App name: `jobbot`. Support email: your own.
   - Audience: **External**.
   - Contact email: your own. Agree, then click **Create**.
5. Add yourself as a test user. Go to **Audience → Test users → Add users**, enter your Gmail address and save.
   - Leave the app in **Testing**. Don't publish it: testing mode means only you can use it.
6. Create the client. Go to **Clients** (or **Credentials → Create credentials → OAuth client ID**).
   - Application type: **Desktop app**. Name: `jobbot`. Click **Create**.
7. Click **Download JSON**. Rename the file to `google-client-secret.json` and move it into `C:\Users\owenb\Documents\jobbot`.

## Part 2: Sign in and create the sheet (on your PC)

Run these in PowerShell:

```powershell
cd C:\Users\owenb\Documents\jobbot
git pull
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m jobbot google-login
```

1. A browser opens. Pick your account.
2. Google warns **"Google hasn't verified this app."** That's expected, because it's your own app. Click **Continue**.
3. Tick both permissions and click **Continue**.

Then create the sheet:

```powershell
python -m jobbot sheet
```

This prints the sheet's link. Cover letters are written only for jobs found by runs from now on, because older runs didn't save the job description. Use `--no-letters` to skip them.

## Part 3: The AppSheet app (about 10 minutes, free)

1. Open the sheet link. In the menu, choose **Extensions → AppSheet → Create an app**. AppSheet builds a starter app from the sheet.
2. In the AppSheet editor, go to **Data → Jobs → Columns** and set:
   - **Job ID**: tick **Key**.
   - **Link**: type **Url**.
   - **Fit**: type **Number**.
   - **Status**: type **Enum**, values `New`, `Applied`, `Interviewing`, `Not interested`. Tick **Editable**.
   - **Notes**: type **LongText**. Tick **Editable**.
   - **Why it fits**, **Concerns**, **Cover letter**: type **LongText**.
   - Every other column: untick **Editable**. jobbot owns those columns and overwrites them.
3. Go to **Views**. Edit the main view:
   - Type: **deck**. Sort by **Fit**, descending.
   - Group by **Status**, so New jobs sit together.
4. Optional: add a second view with a slice where `[Status] = "New"`. Name it **To review**.
5. Click **Save**. Install **AppSheet** from the App Store or Google Play, sign in with the same Google account, and open your app.

## Day to day

- Running `python -m jobbot sheet` after a run adds the new matches to the sheet. It refreshes jobbot's own columns.
- It never changes your **Status** or **Notes**.
- It reads your Status back. Jobs marked Applied, Interviewing or Not interested are never applied to again.
- Coming next: `python -m jobbot inbox` reads your LinkedIn and Indeed alert emails, so runs don't need to search LinkedIn at all.
