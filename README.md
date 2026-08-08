# Kumon Check-In / Check-Out System

A simple, self-hosted Flask app for a Kumon center: students tap their name on a
tablet/kiosk to check in and check out, parents get an email the instant a
child checks out, and the instructor gets a private dashboard plus an automatic
daily PDF report.

## What it does

- **Student kiosk** (`/`) — big buttons, no login needed. Tapping the name box
  drops down the student list; it filters as the student types, and they tap
  their name, then **Check In** or **Check Out**.
- **Instant parent email** — the moment a student checks out, an email goes to the
  parent address on file ("Kumon: Aadhya checked out at 4:32 PM").
- **Instructor dashboard** (`/login` → `/dashboard`) — password protected. Shows
  every check-in/checkout for a chosen day, whether the parent email succeeded,
  lets you generate/download a PDF report for any day, and lets you add or
  add, remove, and update students right from the page — including changing a
  parent's email address when it changes, so no spreadsheet re-import is needed
  for day-to-day roster changes.
- **Nightly reset after closing time** — the center runs 2 PM–10 PM. Every
  night, anyone who forgot to check out is closed out automatically (stamped
  10:00 PM, no email sent to the parent), so nobody is left stuck "checked in"
  and blocked from checking in next session.
- **PDF reports, built on demand** — nothing is stored on the server. Every
  visit is in the database, so a report can be rebuilt for **any date, any
  time** from the dashboard. On session days (**Mondays and Thursdays**) the
  report is also emailed to `REPORT_EMAIL` at closing time, so there's a
  permanent copy in the instructor's inbox without the server keeping one.
- **All times are shown as normal 12-hour clock times** (4:05 PM), never
  military time — on the kiosk, the dashboard, the PDF, and the parent email.

## Security & privacy

- Parent email addresses are **encrypted at rest** (Fernet/AES) using a key you
  control (`EMAIL_ENCRYPTION_KEY`). They are only decrypted in memory, right when
  an email needs to be sent or shown (masked) on the dashboard.
- The dashboard never shows a full address — only `p****@example.com`.
- Each parent gets their own message; addresses are never exposed to each other.
- The instructor password is stored as a salted hash, never in plain text, and
  exactly one instructor account exists at a time - the one in `.env`.
- Student records live in a Postgres database (Supabase), reachable only with
  the `DATABASE_URL` password in `.env`. Because addresses are encrypted before
  they are written, they stay unreadable in the database itself - the
  `EMAIL_ENCRYPTION_KEY` never leaves your server.
- Apart from the database connection and the outgoing check-out email, no
  student data leaves your server.
- Only two fields are imported from your school's export: **name** and one
  **parent email**. Every other column (address, DOB, phone numbers, etc.) is
  ignored and never touches the database.

## One-time setup

```bash
python3 -m venv venv
source venv/bin/activate          # on Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

Now edit `.env`:

1. **SECRET_KEY** — generate one:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. **EMAIL_ENCRYPTION_KEY** — generate one and **save it somewhere safe** (a
   password manager, not git). If you lose it, stored email addresses can't be
   decrypted:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
3. **INSTRUCTOR_USERNAME / INSTRUCTOR_PASSWORD** — your dashboard login. `.env`
   is the single source of truth: every time the app starts it makes the
   database match these values. Change the password here and restart, and the
   old one stops working immediately. Any other instructor account is removed,
   so there's only ever one way in. (If both are left blank, the existing
   account is left untouched rather than locking you out.)
4. **DATABASE_URL** — in Supabase, click **Connect** → **Direct connection
   string** → **Transaction pooler**. It must be the *pooler* string (host
   contains `pooler.supabase.com`, port `6543`), not the direct one on `5432`.
   Each copy of the app opens its own connection, and the direct connection
   runs out of them.
5. **Email sending** (optional at first) — fill in `SMTP_HOST`, `SMTP_PORT`,
   `SMTP_USERNAME`, `SMTP_PASSWORD`, and `FROM_EMAIL`. `FROM_EMAIL` is the
   address parents will see the notification come from. For Gmail, use
   `smtp.gmail.com` port `587` with an **App Password** (Google blocks your
   normal password for SMTP). Until this is filled in, checkouts still work —
   messages are written to `data/email_log.txt` instead of being sent, so you
   can test the whole flow first.

## Create the tables

Once, after filling in `.env`:

```bash
python setup_db.py
```

This creates the tables and your instructor login. **Re-run it any time you
change `INSTRUCTOR_USERNAME` or `INSTRUCTOR_PASSWORD` in `.env`** — that sync
used to happen every time the app started, which stops being reasonable once a
host restarts the app hundreds of times a day.

Coming from the old SQLite version? Copy your existing roster across with:

```bash
python setup_db.py --migrate data/checkinout.db
```

Email addresses stay encrypted the whole way over, so the same
`EMAIL_ENCRYPTION_KEY` keeps working.

## Import your students

Export your roster from your school software (name + a parent email column are
all that's needed) and run:

```bash
python import_students.py "Export_Student_16001514_20260808_070535.xlsx"
```

No single email column in the export covers every student, so the importer tries
**Mother Email**, then **Father Email**, then the student's own **Email**, then
**Other Email**, and uses the first one it finds.

Add `--dry-run` first if you want to preview what would happen without saving
anything. Re-run the same command any time your roster changes — it updates
existing students and adds new ones without duplicating anyone.

## Run it

```bash
python app.py
```

Visit `http://localhost:5000/` for the student kiosk on your tablet, and
`http://localhost:5000/login` for the instructor dashboard.

