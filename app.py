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
from crypto_utils import (
    assert_configured, decrypt_email, encrypt_email, mask_email, normalize_email,
)
from email_utils import send_report_email, send_work_done_email
from pdf_report import generate_report_pdf
from student_codes import CODE_DIGITS, with_new_code
from time_utils import (
    close_time, fmt_date, fmt_date_short, fmt_datetime, fmt_time,
    is_report_day, local_now, local_today, report_days,
)

load_dotenv()


def create_app():
    app = Flask(__name__)

    # Safe by default: everything is treated as production unless someone says
    # otherwise out loud, with KUMON_DEV=1.
    #
    # This used to be `bool(os.environ.get("VERCEL"))`, which meant the checks
    # below only applied on Vercel. The README documents running this on the
    # center's own network with gunicorn, and there VERCEL is unset - so the app
    # signed its session cookies with "dev-key-change-me", a string published in
    # this repository, over plain http. Anyone on the wifi could mint their own
    # instructor cookie and read every student code and parent address. An
    # opt-out that has to be typed cannot be reached by forgetting something.
    in_production = os.environ.get("KUMON_DEV") != "1"

    # Refuse to start in production without a real key rather than falling back
    # to one that is published in this repository. Session cookies are signed
    # with it, so a forgotten environment variable would let anyone mint their
    # own "I am the instructor" cookie - and nothing about the running app would
    # look wrong.
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if in_production:
            raise RuntimeError(
                "SECRET_KEY is not set, and without it session cookies would be "
                "signed with a key published in this repository - anyone could "
                "forge an instructor login. Generate one with:\n"
                "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
                "then set it in your host's environment (on Vercel: Project "
                "Settings -> Environment Variables), or set KUMON_DEV=1 if this "
                "really is a throwaway local run."
            )
        secret_key = "dev-key-change-me"
    app.secret_key = secret_key

    # Whether the cookie may travel over plain http is a SEPARATE question from
    # whether this is production, and conflating them breaks one setup or the
    # other. Secure=True on the README's http://<local-ip>:8000 gunicorn setup
    # means the browser never sends the cookie back and nobody can log in at
    # all; Secure=False on Vercel puts the session on the wire in clear. So it
    # defaults to on in production and has to be turned off deliberately.
    allow_plain_http = os.environ.get("ALLOW_INSECURE_HTTP") == "1"
    if in_production and allow_plain_http:
        print("[startup] WARNING: ALLOW_INSECURE_HTTP=1 - session cookies will be "
              "sent over unencrypted http. Only do this on a trusted local "
              "network, never on the public internet.", flush=True)

    # Same reasoning, for the key that makes parent addresses readable. Checked
    # here so a misconfigured deployment says so on the first request instead of
    # serving a kiosk that quietly cannot complete a check-out.
    if in_production:
        assert_configured()

    # Nothing is written to disk any more: reports are built in memory when
    # asked for, and failed emails are logged to stdout. Serverless hosts give
    # you a read-only filesystem, so the old os.makedirs() calls didn't fail
    # gracefully - they raised at import time and took every route down with
    # them, kiosk included.

    # Secure cookies require HTTPS, so forcing it on would break local http.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=in_production and not allow_plain_http,
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
        if not expected or not same_secret(supplied, expected):
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
        payload = json_body()
        raw = payload.get("student_id", request.form.get("student_id"))
        # bool is a subclass of int, so int(True) is 1 - a client bug sending
        # `true` would silently act on student number 1 instead of 404ing.
        if isinstance(raw, bool):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def json_body() -> dict:
        """The JSON body, guaranteed to be a dict.

        silent=True covers a malformed body, but a body that parses to a valid
        *non-object* - `[1,2]`, `"hi"`, `42`, `true` - is truthy and has no
        .get, so `get_json(silent=True) or {}` let it through and every kiosk
        endpoint answered 500.
        """
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    def code_param() -> str:
        """The 4-digit code the student typed, as text.

        Kept as text all the way through. Read as a number, "0421" and "421"
        would both become 421 and both be accepted, which quietly turns some
        codes into three-digit ones.
        """
        raw = json_body().get("code", request.form.get("code", ""))
        return str(raw or "").strip()

    def wrong_code(student):
        """Check the typed code against this student's, and throttle guessing.

        Returns a ready-to-return error response, or None when the code is
        right and the caller should go ahead.

        Every kiosk action goes through here. The three actions are the three
        moments something happens that a parent would want to be true - the
        child arrived, the child finished, the child left with someone - so
        none of them should be one stray tap away.
        """
        if _code_locked_out(student["id"]):
            return jsonify({
                "error": f"Too many wrong codes for {student['name']}. "
                         f"Please ask your instructor for help."
            }), 429

        stored = (student["code"] or "").strip()
        if not stored:
            # Only reachable if setup_db.py hasn't been run since codes were
            # added. Say which student, so the instructor can fix it.
            return jsonify({
                "error": f"{student['name']} doesn't have a code yet. "
                         f"Please ask your instructor."
            }), 400

        supplied = code_param()

        # A submission that isn't even the right shape is not a guess, and must
        # not count as one. It used to: five blank submissions locked a student
        # out for fifteen minutes, and since /api/students hands the kiosk every
        # student id, anyone standing at the tablet could loop the roster and
        # lock all of them out - including the check-out step, so students could
        # not be signed out by their parents.
        if not supplied.isdigit() or len(supplied) != CODE_DIGITS:
            return jsonify({"error": f"Please enter the {CODE_DIGITS}-digit code."}), 403

        # same_secret rather than compare_digest directly: the value is whatever
        # was typed, and compare_digest raises on non-ASCII text.
        if not same_secret(supplied, stored):
            _record_failed_code(student["id"])
            return jsonify({"error": "That code isn't right. Please try again."}), 403

        _clear_code_attempts(student["id"])
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
        """The roster, each name already carrying which step it's up to.

        This used to return names only, which meant the kiosk had to make a
        second request the moment a student tapped their name, and couldn't
        show the right button until it came back. Sending the stage up front
        is a few extra bytes per student and removes that wait entirely.

        Codes are deliberately not in here. The kiosk never needs to know them
        - it sends what was typed and the server decides - and anything this
        endpoint returns can be read straight out of the page by anyone
        standing at the tablet, which would make the whole list public.

        The LEFT JOIN can't multiply rows: the unique index on open visits
        guarantees at most one per student.
        """
        db = get_db()
        rows = db.execute(
            "SELECT s.id, s.name, v.check_in_time, v.work_done_time, v.email_status "
            "FROM students s "
            "LEFT JOIN visits v ON v.student_id = s.id AND v.check_out_time IS NULL "
            "WHERE s.active = 1 ORDER BY s.name"
        ).fetchall()
        return jsonify([
            {
                "id": r["id"],
                "name": r["name"],
                "stage": visit_stage(r["check_in_time"], r["work_done_time"]),
                "check_in_display": fmt_time(r["check_in_time"]) if r["check_in_time"] else None,
                "work_done_display": fmt_time(r["work_done_time"]) if r["work_done_time"] else None,
                # Whether the parent actually got the message, so the panel can
                # say so honestly after a roster refresh. Not the raw status:
                # that is instructor-facing detail and this endpoint is readable
                # by anyone at the tablet.
                "emailed": r["email_status"] == "sent",
            }
            for r in rows
        ])

    # The kiosk is three steps now, in this order, each needing the student's
    # code:
    #
    #   1. Parent Check In   - the parent drops the student off.
    #   2. Done with Work    - the student has finished. This emails the parent.
    #   3. Parent Check Out  - the parent collects the student.
    #
    # The order is enforced here, not just in the interface. The kiosk only ever
    # offers the next step, but it decides that from a roster it may have loaded
    # some minutes ago, so the server is what actually guarantees that no visit
    # skips a stage.

    @app.post("/api/checkin")
    @kiosk_required
    def api_checkin():
        student_id = student_id_param()
        db = get_db()
        student = db.execute(
            "SELECT id, name, code FROM students WHERE id = %s", (student_id,)
        ).fetchone()
        if not student:
            return jsonify({"error": "Student not found."}), 404

        denied = wrong_code(student)
        if denied:
            return denied

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
            "stage": "checked_in",
            "time": now,
            "time_display": fmt_time(now),
        })

    @app.post("/api/work-done")
    @kiosk_required
    def api_work_done():
        """The student has finished. This is what emails the parent."""
        student_id = student_id_param()
        db = get_db()
        student = db.execute(
            "SELECT id, name, code, email_enc FROM students WHERE id = %s", (student_id,)
        ).fetchone()
        if not student:
            return jsonify({"error": "Student not found."}), 404

        denied = wrong_code(student)
        if denied:
            return denied

        now = local_now()
        now_iso = now.isoformat(timespec="seconds")

        # Claim the visit BEFORE sending anything. The "AND work_done_time IS
        # NULL" means only one request can win, so an impatient student tapping
        # the button five times still results in exactly one email to their
        # parent. Sending first and writing afterwards would send one per tap.
        cur = db.execute(
            "UPDATE visits SET work_done_time = %s "
            "WHERE id = (SELECT id FROM visits WHERE student_id = %s "
            "            AND check_out_time IS NULL AND work_done_time IS NULL "
            "            ORDER BY check_in_time DESC LIMIT 1) "
            "AND work_done_time IS NULL "
            "RETURNING id",
            (now_iso, student_id),
        )
        row = cur.fetchone() if cur.rowcount else None
        db.commit()
        if row is None:
            # Nothing to claim, for one of two reasons, and they need different
            # answers: one means "get a grown-up", the other means "you already
            # did this, sit tight".
            open_visit = db.execute(
                "SELECT work_done_time, email_status FROM visits "
                "WHERE student_id = %s AND check_out_time IS NULL",
                (student_id,),
            ).fetchone()
            if open_visit:
                # Whether the parent was actually told is read from the row, not
                # assumed. This used to say "Their parent has been emailed."
                # unconditionally, so a student whose first tap failed to send
                # was reassured on the second tap and stopped telling anyone -
                # the one case where a parent is left waiting and nobody knows.
                if open_visit["email_status"] == "sent":
                    tail = "Their parent has been emailed."
                else:
                    tail = ("We still could not email their parent — "
                            "please tell your instructor.")
                return jsonify({
                    "error": f"{student['name']} already finished at "
                             f"{fmt_time(open_visit['work_done_time'])}. {tail}"
                }), 400
            return jsonify({"error": f"{student['name']} is not checked in yet."}), 400

        # RETURNING gives us the row we just claimed, rather than a follow-up
        # SELECT that could pick the wrong visit.
        visit_id = row["id"]

        email = decrypt_email(student["email_enc"])
        email_status = send_work_done_email(email, student["name"], now)

        db.execute(
            "UPDATE visits SET email_status = %s WHERE id = %s",
            (email_status, visit_id),
        )
        db.commit()

        # Finishing succeeded either way - the time is recorded and the student
        # can pack up. What may have failed is telling their parent, and only a
        # person can fix that, so say so plainly instead of burying it in a
        # green tick. It matters more than it used to: the parent may be waiting
        # on this email before setting off.
        problems = {
            "failed": "We could not email your parent, so they don't know you're "
                      "finished. Please tell your instructor.",
            "partial": "We could only reach one of your parents' email addresses. "
                       "Please tell your instructor.",
            "not_configured": "Parent emails aren't switched on yet, so nobody was emailed. "
                              "Please tell your instructor.",
            "no_address": "We don't have a parent email on file for you, so nobody was emailed. "
                          "Please tell your instructor.",
        }
        problem = problems.get(email_status)

        # The headline has to match the follow-up. It was hardcoded to "Your
        # parent has been emailed", which the kiosk then rendered immediately
        # above "We could not email your parent" - one toast contradicting
        # itself in consecutive sentences.
        headline = (f"Nice work, {student['name']}! Your parent has been emailed."
                    if email_status == "sent"
                    else f"Nice work, {student['name']}! Your work is marked as done.")

        return jsonify({
            "message": headline,
            "stage": "work_done",
            "email_ok": email_status == "sent",
            "problem": problem,
            "time": now_iso,
            "time_display": fmt_time(now),
            "email_status": email_status,
        })

    @app.post("/api/checkout")
    @kiosk_required
    def api_checkout():
        """The parent collects the student. No email - they're standing here."""
        student_id = student_id_param()
        db = get_db()
        student = db.execute(
            "SELECT id, name, code FROM students WHERE id = %s", (student_id,)
        ).fetchone()
        if not student:
            return jsonify({"error": "Student not found."}), 404

        denied = wrong_code(student)
        if denied:
            return denied

        now = local_now()
        now_iso = now.isoformat(timespec="seconds")

        # "AND work_done_time IS NOT NULL" is what keeps the three steps in
        # order: a visit cannot be checked out until the student has said they
        # finished, so no visit ends up with a pick-up time and no record of
        # what the student was there for.
        cur = db.execute(
            "UPDATE visits SET check_out_time = %s "
            "WHERE id = (SELECT id FROM visits WHERE student_id = %s "
            "            AND check_out_time IS NULL AND work_done_time IS NOT NULL "
            "            ORDER BY check_in_time DESC LIMIT 1) "
            "AND check_out_time IS NULL "
            "RETURNING id",
            (now_iso, student_id),
        )
        row = cur.fetchone() if cur.rowcount else None
        db.commit()
        if row is None:
            open_visit = db.execute(
                "SELECT id FROM visits WHERE student_id = %s AND check_out_time IS NULL",
                (student_id,),
            ).fetchone()
            if open_visit:
                return jsonify({
                    "error": f"{student['name']} hasn't finished their work yet. "
                             f"Tap “Done with Work” first."
                }), 400
            return jsonify({"error": f"{student['name']} is not currently checked in."}), 400

        return jsonify({
            "message": f"{student['name']} checked out at {fmt_time(now)}. See you next time!",
            "stage": "out",
            "time": now_iso,
            "time_display": fmt_time(now),
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
                # to another site. See safe_next() for why checking the raw
                # string is not enough.
                return redirect(safe_next(request.args.get("next"), default_next))
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
            SELECT v.id, s.name AS name, v.check_in_time, v.work_done_time, v.check_out_time,
                   v.email_status, v.auto_closed, s.email_enc
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
                "work_done_time": r["work_done_time"],
                "check_out_time": r["check_out_time"],
                "email_status": r["email_status"],
                "auto_closed": bool(r["auto_closed"]),
                "email_masked": mask_email(decrypt_email(r["email_enc"])),
            })

        student_rows = db.execute(
            "SELECT id, name, email_enc, code FROM students WHERE active = 1 ORDER BY name"
        ).fetchall()
        students = [
            {"id": r["id"], "name": r["name"], "code": r["code"],
             "email_masked": mask_email(decrypt_email(r["email_enc"]))}
            for r in student_rows
        ]

        closing = close_time()
        listed_dates = report_dates()
        # What the "any other session day" dropdown offers. Built from the
        # calendar, so it covers session days the list above leaves out for
        # having no check-ins - a Monday nobody attended is still a fair
        # question to ask - minus the ones already listed, so no date appears
        # in both places.
        listed = set(listed_dates)
        other_days = group_by_month(
            [d for d in recent_session_days() if d not in listed]
        )
        return render_template(
            "dashboard.html",
            visits=visits,
            selected_date=day_str,
            today=local_today().isoformat(),
            session_dates=listed_dates,
            other_session_days=other_days,
            students=students,
            student_count=len(students),
            close_time_display=fmt_time(datetime.combine(local_today(), closing)),
            report_days_display=_report_days_display(),
            min_visits_for_email=MIN_VISITS_FOR_EMAILED_REPORT,
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
            "SELECT id, active, code FROM students WHERE name = %s", (name,)
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
            # They keep the code they had, so a student who left and came back
            # doesn't have to learn a new one. A student from before codes
            # existed, or one whose row predates the migration, gets one now -
            # and is told it's new, because nobody has ever been given it.
            if existing["code"]:
                flash(f"{name} was previously removed — added back with that email. "
                      f"Their code is still {existing['code']}.")
            else:
                flash(f"{name} was previously removed — added back with that email. "
                      f"Their new 4-digit code is {_assign_code(db, existing['id'])}.")
            return redirect(url_for("dashboard"))

        _, code = with_new_code(
            db,
            lambda code: db.execute(
                "INSERT INTO students (name, email_enc, active, code) VALUES (%s, %s, 1, %s)",
                (name, encrypt_email(email), code),
            ),
        )
        db.commit()
        flash(f"Added {name}. Their 4-digit code is {code}.")
        return redirect(url_for("dashboard"))

    @app.get("/api/student-codes")
    @login_required
    def api_student_codes():
        """Names and codes, for the panel on the dashboard.

        Instructor login only - deliberately not @kiosk_required. The kiosk
        unlocks once in the morning and stays unlocked on a tablet in the
        lobby; if that were enough to reach this, every code would be one URL
        away from anyone who picked the tablet up.
        """
        db = get_db()
        rows = db.execute(
            "SELECT id, name, code FROM students WHERE active = 1 ORDER BY name"
        ).fetchall()
        return jsonify([
            {"id": r["id"], "name": r["name"], "code": r["code"]} for r in rows
        ])

    @app.post("/dashboard/students/<int:student_id>/code")
    @login_required
    def regenerate_student_code(student_id):
        """Give a student a new code, for when the old one has got around."""
        db = get_db()
        student = db.execute(
            "SELECT id, name FROM students WHERE id = %s", (student_id,)
        ).fetchone()
        if not student:
            flash("That student isn't on the list.")
            return redirect(url_for("dashboard"))

        code = _assign_code(db, student_id)
        # The old code stops working the moment this runs, so the student has to
        # be told the new one - hence showing it here rather than just saying
        # "done".
        flash(f"{student['name']}'s new code is {code}. The old one no longer works.")
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

        # The dropdown only offers sensible dates, but the URL is typed as
        # easily as it is clicked, and a future date can only ever produce a
        # blank PDF.
        if day > local_today():
            flash("That date hasn't happened yet.")
            return redirect(url_for("dashboard"))

        if report_is_pointless(day):
            flash(f"Nothing to report for {fmt_date(day)} — the center runs on "
                  f"{_report_days_display()}, and nobody checked in that day.")
            return redirect(url_for("dashboard"))

        buf = build_report_pdf(day)
        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"report_{day.isoformat()}.pdf",
        )

    @app.post("/dashboard/reports/<report_date>/email")
    @login_required
    def email_report(report_date):
        """Email a report on demand, to the address the nightly job uses.

        Two jobs. It's the safe way to prove the report email works end to end -
        it goes to the instructor, never to a parent - and it covers the nights
        the automatic one deliberately stays quiet: a day with three or fewer
        check-ins, or any day that isn't a session day. Asking for it by hand is
        explicit, so no threshold is applied.
        """
        try:
            day = date.fromisoformat(report_date)
        except ValueError:
            flash("That isn't a valid date.")
            return redirect(url_for("dashboard"))
        if day > local_today():
            flash("That date hasn't happened yet.")
            return redirect(url_for("dashboard"))

        if report_is_pointless(day):
            flash(f"Nothing to report for {fmt_date(day)} — the center runs on "
                  f"{_report_days_display()}, and nobody checked in that day.")
            return redirect(url_for("dashboard"))

        destination = (os.environ.get("REPORT_EMAIL") or "").strip()
        if not destination:
            flash("No report address is set (REPORT_EMAIL), so there's nowhere to send it.")
            return redirect(url_for("dashboard"))

        buf = build_report_pdf(day)
        status = send_report_email(buf.getvalue(), f"report_{day.isoformat()}.pdf", day)
        if status == "sent":
            flash(f"Report for {fmt_date(day)} emailed to {mask_email(destination)}.")
        elif status == "not_configured":
            flash("Email isn't set up, so the report couldn't be sent.")
        else:
            flash("Couldn't send the report — check the email settings and try again.")
        return redirect(url_for("dashboard"))

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
        if not expected or not same_secret(supplied, f"Bearer {expected}"):
            return jsonify({"error": "unauthorized"}), 401

        closed = _close_open_visits()
        session_day = last_session_day()
        result = {"closed": closed, "date": session_day.isoformat(), "report": "skipped"}

        if is_report_day(session_day):
            # A Monday that falls on a holiday is still a Monday, and the old
            # code cheerfully emailed an empty PDF for it. Below the threshold
            # nothing is built and nothing is sent - the visits are still in the
            # database either way, so the report is downloadable from the
            # dashboard if a quiet day turns out to be worth looking at.
            attended = count_visits(session_day)
            result["visits"] = attended
            if attended > MIN_VISITS_FOR_EMAILED_REPORT:
                buf = build_report_pdf(session_day)
                result["report"] = send_report_email(
                    buf.getvalue(), f"report_{session_day.isoformat()}.pdf", session_day
                )
            else:
                result["report"] = f"skipped_quiet_day({attended})"

        app.logger.info("Nightly close: %s", result)
        return jsonify(result)

    return app


