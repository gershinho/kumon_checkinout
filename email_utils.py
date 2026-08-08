"""Sending the check-out notification email to parents, and the nightly report.

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


def _smtp_settings():
    return {
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "port": int(os.environ.get("SMTP_PORT") or 587),
        "username": os.environ.get("SMTP_USERNAME", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from_email": (os.environ.get("FROM_EMAIL", "").strip()
                       or os.environ.get("SMTP_USERNAME", "").strip()),
        "from_name": os.environ.get("FROM_NAME", "").strip()
                     or os.environ.get("CENTER_NAME", "Kumon").strip(),
    }


def _send(message, recipients, cfg):
    """Deliver one message per recipient over SMTP."""
    context = ssl.create_default_context()
    if cfg["port"] == 465:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20, context=context)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
    with server:
        if cfg["port"] != 465:
            server.starttls(context=context)
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"])
        for address in recipients:
            del message["To"]
            message["To"] = address
            server.send_message(message)


def send_checkout_email(to_email: str, student_name: str, checkout_time) -> str:
    """Send the check-out notification. Returns a status string:
    'sent', 'not_configured', 'no_address', or 'failed'.
    """
    recipients = email_recipients(to_email)
    center = os.environ.get("CENTER_NAME", "Kumon").strip() or "Kumon"
    when = fmt_time(checkout_time)

    subject = f"{center}: {student_name} checked out at {when}"
    body = (
        f"{student_name} checked out of {center} at {when} on "
        f"{checkout_time.strftime('%A, %B')} {checkout_time.day}, {checkout_time.year}.\n\n"
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
        _send(message, recipients, cfg)
        return "sent"
    except Exception as exc:  # noqa: BLE001 - log and keep the app running
        _log_fallback(student_name, len(recipients), "failed", type(exc).__name__)
        return "failed"


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
