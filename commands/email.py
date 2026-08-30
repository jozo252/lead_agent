import imaplib
import os

import click
from flask.cli import with_appcontext
from flask_mail import Message

from extensions import mail


@click.command("test-smtp")
@click.option(
    "--to",
    "recipient",
    required=True,
    help="E-mailová adresa, na ktorú sa odošle test.",
)
@with_appcontext
def test_smtp_command(recipient: str) -> None:
    """Odošle testovací e-mail bez zmeny firmy alebo CRM dát."""
    recipient = recipient.strip()

    if "@" not in recipient:
        raise click.ClickException("Zadaj platnú e-mailovú adresu.")

    try:
        mail.send(
            Message(
                subject="LeadFlow – SMTP test",
                recipients=[recipient],
                body=(
                    "SMTP test z LeadFlow prebehol úspešne. "
                    "Tento e-mail nezmenil žiadne firemné ani CRM dáta."
                ),
            )
        )
    except Exception as exc:
        raise click.ClickException(f"SMTP test zlyhal: {exc}") from exc

    click.echo(f"Testovací e-mail bol odoslaný na {recipient}.")


@click.command("test-imap")
@with_appcontext
def test_imap_command() -> None:
    """Overí IMAP prihlásenie bez čítania alebo zmeny CRM dát."""
    server = os.environ.get("IMAP_SERVER", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    username = os.environ.get("IMAP_USERNAME")
    password = os.environ.get("IMAP_PASSWORD")

    if not username or not password:
        raise click.ClickException(
            "Chýba IMAP_USERNAME alebo IMAP_PASSWORD v .env."
        )

    connection = None
    try:
        connection = imaplib.IMAP4_SSL(server, port)
        connection.login(username, password)
        status, data = connection.select("inbox", readonly=True)

        if status != "OK":
            raise click.ClickException("IMAP sa nepripojil k inboxu.")

        message_count = int(data[0]) if data and data[0] else 0
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"IMAP test zlyhal: {exc}") from exc
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass

    click.echo(
        f"IMAP pripojenie funguje. Inbox obsahuje {message_count} správ."
    )