DEFAULT_SESSION_DATES_SHOWN = 10

# How far back the "any other session day" dropdown reaches, in calendar days.
#
# Much further than the ten rows listed above it, because a dropdown costs no
# space on the page: it is one closed control whether it holds five entries or
# five hundred, and it scrolls inside its own menu rather than lengthening the
# dashboard. A year means last term's reports are still one click away instead
# of only reachable by typing a URL from memory.
SESSION_DAYS_OFFERED_WITHIN = 365

# A session day with this many check-ins or fewer doesn't get an emailed report.
# Guards against a holiday Monday producing a blank PDF in the inbox. Nothing is
# lost when it triggers: the day is still downloadable from the dashboard.
MIN_VISITS_FOR_EMAILED_REPORT = 3

# How long a run of failed logins is remembered, and how many are allowed in it.
LOGIN_WINDOW_MINUTES = 15
LOGIN_MAX_ATTEMPTS = 10

# The same, for wrong 4-digit codes at the kiosk. Tighter than the login limit
# because the secret is so much smaller: at five tries per quarter hour, working
# through ten thousand codes would take a month of standing at the tablet. Low
# enough to matter, high enough that a student who mistypes twice and then gets
# it right never notices it exists.
CODE_WINDOW_MINUTES = 15
CODE_MAX_ATTEMPTS = 5


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


