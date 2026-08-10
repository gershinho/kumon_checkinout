import io
import os
import secrets
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify, redirect,
    url_for, session, send_file, flash
)
from werkzeug.security import check_password_hash
from dotenv import load_dotenv
import psycopg

from db import get_db, init_db
from crypto_utils import decrypt_email, encrypt_email, mask_email, normalize_email
from email_utils import send_checkout_email, send_report_email
from pdf_report import generate_report_pdf
from time_utils import (
    close_time, fmt_date, fmt_date_short, fmt_datetime, fmt_time,
    is_report_day, local_now, local_today, report_days,
)

load_dotenv()


def create_app():
    app = Flask(__name__)

    # Vercel sets VERCEL=1, which is a reliable "am I in production" signal.
    in_production = bool(os.environ.get("VERCEL"))

    # Refuse to start in production without a real key rather than falling back
    # to one that is published in this repository. Session cookies are signed
    # with it, so a forgotten environment variable would let anyone mint their
    # own "I am the instructor" cookie - and nothing about the running app would
    # look wrong.
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if in_production:
            raise RuntimeError(
                "SECRET_KEY is not set. Add it in Vercel: Project Settings -> "
                "Environment Variables. Generate one with: "
                "python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        secret_key = "dev-key-change-me"
    app.secret_key = secret_key

    # Nothing is written to disk any more: reports are built in memory when
    # asked for, and failed emails are logged to stdout. Serverless hosts give
    # you a read-only filesystem, so the old os.makedirs() calls didn't fail
    # gracefully - they raised at import time and took every route down with
    # them, kiosk included.

    # Secure cookies require HTTPS, so forcing it on would break local http.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=in_production,
        # A dashboard left open on the office computer shouldn't stay logged in
        # forever. Twelve hours covers a full day at the center and expires
        # overnight.
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        # Safe to cache hard because the URL carries a version stamp (below), so
        # a restyled page gets a new URL rather than a stale cached file.
        SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=7),
    )

    init_db(app)

    # Every request for the stylesheet otherwise wakes the app up, since the
    # catch-all rewrite sends static files through it too.
    app.jinja_env.globals["static_v"] = _static_version()
    app.jinja_env.globals["csrf_token"] = csrf_token

    # ---------- CSRF ----------
    # Only the cron endpoint is exempt; it authenticates with a bearer token and
    # has no session to protect. The kiosk posts its token in a header instead of
    # a form field, because its requests are JSON sent by JavaScript.
    csrf_exempt = {"cron_nightly_close"}

    @app.before_request
    def verify_csrf():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        if request.endpoint in csrf_exempt:
            return None
        expected = session.get("_csrf", "")
        supplied = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not expected or not secrets.compare_digest(supplied, expected):
            if request.path.startswith("/api/"):
                return jsonify({"error": "expired", "login_url": url_for("kiosk_login")}), 403
            # Almost always a form left open until the session expired, so the
            # message says that rather than accusing the user of an attack.
            flash("That form expired. Please log in and try again.")
            return redirect(url_for("login"))
        return None

    # 12-hour time everywhere in the templates — no military time.
    app.add_template_filter(fmt_time, "time12")
    app.add_template_filter(fmt_date, "datelong")
    app.add_template_filter(fmt_date_short, "dateshort")
    app.add_template_filter(fmt_datetime, "datetime12")

    # ---------- request helpers ----------
    def student_id_param():
        """The student id from a JSON body or a form post, as an int.

        SQLite compared a text '7' against an integer column happily; Postgres
        is stricter, so the value is coerced here instead of at each query.
        Returns None for anything that isn't a number, which the callers turn
        into a 404 the same as an unknown student.
        """
        # silent=True so a malformed body returns None instead of raising, and
        # `or {}` because a bare "null" body parses to None rather than a dict.
        payload = request.get_json(silent=True) or {}
        raw = payload.get("student_id", request.form.get("student_id"))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # ---------- auth helpers ----------
    # There are two doors, and one key. The kiosk needs unlocking before anyone
    # can see the student list, and the dashboard needs the password entered
    # again on top of that - so a tablet left unlocked in the lobby all day is
    # only ever a check-in screen, never a way into the roster, the parent email
    # addresses, or the Remove buttons.
    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("instructor_id"):
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            return view(*args, **kwargs)
        return wrapped

    def kiosk_required(view):
        """Kiosk access. Being logged into the dashboard counts - the instructor
        has already proved they know the password, so don't ask twice going that
        direction."""
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not (session.get("kiosk_ok") or session.get("instructor_id")):
                if request.path.startswith("/api/"):
                    # The tablet's JavaScript is asking. Hand back something it
                    # can parse - a login page would arrive as unreadable HTML.
                    return jsonify({"error": "locked", "login_url": url_for("kiosk_login")}), 401
                return redirect(url_for("kiosk_login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    # ---------- student kiosk ----------
    @app.route("/")
    @kiosk_required
    def kiosk():
        return render_template("checkin.html", center_name=center_name())

    @app.get("/api/students")
    @kiosk_required
    def api_students():
        db = get_db()
        rows = db.execute(
            "SELECT id, name FROM students WHERE active = 1 ORDER BY name"
        ).fetchall()
        return jsonify([{"id": r["id"], "name": r["name"]} for r in rows])

    @app.get("/api/students/<int:student_id>/status")
    @kiosk_required
    def api_student_status(student_id):
        db = get_db()
        open_visit = db.execute(
            "SELECT id, check_in_time FROM visits "
            "WHERE student_id = %s AND check_out_time IS NULL "
            "ORDER BY check_in_time DESC LIMIT 1",
            (student_id,),
        ).fetchone()
        if open_visit:
            return jsonify({
                "status": "checked_in",
                "check_in_time": open_visit["check_in_time"],
                "check_in_display": fmt_time(open_visit["check_in_time"]),
            })
        return jsonify({"status": "checked_out"})

    @app.post("/api/checkin")
    @kiosk_required
    def api_checkin():
        student_id = student_id_param()
        db = get_db()
        student = db.execute("SELECT id, name FROM students WHERE id = %s", (student_id,)).fetchone()
        if not student:
            return jsonify({"error": "Student not found."}), 404

        now = local_now().isoformat(timespec="seconds")
        # Guarded insert: the WHERE NOT EXISTS is evaluated as part of the same
        # statement, so two rapid taps can't both create a visit. Checking first
        # and then inserting would let both pass the check before either wrote.
        #
        # Postgres runs writers concurrently, so unlike SQLite this guard alone
        # isn't enough - two taps landing at the same instant can both see no
        # open visit. The unique index in the schema is the real backstop, and
        # the loser of that race arrives here as a UniqueViolation.
        try:
            cur = db.execute(
                "INSERT INTO visits (student_id, check_in_time) "
                "SELECT %s, %s WHERE NOT EXISTS ("
                "  SELECT 1 FROM visits WHERE student_id = %s AND check_out_time IS NULL"
                ")",
                (student_id, now, student_id),
            )
            db.commit()
        except psycopg.errors.UniqueViolation:
            db.rollback()
            return jsonify({"error": f"{student['name']} is already checked in."}), 400
        if cur.rowcount == 0:
            return jsonify({"error": f"{student['name']} is already checked in."}), 400
        return jsonify({
            "message": f"{student['name']} checked in at {fmt_time(now)}!",
            "time": now,
            "time_display": fmt_time(now),
        })

    @app.post("/api/checkout")
    @kiosk_required
    def api_checkout():
        student_id = student_id_param()
        db = get_db()
        student = db.execute(
            "SELECT id, name, email_enc FROM students WHERE id = %s", (student_id,)
        ).fetchone()
        if not student:
            return jsonify({"error": "Student not found."}), 404

        now = local_now()
        now_iso = now.isoformat(timespec="seconds")

        # Claim the visit BEFORE sending anything. The "AND check_out_time IS NULL"
        # means only one request can win, so an impatient student tapping Check Out
        # five times still results in exactly one email to their parent. Sending
        # first and writing afterwards would send one email per tap.
        cur = db.execute(
            "UPDATE visits SET check_out_time = %s "
            "WHERE id = (SELECT id FROM visits WHERE student_id = %s AND check_out_time IS NULL "
            "            ORDER BY check_in_time DESC LIMIT 1) "
            "AND check_out_time IS NULL "
            "RETURNING id",
            (now_iso, student_id),
        )
        row = cur.fetchone() if cur.rowcount else None
        db.commit()
        if row is None:
            return jsonify({"error": f"{student['name']} is not currently checked in."}), 400

        # RETURNING gives us the row we just claimed. The old follow-up SELECT
        # matched on check_out_time, which would pick the wrong visit if the
        # same student somehow had two rows stamped in the same second.
        visit_id = row["id"]

        email = decrypt_email(student["email_enc"])
        email_status = send_checkout_email(email, student["name"], now)

        db.execute(
            "UPDATE visits SET email_status = %s WHERE id = %s",
            (email_status, visit_id),
        )
        db.commit()

        note = ""
        if email_status == "not_configured":
            note = " (Email isn't set up yet — see the instructor dashboard.)"
        elif email_status == "failed":
            note = " (We couldn't send the email — the instructor has been notified.)"
        elif email_status == "no_address":
            note = " (No parent email on file — please tell your instructor.)"

        return jsonify({
            "message": f"{student['name']} checked out at {fmt_time(now)}!{note}",
            "time": now_iso,
            "time_display": fmt_time(now),
            "email_status": email_status,
        })

    # ---------- auth ----------
    # Both doors are this one function, rendering this one page. Same look, same
    # username and password. The only difference is how much a success grants:
    # the kiosk door unlocks the check-in screen and nothing else, so getting
    # into the dashboard means typing the password again even from a tablet
    # that's already unlocked.
    def _login_page(grants_dashboard: bool, default_next: str, show_kiosk_link: bool):
        if request.method == "POST":
            if _login_locked_out():
                flash("Too many failed attempts. Please wait 15 minutes and try again.")
                return render_template("login.html", show_kiosk_link=show_kiosk_link), 429

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            db = get_db()
            row = db.execute(
                "SELECT id, password_hash FROM instructors WHERE username = %s", (username,)
            ).fetchone()
            if row and check_password_hash(row["password_hash"], password):
                _clear_login_attempts()
                # Cleared and rebuilt so an old session can't be re-used, and so
                # the CSRF token is reissued along with the new access.
                session.clear()
                session.permanent = True
                session["kiosk_ok"] = True
                if grants_dashboard:
                    session["instructor_id"] = row["id"]
                # Only same-site paths. The decorators only ever set `next` to a
                # local path, but the URL is user-supplied, so "next=https://
                # elsewhere" would otherwise hand a freshly logged-in instructor
                # to another site. "//host" is a protocol-relative URL, so it has
                # to be rejected too even though it starts with a slash.
                nxt = request.args.get("next") or ""
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = default_next
                return redirect(nxt)
            _record_failed_login()
            flash("Incorrect username or password.")
        return render_template("login.html", show_kiosk_link=show_kiosk_link)

    @app.route("/kiosk-login", methods=["GET", "POST"])
    def kiosk_login():
        """The door onto the check-in screen. Unlocked once in the morning."""
        # No "Back to check-in" link here - it would point at the page you are
        # already looking at.
        return _login_page(grants_dashboard=False, default_next=url_for("kiosk"),
                           show_kiosk_link=False)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """The door onto the dashboard. Always asks, even from an unlocked kiosk."""
        return _login_page(grants_dashboard=True, default_next=url_for("dashboard"),
                           show_kiosk_link=True)

    @app.route("/logout")
    def logout():
        # Drops kiosk access too. Logging out should mean logging out; the
        # tablet is unlocked again with the same password.
        session.clear()
        return redirect(url_for("login"))

    # ---------- instructor dashboard ----------
    @app.get("/dashboard")
    @login_required
    def dashboard():
        db = get_db()
        # Validated before it reaches SQL: Postgres raises on a malformed date
        # rather than returning nothing, which would 500 the page and leave the
        # connection in a failed transaction.
        day_str = request.args.get("date") or ""
        try:
            day_str = date.fromisoformat(day_str).isoformat()
        except ValueError:
            day_str = local_today().isoformat()

        rows = db.execute(
            """
            SELECT v.id, s.name AS name, v.check_in_time, v.check_out_time, v.email_status, s.email_enc
            FROM visits v JOIN students s ON s.id = v.student_id
            WHERE v.check_in_time::date = %s::date
            ORDER BY v.check_in_time DESC
            """,
            (day_str,),
        ).fetchall()

        visits = []
        for r in rows:
            visits.append({
                "id": r["id"],
                "name": r["name"],
                "check_in_time": r["check_in_time"],
                "check_out_time": r["check_out_time"],
                "email_status": r["email_status"],
                "email_masked": mask_email(decrypt_email(r["email_enc"])),
            })

        student_rows = db.execute(
            "SELECT id, name, email_enc FROM students WHERE active = 1 ORDER BY name"
        ).fetchall()
        students = [
            {"id": r["id"], "name": r["name"],
             "email_masked": mask_email(decrypt_email(r["email_enc"]))}
            for r in student_rows
        ]

        closing = close_time()
        return render_template(
            "dashboard.html",
            visits=visits,
            selected_date=day_str,
            today=local_today().isoformat(),
            session_dates=recent_session_dates(),
            students=students,
            student_count=len(students),
            close_time_display=fmt_time(datetime.combine(local_today(), closing)),
            report_days_display=_report_days_display(),
        )

    @app.post("/dashboard/students")
    @login_required
    def add_student():
        name = request.form.get("name", "").strip()
        email_raw = request.form.get("email", "").strip()

        if not name or not email_raw:
            flash("Enter both a name and a parent email address.")
            return redirect(url_for("dashboard"))

        email = normalize_email(email_raw)
        if not email:
            flash(f"'{email_raw}' doesn't look like a valid email address.")
            return redirect(url_for("dashboard"))

        db = get_db()
        # Matched without filtering on `active` on purpose. "Removed" students
        # are only deactivated, never deleted, so filtering would insert a
        # second row with the same name - and import_students.py matches on name
        # alone, so it would then update whichever of the two it happened to
        # find first. Re-adding someone reactivates the row that already exists.
        existing = db.execute(
            "SELECT id, active FROM students WHERE name = %s", (name,)
        ).fetchone()

        if existing and existing["active"]:
            flash(f"A student named '{name}' is already on the list.")
            return redirect(url_for("dashboard"))

        if existing:
            db.execute(
                "UPDATE students SET email_enc = %s, active = 1 WHERE id = %s",
                (encrypt_email(email), existing["id"]),
            )
            db.commit()
            flash(f"{name} was previously removed — added back with that email.")
            return redirect(url_for("dashboard"))

        db.execute(
            "INSERT INTO students (name, email_enc, active) VALUES (%s, %s, 1)",
            (name, encrypt_email(email)),
        )
        db.commit()
        flash(f"Added {name}.")
        return redirect(url_for("dashboard"))

    @app.post("/dashboard/students/<int:student_id>/email")
    @login_required
    def update_student_email(student_id):
        db = get_db()
        student = db.execute(
            "SELECT id, name FROM students WHERE id = %s", (student_id,)
        ).fetchone()
        if not student:
            flash("That student isn't on the list.")
            return redirect(url_for("dashboard"))

        email_raw = request.form.get("email", "").strip()
        if not email_raw:
            flash("Enter the new email address.")
            return redirect(url_for("dashboard"))

        email = normalize_email(email_raw)
        if not email:
            flash(f"'{email_raw}' doesn't look like a valid email address. "
                  f"{student['name']}'s email was not changed.")
            return redirect(url_for("dashboard"))

        db.execute(
            "UPDATE students SET email_enc = %s WHERE id = %s",
            (encrypt_email(email), student_id),
        )
        db.commit()
        flash(f"Updated {student['name']}'s email to {mask_email(email)}.")
        return redirect(url_for("dashboard"))

    @app.post("/dashboard/students/<int:student_id>/deactivate")
    @login_required
    def deactivate_student(student_id):
        db = get_db()
        student = db.execute("SELECT name FROM students WHERE id = %s", (student_id,)).fetchone()
        if student:
            db.execute("UPDATE students SET active = 0 WHERE id = %s", (student_id,))
            db.commit()
            flash(f"Removed {student['name']} from the check-in list.")
        return redirect(url_for("dashboard"))

    @app.get("/dashboard/reports/<report_date>")
    @login_required
    def download_report(report_date):
        """Build the report for a date and hand it straight to the browser.

        Reports used to be written to disk at 10 PM, listed in a `reports`
        table, and pruned once there were more than ten. None of that is needed:
        every visit is already in the database, so the PDF can be rebuilt from
        scratch whenever it's asked for. That removed the storage, the table,
        the pruning, and the "download anything you want to keep" caveat - and
        as a bonus, any date works now, not just Mondays and Thursdays.
        """
        try:
            day = date.fromisoformat(report_date)
        except ValueError:
            flash("That isn't a valid date.")
            return redirect(url_for("dashboard"))

        buf = build_report_pdf(day)
        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"report_{day.isoformat()}.pdf",
        )

    # ---------- nightly close-out, triggered by the host's scheduler ----------
    # The center runs 2 PM - 10 PM on Mondays and Thursdays. Every night after
    # close we clear out anyone who forgot to check out, so nobody is left
    # "checked in" and blocked from checking in next session. On session days the
    # PDF report is emailed out, since nothing is stored on the server.
    #
    # This used to be an APScheduler background thread. That needs a process
    # that stays alive between requests, which serverless hosts don't have - the
    # timer would have been created and destroyed in the same half-second, every
    # request, silently never firing. Vercel Cron calls this URL instead.
    #
    # Timing is forgiving on purpose. _close_open_visits() stamps the closing
    # time based on each visit's own check-in date, not on when the job runs, and
    # the report covers last_session_day() rather than "today", so a late run -
    # or one that crosses midnight because the cron schedule is in UTC and the
    # center isn't - still reports on the session that just ended. Re-running is
    # harmless too: the second run finds nothing left open.
    # GET as well as POST: Vercel Cron invokes the path with a GET request.
    @app.route("/api/cron/nightly-close", methods=["GET", "POST"])
    def cron_nightly_close():
        expected = os.environ.get("CRON_SECRET")
        supplied = request.headers.get("Authorization", "")
        # compare_digest rather than != so the comparison doesn't return early
        # on the first wrong character. Without this check at all, anyone who
        # found the URL could close out the center.
        if not expected or not secrets.compare_digest(supplied, f"Bearer {expected}"):
            return jsonify({"error": "unauthorized"}), 401

        closed = _close_open_visits()
        session_day = last_session_day()
        result = {"closed": closed, "date": session_day.isoformat(), "report": "skipped"}

        if is_report_day(session_day):
            buf = build_report_pdf(session_day)
            result["report"] = send_report_email(
                buf.getvalue(), f"report_{session_day.isoformat()}.pdf", session_day
            )

        app.logger.info("Nightly close: %s", result)
        return jsonify(result)

    return app


DEFAULT_SESSION_DATES_SHOWN = 10

# How long a run of failed logins is remembered, and how many are allowed in it.
LOGIN_WINDOW_MINUTES = 15
LOGIN_MAX_ATTEMPTS = 10


def center_name() -> str:
    """The center's name, for the kiosk, the parent emails and the PDF.

    One function so there is one default. The PDF used to fall back to "Kumon
    Center" while everywhere else fell back to "Kumon", which meant leaving
    CENTER_NAME unset quietly retitled the report but nothing else.
    """
    return (os.environ.get("CENTER_NAME") or "").strip() or "Kumon"


def _static_version() -> str:
    """A stamp that changes when the stylesheet does, used as a cache buster.

    Lets the stylesheet be cached for a week without the risk of a restyled page
    serving the old one: editing the file changes its size and timestamp, which
    changes the URL.
    """
    try:
        stat = os.stat(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "static", "style.css"))
        return f"{int(stat.st_mtime)}-{stat.st_size}"
    except OSError:
        return "1"


