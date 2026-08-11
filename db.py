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
    active      INTEGER NOT NULL DEFAULT 1,
    -- The 4-digit code the student types at the kiosk. TEXT rather than a
    -- number so "0042" keeps its leading zeros - as an integer it would come
    -- back as 42 and never match what is printed on the instructor's list.
    code        TEXT
);

-- A visit now has three stamps, not two: the parent checks the student in, the
-- student says when their work is done (which is what emails the parent), and
-- the parent checks them out on collection. A visit is "open" until
-- check_out_time is set, exactly as before.
CREATE TABLE IF NOT EXISTS visits (
    id              SERIAL PRIMARY KEY,
    student_id      INTEGER NOT NULL REFERENCES students(id),
    check_in_time   TEXT NOT NULL,
    work_done_time  TEXT,
    check_out_time  TEXT,
    email_status    TEXT,
    -- Set when the nightly job closed this visit because nobody did. Its own
    -- column because email_status can no longer carry it: the email is sent at
    -- "done with work", so a student can be emailed about *and* forgotten
    -- about, and both facts have to survive.
    auto_closed     INTEGER NOT NULL DEFAULT 0
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

-- Wrong 4-digit codes, throttled the same way and for the same reason. Four
-- digits is ten thousand guesses, which a bored sibling can work through in an
-- afternoon; five wrong tries in fifteen minutes stops that being worth doing.
CREATE TABLE IF NOT EXISTS code_attempts (
    id            SERIAL PRIMARY KEY,
    student_id    INTEGER NOT NULL REFERENCES students(id),
    attempted_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_code_attempts ON code_attempts(student_id, attempted_at);

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


# Changes to tables that already exist. SCHEMA above only ever runs CREATE TABLE
# IF NOT EXISTS, which is a no-op on a database that already has the table - so
# it will happily leave an old `students` table without its `code` column and
# report success. This block is what actually brings an existing deployment
# forward, and every statement in it is written to be safe to run again.
#
# Runs after SCHEMA, so on a brand-new database the columns are already there
# and every ALTER here is a no-op.
MIGRATIONS = """
ALTER TABLE students ADD COLUMN IF NOT EXISTS code TEXT;

ALTER TABLE visits ADD COLUMN IF NOT EXISTS work_done_time TEXT;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS auto_closed INTEGER NOT NULL DEFAULT 0;

-- "Forgot to check out" used to be recorded by writing 'auto_closed' into
-- email_status, which conflated two unrelated things. Move the old rows onto
-- the new column and give email_status back its one job. Matching on the old
-- value makes this self-limiting: after the first run nothing matches.
UPDATE visits SET auto_closed = 1, email_status = NULL WHERE email_status = 'auto_closed';

-- Created here rather than in SCHEMA because the column it indexes may have
-- only just been added by the ALTER above.
--
-- Postgres allows any number of NULLs in a unique index, which is what makes
-- the backfill possible: existing students start with no code, all of them
-- "distinct" from each other, and get filled in one at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_students_code ON students(code);
"""


# Shut the front door Supabase leaves open.
#
# Supabase runs an automatic REST API over the public schema, reachable with the
# project's "anon" key - a key designed to be published in browser code. New
# tables are granted to that role by default and start with Row Level Security
# switched off, so out of the box every student name and visit is readable, and
# writable, by anyone holding a key that is meant to be public.
#
# This app never uses that API; it connects straight to Postgres as the owning
# role. So the safe thing is to take the API's access away entirely: revoke the
# grants, revoke the default that would re-grant them on the next new table, and
# turn RLS on as a second layer that denies by default because no policy allows
# anything. The app is unaffected - its role owns these tables and bypasses RLS.
#
# Wrapped in a role check so this still runs against a plain Postgres (a local
# one, say), where "anon" and "authenticated" don't exist.
LOCKDOWN = """
DO $$
DECLARE
    web_role text;
BEGIN
    FOREACH web_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = web_role) THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', web_role);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', web_role);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I',
                web_role);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I',
                web_role);
        END IF;
    END LOOP;
END $$;

-- Every table in the schema, found rather than listed.
--
-- This used to be a hand-written list, which meant the second layer of defence
-- silently didn't cover whatever was added last. It had already drifted: the
-- legacy `reports` table, left over from when PDFs were stored on disk, had RLS
-- switched off in production. Enumerating pg_tables means the next table added
-- to SCHEMA is covered the first time setup_db.py runs, without anyone
-- remembering to come back here.
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    END LOOP;
END $$;
"""


def init_db(app):
    """Wire up connection teardown for the app.

    Creating tables is setup_db.py's job now. This used to run the schema on
    every start, which was free when "every start" meant "when I run python
    app.py". On Vercel it would mean issuing DDL on every cold start - hundreds
    of times a day - to look for tables that have existed for months.
    """
    app.teardown_appcontext(close_db)