def same_secret(supplied, expected) -> bool:
    """Constant-time comparison that survives whatever arrives from the network.

    secrets.compare_digest raises TypeError when either str argument contains a
    non-ASCII character, and every caller here compares something a stranger
    typed. That turned an Arabic-Indic or fullwidth digit - what an iPad set to
    another numeral system produces - into a 500 instead of "that code isn't
    right", and let anyone crash the public cron endpoint with one header. Bytes
    have no such restriction, so encode first.
    """
    try:
        return secrets.compare_digest(str(supplied).encode("utf-8"),
                                      str(expected).encode("utf-8"))
    except (UnicodeEncodeError, AttributeError):
        return False


def safe_next(candidate: str, fallback: str) -> str:
    """A ?next= value that cannot leave this site.

    Checking `startswith("/")` on the raw string is not enough. Werkzeug strips
    tab and newline when it writes the Location header, so "/%09/evil.com"
    passes the check as a relative path and is then emitted as "//evil.com" - a
    protocol-relative URL that sends a freshly logged-in instructor to another
    site, having just typed their password into the real one. A newline instead
    made Werkzeug raise, 500-ing an otherwise successful login.

    So: strip what the header serializer would strip, then validate. Backslashes
    are rejected too because browsers treat "/\\evil.com" as "//evil.com".
    """
    cleaned = (candidate or "").translate({0x09: None, 0x0A: None, 0x0D: None, 0x00: None})
    if not cleaned.startswith("/"):
        return fallback
    if cleaned.startswith("//") or cleaned.startswith("/\\"):
        return fallback
    return cleaned


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
    """The visitor's address, for throttling failed logins.

    The LAST entry of X-Forwarded-For, not the first. The header is a chain that
    each proxy appends to, so the leftmost entry is whatever the *client* sent -
    it is data from the attacker, not about them. Reading it meant a script
    could rotate one header value and guess passwords forever: ten failures,
    change the header, ten more. The rightmost entry is the one written by the
    proxy directly in front of this app, which a client cannot forge.

    This is correct for exactly one trusted proxy (Vercel's edge). Behind two,
    the second-from-last would be the one to read.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()[:100]
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


def _assign_code(db, student_id) -> str:
    """Put a fresh code on a student and return it.

    Wrong-guess history is wiped at the same time. Otherwise a student locked
    out by someone else's guessing would still be locked out after the
    instructor handed them a new code, which is exactly the moment the lock has
    stopped being useful.
    """
    _, code = with_new_code(
        db,
        lambda code: db.execute(
            "UPDATE students SET code = %s WHERE id = %s", (code, student_id)
        ),
    )
    db.commit()
    _clear_code_attempts(student_id)
    return code


def visit_stage(check_in_time, work_done_time) -> str:
    """Which of the three steps a student is up to.

    'out'        - not here, or already collected. Next: Parent Check In.
    'checked_in' - here and working.               Next: Done with Work.
    'work_done'  - finished, waiting to go home.   Next: Parent Check Out.

    Derived from the timestamps rather than stored as a column of its own, so
    there is no way for a stage to disagree with the times it is meant to
    describe. Both arguments come from the open visit, or are None when the
    student hasn't got one.
    """
    if check_in_time is None:
        return "out"
    return "work_done" if work_done_time else "checked_in"


def _code_locked_out(student_id) -> bool:
    """True once someone has guessed wrong at this student too many times.

    Per student rather than per address: the tablet is one device that everyone
    shares, so throttling by IP would let one wrong guess for one student start
    locking out the whole center.

    Fails open if the table isn't there, for the same reason as the login
    throttle - a kiosk nobody can use is worse than an unthrottled one.
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT count(*) AS n FROM code_attempts "
            "WHERE student_id = %s AND attempted_at > %s",
            (student_id, _code_window_start()),
        ).fetchone()
    except psycopg.errors.UndefinedTable:
        db.rollback()
        print("[code] WARNING: no code_attempts table, so wrong student codes are "
              "not being throttled. Run: python setup_db.py", flush=True)
        return False
    return row["n"] >= CODE_MAX_ATTEMPTS


