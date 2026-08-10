import os
import re
from cryptography.fernet import Fernet, InvalidToken


class EncryptionNotConfigured(RuntimeError):
    pass


def _get_fernet():
    key = os.environ.get("EMAIL_ENCRYPTION_KEY") or os.environ.get("PHONE_ENCRYPTION_KEY")
    if not key:
        raise EncryptionNotConfigured(
            "EMAIL_ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
            "and put it in your .env file."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def assert_configured():
    """Raise at startup if the key is missing or unusable.

    Without this the app boots happily and then fails with a 500 on exactly the
    two paths that decrypt - the dashboard, and check-out. Check-out is the bad
    one: it claims the visit before it decrypts, so the student is recorded as
    checked out and *then* shown an error, with no email sent. A missing
    environment variable should not be discoverable only by a student pressing
    a button.
    """
    try:
        _get_fernet()
    except EncryptionNotConfigured:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed key, whatever the cause
        raise EncryptionNotConfigured(
            f"EMAIL_ENCRYPTION_KEY is set but unusable ({type(exc).__name__}). "
            "It must be the 44-character Fernet key you generated, copied exactly "
            "- no quotes, no spaces, no line break, and the trailing '=' included."
        ) from exc


def encrypt_email(email: str) -> bytes:
    f = _get_fernet()
    return f.encrypt(email.encode("utf-8"))


def decrypt_email(token: bytes) -> str:
    if not token:
        return ""  # student imported without an address on file
    f = _get_fernet()
    try:
        return f.decrypt(bytes(token)).decode("utf-8")
    except InvalidToken:
        return "(unable to decrypt - check EMAIL_ENCRYPTION_KEY)"


def mask_email(email: str) -> str:
    """Show just enough to recognise the address without exposing it in full:
    parent@example.com -> p****@example.com. Handles the comma-separated form too."""
    if not email:
        return "****"
    masked = []
    for part in str(email).split(","):
        part = part.strip()
        if "@" not in part:
            masked.append("****")
            continue
        local, _, domain = part.partition("@")
        head = local[0] if local else "*"
        masked.append(f"{head}{'*' * max(len(local) - 1, 3)}@{domain}")
    return ", ".join(masked)


# Deliberately permissive: this catches typos and blank cells without rejecting
# the unusual-but-valid addresses a real roster contains.
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")


def normalize_email(raw) -> str | None:
    """Clean up an address (or a comma-separated list of them) into the form we
    store. Returns None if nothing usable is present."""
    if raw is None:
        return None

    # Rosters separate multiple addresses with commas, semicolons, or slashes.
    candidates = re.split(r"[,;/]+", str(raw))
    cleaned = []
    for candidate in candidates:
        candidate = candidate.strip().strip("<>").lower()
        if not candidate:
            continue
        if not _EMAIL_RE.match(candidate):
            return None  # a malformed address is a data problem worth surfacing
        if candidate not in cleaned:
            cleaned.append(candidate)

    return ", ".join(cleaned) if cleaned else None


def email_recipients(stored: str) -> list[str]:
    """Split a stored value into the list of addresses to actually send to.
    Anything that isn't a plausible address (a blank record, or the decryption
    error placeholder) is dropped rather than handed to the mail server."""
    if not stored:
        return []
    return [part.strip() for part in str(stored).split(",")
            if part.strip() and _EMAIL_RE.match(part.strip())]
