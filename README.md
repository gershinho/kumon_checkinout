# Kumon Check-In / Check-Out System

A simple, self-hosted Flask app for a Kumon center: students tap their name on a
tablet/kiosk to check in and check out, parents get an email the instant a
child checks out, and the instructor gets a private dashboard plus an automatic
daily PDF report.

## What it does

- **Student kiosk** (`/`) — big buttons, and students don't log in. The tablet
  itself is unlocked once by the instructor at the start of a session and stays
  unlocked all day. Tapping the name box drops down the student list; it filters
  as the student types, and they tap their name, then **Check In** or
  **Check Out**.
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

- **Two doors, one key.** Nothing is visible to the public — not the check-in
  screen, not the student list behind it. Both the kiosk and the dashboard are
  opened with the same username and password, on the same login page, but they
  are unlocked separately: signing in at the kiosk unlocks *only* the check-in
  screen. Getting into the dashboard means entering the password again, even on
  a tablet that's already unlocked. So a tablet sitting in the lobby all session
  is only ever a check-in screen — a curious student who types in the dashboard
  address is asked for the password, same as anyone on the internet.
- Unlocking lasts twelve hours of inactivity and refreshes with use, so the
  tablet is unlocked once per session rather than once per student.
- Parent email addresses are **encrypted at rest** (Fernet/AES) using a key you
  control (`EMAIL_ENCRYPTION_KEY`). They are only decrypted in memory, right when
  an email needs to be sent or shown (masked) on the dashboard.
- The dashboard never shows a full address — only `p****@example.com`.
- Each parent gets their own message; addresses are never exposed to each other.
- The instructor password is stored as a salted hash, never in plain text, and
  exactly one instructor account exists at a time - the one in `.env`.
- Repeated wrong passwords are throttled: ten failures from the same address
  within fifteen minutes and that address is locked out for the rest of the
  window, so the login page can't be guessed at from a public URL. A correct
  password clears the count.
- A dashboard session expires after twelve hours, and every form on the site
  carries a one-time token, so another site can't post to the dashboard using a
  logged-in instructor's browser.
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
pip install -r requirements-dev.txt

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
   is the single source of truth, applied by `python setup_db.py`: change the
   password here, re-run that command, and the old one stops working
   immediately. Any other instructor account is removed, so there's only ever
   one way in. (If both are left blank, the existing account is left untouched
   rather than locking you out.) Pick a long one — this is the only thing
   standing between the internet and the dashboard.
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
   the dashboard marks them "Not configured" and a line is written to the
   server log instead of an email being sent, so you can test the whole flow
   first. Parent addresses are never written to the log.

## Create the tables

Once, after filling in `.env`:

```bash
python setup_db.py
```

This creates the tables and your instructor login. **Re-run it any time you
change `INSTRUCTOR_USERNAME` or `INSTRUCTOR_PASSWORD` in `.env`**, and any time
this project adds a table — that sync used to happen every time the app started,
which stops being reasonable once a host restarts the app hundreds of times a
day. It's safe to run as often as you like; existing data is left alone.

To point it at your deployed database rather than a local one, put the
production `DATABASE_URL` in `.env` and run it from your own machine — the app
never creates tables by itself.

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

That's the development server — fine for trying things out on your own machine.
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

`vercel.json`, `.python-version`, and `api/index.py` are already set up. Push the
repo to GitHub, import it in Vercel, and set **every variable from your `.env`**
in Project Settings → Environment Variables — especially `DATABASE_URL`,
`EMAIL_ENCRYPTION_KEY`, `SECRET_KEY`, `REPORT_TIMEZONE`, and the `INSTRUCTOR_*`
pair. Then run `python setup_db.py` once from your own machine, pointed at the
production `DATABASE_URL`, to create the tables.

`SECRET_KEY` is not optional in production: without it the app refuses to start
rather than fall back to a placeholder that anyone reading this repository could
use to forge an instructor session.

The cron entry in `vercel.json` calls `/api/cron/nightly-close` daily at
`05:00 UTC` — midnight Central in summer, 11 PM in winter. Vercel provides
`CRON_SECRET` automatically; the endpoint refuses any request without it.

An hour earlier would also land after the 10 PM close, but in winter it would
land *exactly* on it, and "which session just ended" is decided by comparing
against closing time — so a couple of seconds of clock skew either way would
attribute the night to the wrong day. `05:00` keeps an hour of clearance in both
directions, in both seasons, even on a Hobby plan where a job can start up to an
hour late.

Running late doesn't matter. Visits are stamped with the closing time from their
own check-in date, not from when the job runs, and the report covers the session
that has just finished rather than whatever "today" happens to be when the job
fires — so a run that slips past local midnight still reports on the right day.
Running twice doesn't matter either: the second run finds nothing open.

Two things worth knowing about the Vercel environment specifically:

- Its Python image has no timezone database of its own, so `tzdata` is listed in
  `requirements.txt`. Without it `REPORT_TIMEZONE` would silently do nothing and
  every time would be recorded in UTC. If that ever happens the server log says
  so explicitly.
- `vercel.json` allows each request 30 seconds; the check-out email gives up
  after 10. The email is sent while the student waits, so the mail server has to
  be the thing that gives up first — otherwise a slow send would fail the whole
  request for a check-out that actually worked.

**On the tablet:** browse to the site once at the start of a session, enter the
instructor username and password, and leave it on the check-in screen. Students
tap their own names from there; they never see a password prompt. If the tablet
has been idle long enough for the session to lapse, it returns to the login page
by itself rather than showing an error.

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
app.py                  Flask routes (kiosk, dashboard, API, nightly cron endpoint)
db.py                    Postgres schema + connection handling
setup_db.py               One-time setup: create tables, instructor login, migration
api/index.py               Vercel entry point (imports app.py)
vercel.json                 Vercel routing, request time limit, nightly cron schedule
.python-version              Python version Vercel builds with
requirements.txt              What the deployed app needs
requirements-dev.txt           ...plus the tools that only run on your own machine
time_utils.py                Timezone, 12-hour formatting, closing time, report days
crypto_utils.py               Email address encryption/decryption
email_utils.py                 Check-out email sending (with safe log fallback)
pdf_report.py                  Daily PDF report generation
import_students.py              Roster importer (reads name + parent email only)
templates/                        checkin.html, login.html, dashboard.html
static/style.css                    Kumon-blue styling
                                     (nothing is written to disk — reports are built
                                     in memory, failed emails are logged to stdout)
```