def _code_window_start() -> str:
    return (local_now() - timedelta(minutes=CODE_WINDOW_MINUTES)).isoformat(timespec="seconds")


def _record_failed_code(student_id):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO code_attempts (student_id, attempted_at) VALUES (%s, %s)",
            (student_id, local_now().isoformat(timespec="seconds")),
        )
        # Rows older than the window can never matter again, so pruning here
        # saves needing a cleanup job.
        db.execute("DELETE FROM code_attempts WHERE attempted_at < %s", (_code_window_start(),))
        db.commit()
    except psycopg.errors.UndefinedTable:
        db.rollback()


def _clear_code_attempts(student_id):
    """A correct code wipes the slate, so a couple of fumbled digits don't lock
    a student out of checking themselves in later in the session."""
    db = get_db()
    try:
        db.execute("DELETE FROM code_attempts WHERE student_id = %s", (student_id,))
        db.commit()
    except psycopg.errors.UndefinedTable:
        db.rollback()


def recent_session_days(within_days=SESSION_DAYS_OFFERED_WITHIN):
    """Every session day in the last `within_days` days, newest first.

    Worked out from the calendar rather than from the database, so a session day
    nobody attended is still offered - a report showing an empty Monday is a
    real answer to "did anyone come in?", where a missing entry just looks like
    the system lost it.

    A window rather than a count, so the answer stays "about a year" whatever
    REPORT_DAYS says. Counting instead would mean two session days a week
    reaches back a year and five reaches back five months, silently, with
    nothing on screen explaining why last spring had gone missing.
    """
    today = local_today()
    return [day for day in (today - timedelta(days=n) for n in range(within_days + 1))
            if is_report_day(day)]