## Running it for real (always-on, on your own network)

For day-to-day use, run it with a production server instead of the built-in
dev server:

```bash
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

Then point the tablet's browser at `http://<your-computer's-local-IP>:8000/`.

Note that the nightly close-out doesn't run by itself here — it's triggered by
an HTTP request now (see below), so on a local server you'd want a `cron` entry
hitting `/api/cron/nightly-close` with the `CRON_SECRET` header.

## Deploying to Vercel

`vercel.json` and `api/index.py` are already set up. Push the repo to GitHub,
import it in Vercel, and set **every variable from your `.env`** in Project
Settings → Environment Variables — especially `DATABASE_URL`,
`EMAIL_ENCRYPTION_KEY`, `SECRET_KEY`, and the `INSTRUCTOR_*` pair.

The cron entry in `vercel.json` calls `/api/cron/nightly-close` daily at
`05:00 UTC` (midnight Central in summer, 11 PM in winter — comfortably after the
center closes). Vercel provides `CRON_SECRET` automatically; the endpoint
refuses any request without it.

Running late doesn't matter: visits are stamped with the closing time from their
own check-in date, not from when the job runs, so daylight saving can't change
the recorded data. Running twice doesn't matter either — the second run finds
nothing open.

**Before you go live:** the kiosk at `/` and the roster at `/api/students` have
no login, which is correct on a private network but not on a public URL — anyone
with the address would see every student's name and be able to check them out.
Turn on Vercel's **Deployment Protection**, or add a kiosk password, first.

## Changing the schedule

Set in `.env`:

- `REPORT_TIMEZONE` — the center's timezone (e.g. `America/Chicago`). All
  timestamps and the nightly job follow this, so the app stays correct even if
  the server's own clock is set elsewhere.
- `CENTER_CLOSE_HOUR` / `CENTER_CLOSE_MINUTE` — closing time, default `22:00`
  (10 PM). This is when the day resets and forgotten check-outs are closed.
- `REPORT_DAYS` — the days the center runs, default `mon,thu`. Comma separated,
  e.g. `mon,wed,fri`. These are the nights a report is emailed, and the dates
  offered on the dashboard.
- `REPORT_EMAIL` — where the nightly report is emailed. Leave blank and none is
  sent; you can still download any date from the dashboard.

The reset runs *every* night, even on non-session days, so a stray check-in
never carries over. Only the emailed report is limited to `REPORT_DAYS`.

## Project layout

```
app.py               Flask routes (kiosk, dashboard, API, nightly cron endpoint)
db.py                 Postgres schema + connection handling
setup_db.py            One-time setup: create tables, instructor login, migration
api/index.py            Vercel entry point (imports app.py)
vercel.json              Vercel routing + nightly cron schedule
time_utils.py          Timezone, 12-hour formatting, closing time, report days
crypto_utils.py         Email address encryption/decryption
email_utils.py           Check-out email sending (with safe log fallback)
pdf_report.py            Daily PDF report generation
import_students.py        Roster importer (reads name + parent email only)
templates/                  checkin.html, login.html, dashboard.html
static/style.css              Kumon-blue styling
                               (nothing is written to disk — reports are built
                               in memory, failed emails are logged to stdout)
```
