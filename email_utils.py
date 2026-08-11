"""Sending the ready-for-pick-up email to parents, and the nightly report.

Returns a status string that gets stored on the visit row. When SMTP isn't
configured the message is logged instead of sent, so check-in/check-out keeps
working during setup.

Nothing here writes to disk. The old version appended to data/email_log.txt,
which is a read-only path on most hosts - and it did so on the *failure* path,
so a failed send turned into a crashed checkout instead of a graceful one.
Logs go to stdout, which the host captures.

Parent addresses are deliberately never logged. They're encrypted at rest for a
reason, and writing them into a log in the clear would undo that.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage

from crypto_utils import email_recipients
from time_utils import fmt_time, local_now


def _log_fallback(student_name: str, recipient_count: int, reason: str, detail: str = ""):
    """Record that a message wasn't sent, without recording who it was for."""
    stamp = local_now().isoformat(timespec="seconds")
    suffix = f" | {detail}" if detail else ""
    print(f"[{stamp}] email {reason}: {student_name} -> "
          f"{recipient_count} recipient(s){suffix}", flush=True)


def _smtp_port() -> int:
    """The SMTP port, falling back to 587 rather than raising.

    int() on a typo'd value raised ValueError from _smtp_settings, which is
    called *outside* the try in both senders - so a stray character in the
    environment 500'd the request after the visit had already been committed as
    finished, leaving it marked done with no email and no recorded status.
    """
    raw = (os.environ.get("SMTP_PORT") or "").strip()
    if not raw:
        return 587
    try:
        return int(raw)
    except ValueError:
        print(f"[email] WARNING: SMTP_PORT={raw!r} is not a number; using 587.", flush=True)
        return 587


def _smtp_settings():
    return {
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "port": _smtp_port(),
        "username": os.environ.get("SMTP_USERNAME", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from_email": (os.environ.get("FROM_EMAIL", "").strip()
                       or os.environ.get("SMTP_USERNAME", "").strip()),
        "from_name": os.environ.get("FROM_NAME", "").strip()
                     or os.environ.get("CENTER_NAME", "Kumon").strip(),
    }


# Comfortably under the host's own limit on how long a request may run (30s, set
# in vercel.json). The check-out email is sent inside the student's request, so
# an SMTP timeout longer than that would take the whole request down with it -
# the visit is already recorded as checked out by then, but the student would
# see "couldn't reach the check-in system" for something that actually worked.
SMTP_TIMEOUT_SECONDS = 10


def _send(message, recipients, cfg) -> int:
    """Deliver one message per recipient over SMTP. Returns how many were sent.

    Each recipient is attempted separately so one bad address doesn't discard
    the others. This used to let the exception escape the loop, so a roster with
    two parent addresses where the second was refused reported the whole send as
    a failure - the student was told nobody had been notified while the first
    parent was already driving over. Connection-level failures still raise,
    because then nothing was delivered and the caller needs to know.
    """
    context = ssl.create_default_context()
    if cfg["port"] == 465:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"],
                                  timeout=SMTP_TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT_SECONDS)
    delivered = 0
    with server:
        if cfg["port"] != 465:
            server.starttls(context=context)
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"])
        for address in recipients:
            del message["To"]
            message["To"] = address
            try:
                server.send_message(message)
                delivered += 1
            except smtplib.SMTPRecipientsRefused:
                # This address is bad; the others may be fine. Never log the
                # address itself - that is the thing encryption at rest exists
                # to protect.
                print("[email] one recipient refused; continuing with the rest", flush=True)
    return delivered


def send_work_done_email(to_email: str, student_name: str, finished_time) -> str:
    """Tell a parent their child has finished and can be collected.

    This is the one email the system sends, and it goes out when the student
    taps "Done with Work" - not at check-out. That is the moment the parent can
    act on: they are somewhere else, and this is what tells them to set off. By
    check-out time they are standing at the desk doing it themselves, so an
    email then would be telling them something they just did.

    Returns a status string: 'sent', 'partial', 'not_configured', 'no_address',
    or 'failed'. 'partial' means at least one parent address was reached and at
    least one was refused - worth telling the instructor about, and distinctly
    not the same as nobody being told.
    """
    recipients = email_recipients(to_email)
    center = os.environ.get("CENTER_NAME", "Kumon").strip() or "Kumon"
    when = fmt_time(finished_time)

    subject = f"{center}: {student_name} has finished and is ready for pick-up"
    body = (
        f"{student_name} finished their work at {center} at {when} on "
        f"{finished_time.strftime('%A, %B')} {finished_time.day}, {finished_time.year}, "
        f"and is ready to be picked up.\n\n"
        f"Please check {student_name} out at the kiosk when you collect them.\n\n"
        f"This is an automatic message — please don't reply to it.\n"
    )

    if not recipients:
        _log_fallback(student_name, 0, "no_address")
        return "no_address"

    cfg = _smtp_settings()
    if not (cfg["host"] and cfg["from_email"]):
        _log_fallback(student_name, len(recipients), "not_configured")
        return "not_configured"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f'{cfg["from_name"]} <{cfg["from_email"]}>' if cfg["from_name"] else cfg["from_email"]
    # One address per message: parents shouldn't see each other's addresses.
    message["To"] = recipients[0]
    message.set_content(body)

    try:
        delivered = _send(message, recipients, cfg)
    except Exception as exc:  # noqa: BLE001 - log and keep the app running
        _log_fallback(student_name, len(recipients), "failed", type(exc).__name__)
        return "failed"

    if delivered == 0:
        _log_fallback(student_name, len(recipients), "failed", "all recipients refused")
        return "failed"
    if delivered < len(recipients):
        _log_fallback(student_name, len(recipients), "partial",
                      f"{delivered} of {len(recipients)} delivered")
        return "partial"
    return "sent"


def send_report_email(pdf_bytes: bytes, filename: str, report_date) -> str:
    """Email the nightly PDF report to REPORT_EMAIL.

    With reports built on demand rather than saved to disk, this is what makes
    them permanent: the instructor's inbox becomes the archive, and the app
    stores nothing. Returns 'sent', 'not_configured', or 'failed'.
    """
    to_address = (os.environ.get("REPORT_EMAIL") or "").strip()
    cfg = _smtp_settings()
    if not to_address or not (cfg["host"] and cfg["from_email"]):
        print(f"[report] not emailed ({report_date}): REPORT_EMAIL or SMTP not configured",
              flush=True)
        return "not_configured"

    center = os.environ.get("CENTER_NAME", "Kumon").strip() or "Kumon"
    message = EmailMessage()
    message["Subject"] = f"{center}: check-in/check-out report for {report_date}"
    message["From"] = f'{cfg["from_name"]} <{cfg["from_email"]}>' if cfg["from_name"] else cfg["from_email"]
    message["To"] = to_address
    message.set_content(
        f"Attached is the {center} check-in/check-out report for {report_date}.\n\n"
        f"Keep this email — the report isn't stored on the server.\n"
    )
    message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)

    try:
        _send(message, [to_address], cfg)
        print(f"[report] emailed {filename}", flush=True)
        return "sent"
    except Exception as exc:  # noqa: BLE001
        print(f"[report] failed to email {filename}: {type(exc).__name__}", flush=True)
        return "failed"
