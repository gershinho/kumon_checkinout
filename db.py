"""Database access, backed by Postgres (Supabase).

Connections go through Supabase's transaction pooler, which is why prepared
statements are turned off below: the pooler hands each statement to whichever
backend connection is free, so a statement prepared on one physical connection
usually isn't there for the next one. psycopg auto-prepares after a few repeats,
which would start failing under load rather than during testing - the worst kind
of bug to find in production.
"""
import os

import psycopg
from psycopg.rows import dict_row
from flask import g


def get_database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. In Supabase: Connect -> 'Direct connection "
            "string' -> Transaction pooler (port 6543), then put it in .env."
        )
    return url


def connect():
    """A new connection to the database. Used by the app and by setup_db.py."""
    return psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
        prepare_threshold=None,
    )


def get_db():
    """Get a request-scoped connection."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Timestamps are stored as naive local-time ISO text ("2026-08-10T16:05:00"),
# the same as before. local_now() returns a naive datetime, so there is no
# timezone offset in the string for Postgres to shift when casting to a date.
SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email_enc   BYTEA NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS visits (
    id              SERIAL PRIMARY KEY,
    student_id      INTEGER NOT NULL REFERENCES students(id),
    check_in_time   TEXT NOT NULL,
    check_out_time  TEXT,
    email_status    TEXT
);

CREATE TABLE IF NOT EXISTS instructors (
    id             SERIAL PRIMARY KEY,
    username       TEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL
);

-- Failed instructor logins, so a public URL can't be brute-forced. A table
-- rather than a counter in memory: each request may run in a different
-- serverless instance, so anything held in process memory is forgotten between
-- attempts and would count every try as the first.
CREATE TABLE IF NOT EXISTS login_attempts (
    id            SERIAL PRIMARY KEY,
    ip            TEXT NOT NULL,
    attempted_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(ip, attempted_at);

CREATE INDEX IF NOT EXISTS idx_visits_student ON visits(student_id);
CREATE INDEX IF NOT EXISTS idx_visits_checkin ON visits(check_in_time);

-- At most one open visit per student, enforced by the database itself.
--
-- The check-in query guards against double-tapping with "INSERT ... WHERE NOT
-- EXISTS". Under SQLite that was airtight, because SQLite serialises writers.
-- Postgres runs writers concurrently, so two simultaneous taps can both find no
-- open visit and both insert. This index makes the second one fail instead, and
-- api_checkin turns that failure into the same "already checked in" message.
CREATE UNIQUE INDEX IF NOT EXISTS idx_visits_one_open
    ON visits(student_id) WHERE check_out_time IS NULL;
"""


def init_db(app):
    """Wire up connection teardown for the app.

    Creating tables is setup_db.py's job now. This used to run the schema on
    every start, which was free when "every start" meant "when I run python
    app.py". On Vercel it would mean issuing DDL on every cold start - hundreds
    of times a day - to look for tables that have existed for months.
    """
    app.teardown_appcontext(close_db)
