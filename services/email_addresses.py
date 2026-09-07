"""Strict normalization for addresses crossing an email or HTML boundary."""

from email_validator import EmailNotValidError, validate_email


MAX_EMAIL_SUBJECT_LENGTH = 255


def normalize_valid_email(value):
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 254 or "\r" in candidate or "\n" in candidate:
        return None
    try:
        result = validate_email(
            candidate,
            check_deliverability=False,
            allow_smtputf8=False,
            test_environment=True,
        )
    except EmailNotValidError:
        return None
    return (result.ascii_email or result.normalized).casefold()


def normalize_email_subject(value, *, max_length=MAX_EMAIL_SUBJECT_LENGTH):
    """Return a safe single-line subject, or ``None`` when it is unsafe."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > max_length:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        return None
    return candidate
