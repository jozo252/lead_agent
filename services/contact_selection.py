"""Pure helpers for selecting safe email contacts."""

from email_validator import EmailNotValidError, validate_email


def normalized_contact_email(contact):
    """Return a normalized syntactically valid email, otherwise ``None``."""
    value = str(getattr(contact, "value", "") or "").strip()
    if not value:
        return None

    try:
        result = validate_email(value, check_deliverability=False)
    except EmailNotValidError:
        return None
    return result.normalized


def verified_contact_matches(contact, recipient_email):
    """Return whether a verified contact safely matches the delivery address."""
    if not bool(getattr(contact, "is_verified", False)):
        return False
    contact_email = normalized_contact_email(contact)
    if contact_email is None:
        return False
    try:
        delivery_email = validate_email(
            str(recipient_email or "").strip(),
            check_deliverability=False,
        ).normalized
    except EmailNotValidError:
        return False
    return contact_email.casefold() == delivery_email.casefold()


def email_contact_sort_key(contact):
    """Rank verified contacts first, then primary and confidence."""
    confidence = getattr(contact, "confidence_score", None)
    try:
        confidence = float(confidence or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        bool(getattr(contact, "is_verified", False)),
        bool(getattr(contact, "is_primary", False)),
        confidence,
    )


def select_email_contact(contacts, *, require_verified=False):
    """Select the best valid email contact from an iterable.

    ``require_verified=True`` is intended for automated sending. It prevents an
    unverified address from being selected even when it is marked as primary.
    """
    candidates = []
    for contact in contacts:
        contact_type = str(getattr(contact, "contact_type", "") or "").casefold()
        if contact_type != "email" or normalized_contact_email(contact) is None:
            continue
        if require_verified and not bool(getattr(contact, "is_verified", False)):
            continue
        candidates.append(contact)

    if not candidates:
        return None
    return max(candidates, key=email_contact_sort_key)