def group_by_month(days):
    """[(month label, [dates])] - newest month first, for a grouped dropdown.

    A year of Mondays and Thursdays is a hundred-odd entries. As one flat list
    that is a wall of near-identical dates to scroll; broken into months it is
    something you can aim at.
    """
    months = []
    for day in days:
        label = day.strftime("%B %Y")
        if not months or months[-1][0] != label:
            months.append((label, []))
        months[-1][1].append(day)
    return months


def report_dates(count=DEFAULT_SESSION_DATES_SHOWN):
    """Dates worth offering a report for: today, plus past days that actually
    have check-ins recorded. Newest first.

    This used to be worked out from the calendar - every Monday and Thursday
    going back weeks, regardless of whether the center had met on any of them.
    That offered a download for days that could only ever produce an empty PDF,
    including dates from before the system existed. Asking the database which
    days have visits means the list only ever contains reports with something
    in them.

    Today is offered even when it's empty - but only if the center actually
    meets today, so a Tuesday doesn't sit at the top of the list promising a
    report that could only ever come out blank.

    Days with visits are listed whatever weekday they fall on. The center runs
    Mondays and Thursdays, but if a make-up session ever happens on a Wednesday
    those check-ins are real and the report has something in it, so hiding it
    for not matching the timetable would lose data that exists.
    """
    db = get_db()
    today = local_today()
    rows = db.execute(
        "SELECT DISTINCT check_in_time::date AS day FROM visits "
        "WHERE check_in_time::date <= %s::date "
        "ORDER BY day DESC LIMIT %s",
        (today.isoformat(), count),
    ).fetchall()
    # Trim first, then prepend today. Slicing last meant adding today pushed a
    # real day off the end - and if that day was a make-up session on an unusual
    # weekday, it vanished from the dashboard entirely, because the dropdown
    # beside this list is built from the calendar and only ever offers session
    # weekdays.
    days = [r["day"] for r in rows]
    if today in days or not is_report_day(today):
        return days[:count]
    return [today] + days[:count - 1]


