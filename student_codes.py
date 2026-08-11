"""The 4-digit code each student types to check in, finish, and check out.

Kept in one place because three different things create students - the
dashboard's Add Student form, import_students.py, and the backfill in
setup_db.py - and a student without a code cannot use the kiosk at all.

Why four digits, and what that is and isn't worth: this stops a student tapping
someone else's name, by accident or as a joke. It is not a password and it does
not protect anything sensitive - parent email addresses live behind the
instructor login, not behind this. Ten thousand combinations is thin on its own,
so app.py throttles wrong guesses; the two together are what make it hold up.
"""
import secrets

import psycopg

CODE_DIGITS = 4
CODE_MAX = 10 ** CODE_DIGITS


def random_code() -> str:
    """A random code, zero-padded. secrets rather than random: the sequence
    shouldn't be predictable from the codes already handed out."""
    return f"{secrets.randbelow(CODE_MAX):0{CODE_DIGITS}d}"


def unused_code(db, attempts: int = 100) -> str:
    """A code no active or inactive student currently holds.

    Deactivated students are included in the check on purpose. They keep their
    code so that re-adding someone gives them back the code they already know,
    which means handing it to a new student would put two people on one code.
    """
    for _ in range(attempts):
        code = random_code()
        taken = db.execute("SELECT 1 FROM students WHERE code = %s", (code,)).fetchone()
        if taken is None:
            return code
    # Only reachable with thousands of students, at which point four digits is
    # the wrong size and someone should hear about it rather than get a
    # duplicate.
    raise RuntimeError(
        f"Couldn't find a free {CODE_DIGITS}-digit code after {attempts} tries. "
        "There may be too many students for codes this short."
    )


def with_new_code(db, write, attempts: int = 10):
    """Run `write(code)` with a fresh code, retrying if that code is taken.

    unused_code() checks before writing, but checking and then inserting is two
    steps, and two students added at the same instant can both pass the check.
    The unique index on students.code is what actually prevents the duplicate;
    this turns losing that race into another attempt instead of an error page.

    Returns whatever `write` returns, with the code that was used.

    A failed statement poisons the whole transaction in Postgres, so something
    has to undo it before the next attempt's SELECT can run. That something is a
    SAVEPOINT, NOT db.rollback().
    
    This used to call db.rollback(), which is not a statement-level undo - it
    throws away everything the caller has written since its last commit, and
    then this function returned normally so the caller never knew. In
    import_students.py, where the whole roster is updated in one transaction and
    committed at the end, a single code collision silently discarded every
    parent-email update made before it, and the script still printed "Imported:
    40" and exited 0. backfill_codes lied the same way, reporting codes it had
    just thrown away.

    db.transaction() opens a real transaction when there isn't one and a
    SAVEPOINT when there is, so the caller's earlier work survives either way and
    only the statement that collided is rolled back.
    """
    for _ in range(attempts):
        code = unused_code(db)
        try:
            with db.transaction():
                result = write(code)
        except psycopg.errors.UniqueViolation:
            continue
        return result, code
    raise RuntimeError("Couldn't assign a unique student code after several attempts.")


def backfill_codes(db) -> int:
    """Give a code to every student that hasn't got one. Returns how many.

    Idempotent: students who already have a code are not touched, so this is
    safe to run on every setup.
    """
    rows = db.execute(
        "SELECT id, name FROM students WHERE code IS NULL ORDER BY name"
    ).fetchall()

    for row in rows:
        with_new_code(
            db,
            lambda code, student_id=row["id"]: db.execute(
                "UPDATE students SET code = %s WHERE id = %s", (code, student_id)
            ),
        )
    db.commit()
    return len(rows)