def csrf_token() -> str:
    """A per-session token that every form on the site has to send back.

    Without it, another site could quietly post to /dashboard/students/5/deactivate
    while an instructor is logged in, and the browser would attach their cookie.
    SameSite=Lax already blocks the usual version of that; this closes the rest.
    """
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def last_session_day():
    """The date of the session that has just finished.

    The nightly job is scheduled in UTC, so depending on daylight saving it can
    run either side of local midnight - and "today" would then mean the day that
    is only just starting. Anchoring on the closing time instead means the report
    always covers the session that actually happened, whether the job runs at
    11 PM or at 1 AM.
    """
    now = local_now()
    if now.time() >= close_time():
        return now.date()
    return now.date() - timedelta(days=1)


def _client_ip() -> str:
    """The visitor's address. Behind Vercel's proxy remote_addr is the proxy, so
    the first entry of X-Forwarded-For is the real client."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:100]
    return (request.remote_addr or "unknown")[:100]


def _login_window_start() -> str:
    return (local_now() - timedelta(minutes=LOGIN_WINDOW_MINUTES)).isoformat(timespec="seconds")


def _login_locked_out() -> bool:
    """True once an address has failed too many times recently.

    Fails open if the login_attempts table hasn't been created yet - a login
    page nobody can use is a worse outcome than an unthrottled one, and the
    warning below says exactly what to run.
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT count(*) AS n FROM login_attempts WHERE ip = %s AND attempted_at > %s",
            (_client_ip(), _login_window_start()),
        ).fetchone()
    except psycopg.errors.UndefinedTable:
        db.rollback()
        print("[login] WARNING: no login_attempts table, so failed logins are not "
              "being throttled. Run: python setup_db.py", flush=True)
        return False
    return row["n"] >= LOGIN_MAX_ATTEMPTS


