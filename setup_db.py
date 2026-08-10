"""One-time database setup.

    python setup_db.py                              create tables + instructor login
    python setup_db.py --migrate data/checkinout.db  ...and copy the old SQLite data

Run this once after creating your Supabase project. Run it again any time you
change INSTRUCTOR_USERNAME or INSTRUCTOR_PASSWORD in .env - that sync used to
happen on every app start, which made sense when starting the app was something
you did by hand, but not when the host restarts it hundreds of times a day.
"""
import argparse
import os
import sqlite3
import sys

from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

from db import LOCKDOWN, SCHEMA, connect  # noqa: E402


def create_schema(conn):
    conn.execute(SCHEMA)
    conn.commit()
    print("Schema ready (tables, indexes, one-open-visit constraint).")


def lock_down_public_access(conn):
    """Take away the access Supabase's public REST API is granted by default."""
    conn.execute(LOCKDOWN)
    conn.commit()
    print("Locked down: student data is not reachable from Supabase's public API.")


def sync_instructor_account(conn):
    """Make the database match the credentials in .env.

    .env is the single source of truth: the configured account is created (or
    its password updated if it changed), and any other instructor account is
    removed, so there is only ever one way into the dashboard.
    """
    username = (os.environ.get("INSTRUCTOR_USERNAME") or "").strip()
    password = os.environ.get("INSTRUCTOR_PASSWORD") or ""

    if not username or not password:
        print("INSTRUCTOR_USERNAME/PASSWORD not set - leaving the instructors table alone.")
        return

    row = conn.execute(
        "SELECT id, password_hash FROM instructors WHERE username = %s", (username,)
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO instructors (username, password_hash) VALUES (%s, %s)",
            (username, generate_password_hash(password)),
        )
        print(f"Created instructor account '{username}'.")
    elif not check_password_hash(row["password_hash"], password):
        conn.execute(
            "UPDATE instructors SET password_hash = %s WHERE id = %s",
            (generate_password_hash(password), row["id"]),
        )
        print(f"Updated password for '{username}'.")
    else:
        print(f"Instructor account '{username}' already up to date.")

    cur = conn.execute("DELETE FROM instructors WHERE username != %s", (username,))
    if cur.rowcount:
        print(f"Removed {cur.rowcount} other instructor account(s).")
    conn.commit()


def migrate_from_sqlite(conn, sqlite_path):
    """Copy students and visits out of the old SQLite file.

    Email addresses stay encrypted the whole way across - the stored bytes are
    copied as-is, so nothing is decrypted in the process and the same
    EMAIL_ENCRYPTION_KEY keeps working. Students are matched by name, so
    re-running this updates rather than duplicating.
    """
    if not os.path.isfile(sqlite_path):
        sys.exit(f"No SQLite database at {sqlite_path}")

    old = sqlite3.connect(sqlite_path)
    old.row_factory = sqlite3.Row

    students = old.execute("SELECT id, name, email_enc, active FROM students").fetchall()
    added = updated = 0
    id_map = {}

    for s in students:
        existing = conn.execute(
            "SELECT id FROM students WHERE name = %s", (s["name"],)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE students SET email_enc = %s, active = %s WHERE id = %s",
                (bytes(s["email_enc"] or b""), s["active"], existing["id"]),
            )
            id_map[s["id"]] = existing["id"]
            updated += 1
        else:
            new = conn.execute(
                "INSERT INTO students (name, email_enc, active) VALUES (%s, %s, %s) "
                "RETURNING id",
                (s["name"], bytes(s["email_enc"] or b""), s["active"]),
            ).fetchone()
            id_map[s["id"]] = new["id"]
            added += 1

    # Visit ids are reassigned by Postgres, so student_id has to be remapped
    # rather than copied straight across.
    visits = old.execute(
        "SELECT student_id, check_in_time, check_out_time, email_status FROM visits"
    ).fetchall()
    copied_visits = 0
    for v in visits:
        new_student_id = id_map.get(v["student_id"])
        if new_student_id is None:
            continue  # visit for a student that no longer exists
        conn.execute(
            "INSERT INTO visits (student_id, check_in_time, check_out_time, email_status) "
            "VALUES (%s, %s, %s, %s)",
            (new_student_id, v["check_in_time"], v["check_out_time"], v["email_status"]),
        )
        copied_visits += 1

    conn.commit()
    old.close()
    print(f"Migrated students: {added} added, {updated} updated. Visits copied: {copied_visits}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--migrate", metavar="SQLITE_PATH", nargs="?",
                        const="data/checkinout.db", default=None,
                        help="Copy students and visits from an old SQLite database.")
    args = parser.parse_args()

    with connect() as conn:
        create_schema(conn)
        lock_down_public_access(conn)
        if args.migrate:
            migrate_from_sqlite(conn, args.migrate)
        sync_instructor_account(conn)

        count = conn.execute("SELECT count(*) AS n FROM students WHERE active = 1").fetchone()["n"]
        print(f"\nDone. {count} active student(s) in the database.")


if __name__ == "__main__":
    main()