def report_is_pointless(day) -> bool:
    """True for a date that can only ever produce an empty PDF.

    A day the center doesn't run, with nothing recorded on it. Both halves
    matter: a session day is always worth a report even if it turns out empty
    (that is itself the answer), and a day with visits is always worth one no
    matter which weekday it lands on.
    """
    return not is_report_day(day) and count_visits(day) == 0


def count_visits(day) -> int:
    db = get_db()
    return db.execute(
        "SELECT count(*) AS n FROM visits WHERE check_in_time::date = %s::date",
        (day.isoformat(),),
    ).fetchone()["n"]


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

    Both open stages are swept up: a student who never said they were done, and
    a student who did but whose parent never checked them out. Only
    check_out_time is filled in — work_done_time is left alone, so a blank one
    still means "never told us they finished" rather than being back-filled with
    a time nobody observed. email_status is untouched for the same reason: if
    the parent was emailed at pick-up-ready time, that happened, and the
    close-out shouldn't overwrite the record of it.

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
        # "AND check_out_time IS NULL" matters even though the SELECT above only
        # returned open visits: the list is read once and then updated row by
        # row with no commit until the end, and the cron runs at 11 PM local in
        # winter - an hour after closing, with stragglers still plausible. A
        # parent checking out in that window would otherwise have the time they
        # actually arrived overwritten with the 10 PM stamp and the visit marked
        # as nobody-collected-them.
        cur = db.execute(
            "UPDATE visits SET check_out_time = %s, auto_closed = 1 "
            "WHERE id = %s AND check_out_time IS NULL",
            (close_dt.isoformat(timespec="seconds"), visit["id"]),
        )
        closed += cur.rowcount
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
        SELECT s.name AS name, v.check_in_time, v.work_done_time, v.check_out_time,
               v.email_status, v.auto_closed
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