def _record_failed_login():
    db = get_db()
    try:
        db.execute(
            "INSERT INTO login_attempts (ip, attempted_at) VALUES (%s, %s)",
            (_client_ip(), local_now().isoformat(timespec="seconds")),
        )
        # Cheap housekeeping: rows older than the window can never matter again,
        # and pruning here means the table needs no separate cleanup job.
        db.execute("DELETE FROM login_attempts WHERE attempted_at < %s", (_login_window_start(),))
        db.commit()
    except psycopg.errors.UndefinedTable:
        db.rollback()


def _clear_login_attempts():
    """A correct password wipes the slate, so a few typos don't lock out the
    instructor for the rest of the window."""
    db = get_db()
    try:
        db.execute("DELETE FROM login_attempts WHERE ip = %s", (_client_ip(),))
        db.commit()
    except psycopg.errors.UndefinedTable:
        db.rollback()


def recent_session_dates(count=DEFAULT_SESSION_DATES_SHOWN):
    """The most recent days the center ran, newest first.

    This replaces the old list of saved report files. Since any date's report
    can be rebuilt on demand, the dashboard just needs to know which days are
    worth offering - and there's no longer a limit on how far back you can go.
    """
    days = []
    day = local_today()
    # Enough lookback to find `count` session days even on a one-day-a-week
    # schedule, without an unbounded loop if REPORT_DAYS is somehow empty.
    for _ in range(count * 7 + 7):
        if is_report_day(day):
            days.append(day)
            if len(days) >= count:
                break
        day -= timedelta(days=1)
    return days


def _report_days_display() -> str:
    """'Mondays and Thursdays' for on-screen copy."""
    names = {
        "mon": "Mondays", "tue": "Tuesdays", "wed": "Wednesdays", "thu": "Thursdays",
        "fri": "Fridays", "sat": "Saturdays", "sun": "Sundays",
    }
    labels = [names[d] for d in report_days()]
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _close_open_visits():
    """Close out anyone still checked in, stamping the center's closing time on
    the day they checked in. No email is sent — nobody witnessed them leaving, so
    telling a parent 'checked out at 10:00 PM' would be a guess presented as fact.
    Returns the number of visits closed.

    Runs inside the caller's app context. It used to push its own, which was
    free under a background scheduler that had none; called from a request it
    would open a second connection to do work the first one could already do.
    """
    closing = close_time()
    db = get_db()
    open_visits = db.execute(
        "SELECT id, check_in_time FROM visits WHERE check_out_time IS NULL"
    ).fetchall()

    closed = 0
    for visit in open_visits:
        try:
            checked_in = datetime.fromisoformat(visit["check_in_time"])
        except (TypeError, ValueError):
            continue
        close_dt = datetime.combine(checked_in.date(), closing)
        # A check-in after closing time would otherwise get a negative duration.
        if close_dt < checked_in:
            close_dt = checked_in
        db.execute(
            "UPDATE visits SET check_out_time = %s, email_status = %s WHERE id = %s",
            (close_dt.isoformat(timespec="seconds"), "auto_closed", visit["id"]),
        )
        closed += 1
    db.commit()
    return closed


def build_report_pdf(report_date) -> io.BytesIO:
    """Build the PDF report for a date, in memory.

    reportlab writes to any file-like object, so generate_report_pdf() needed no
    changes at all - it just gets a BytesIO instead of a path. Nothing is stored
    afterwards: the visits are already in the database, so the report is
    reproducible for any date, any time.
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT s.name AS name, v.check_in_time, v.check_out_time, v.email_status
        FROM visits v JOIN students s ON s.id = v.student_id
        WHERE v.check_in_time::date = %s::date
        """,
        (report_date.isoformat(),),
    ).fetchall()
    visits = [dict(r) for r in rows]

    buf = io.BytesIO()
    generate_report_pdf(
        report_date, visits, buf,
        center_name=center_name(),
    )
    buf.seek(0)
    return buf


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
