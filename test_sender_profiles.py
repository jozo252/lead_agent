import email
import os
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from flask_mail import Message

from extensions import mail
from services.campaigns import OPT_OUT_FOOTER
from services.sender_profiles import (
    NETWORK_TIMEOUT_SECONDS,
    SenderProfileError,
    append_profile_signature,
    fetch_profile_messages,
    profile_readiness,
    send_profile_message,
)


class SenderProfileTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            MAIL_DEFAULT_SENDER="legacy@example.test",
            MAIL_SERVER="legacy-smtp.example.test",
            MAIL_USERNAME="legacy@example.test",
            MAIL_PASSWORD="legacy-secret",
            MAIL_SUPPRESS_SEND=False,
            SENDER_PROFILE_SETTINGS={},
        )
        mail.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        self.profile = self.add_profile("MG_STAV", "stav@example.test")

    def tearDown(self):
        self.context.pop()
        self.environment.stop()

    def add_profile(self, key, address):
        self.app.config["SENDER_PROFILE_SETTINGS"][key] = {
            "MAIL_SERVER": f"smtp-{key.lower()}.example.test",
            "MAIL_PORT": "587",
            "MAIL_USE_TLS": "true",
            "MAIL_USE_SSL": "false",
            "MAIL_USERNAME": address,
            "MAIL_PASSWORD": f"{key}-smtp-test-secret",
            "IMAP_SERVER": f"imap-{key.lower()}.example.test",
            "IMAP_PORT": "993",
            "IMAP_USERNAME": address,
            "IMAP_PASSWORD": f"{key}-imap-test-secret",
        }
        return SimpleNamespace(
            id=key,
            name=key,
            config_key=key,
            sender_name=f"Firma {key}",
            sender_email=address,
            signature=f"S pozdravom\nFirma {key}",
            enabled=True,
        )

    def message(self, **kwargs):
        return Message(
            subject="Test ponuky", recipients=["recipient@example.test"],
            body=f"Test správy.\n\n{OPT_OUT_FOOTER}", **kwargs,
        )

    def settings(self):
        return self.app.config["SENDER_PROFILE_SETTINGS"][self.profile.config_key]

    def inbox_message(self, uid, date="Sun, 06 Sep 2026 10:30:00 +0200"):
        message = EmailMessage()
        message["From"] = "Príjemca <recipient@example.test>"
        message["To"] = "stav@example.test"
        message["Subject"] = "Re: Test ponuky"
        message["Message-ID"] = f"<reply-{uid}@example.test>"
        message["In-Reply-To"] = "<sent@example.test>"
        if date is not None:
            message["Date"] = date
        message.set_content("Ďakujem, mám záujem.")
        return message.as_bytes()

    def imap(self, raw_messages):
        connection = MagicMock()
        connection.login.return_value = ("OK", [b"logged in"])
        connection.select.return_value = ("OK", [str(len(raw_messages)).encode()])

        def uid(command, *args):
            if command == "search":
                return "OK", [b" ".join(raw_messages)]
            if command == "fetch":
                identifier = args[0]
                raw = raw_messages[identifier]
                metadata = b"1 (UID " + identifier + b" BODY[] {" + str(len(raw)).encode() + b"}"
                return "OK", [(metadata, raw), b")"]
            raise AssertionError("Unexpected IMAP command")

        connection.uid.side_effect = uid
        return connection

    def test_profiles_use_separate_accounts_without_mutating_shared_configuration(self):
        other = self.add_profile("ELEKTRO", "elektro@example.test")
        first_smtp, second_smtp = MagicMock(), MagicMock()
        shared_config = self.app.config.copy()
        shared_mail = self.app.extensions["mail"]
        first_message, second_message = self.message(), self.message()
        with patch("services.sender_profiles.smtplib.SMTP", side_effect=[first_smtp, second_smtp]) as smtp:
            send_profile_message(first_message, self.profile)
            send_profile_message(second_message, other)

        self.assertEqual(self.app.config, shared_config)
        self.assertIs(self.app.extensions["mail"], shared_mail)
        self.assertEqual(smtp.call_args_list[0].args, ("smtp-mg_stav.example.test", 587))
        self.assertEqual(smtp.call_args_list[1].args, ("smtp-elektro.example.test", 587))
        self.assertEqual(smtp.call_args_list[0].kwargs["timeout"], NETWORK_TIMEOUT_SECONDS)
        first_smtp.login.assert_called_once_with("stav@example.test", "MG_STAV-smtp-test-secret")
        second_smtp.login.assert_called_once_with("elektro@example.test", "ELEKTRO-smtp-test-secret")
        first_smtp.set_debuglevel.assert_called_once_with(0)
        self.assertIsNotNone(first_smtp.starttls.call_args.kwargs["context"])
        sent = email.message_from_bytes(first_smtp.sendmail.call_args.args[2], policy=email.policy.default)
        self.assertEqual(sent["From"], "Firma MG_STAV <stav@example.test>")
        self.assertEqual(sent["Reply-To"], "stav@example.test")
        self.assertEqual(first_message.body.count(self.profile.signature), 1)
        self.assertTrue(first_message.body.endswith(OPT_OUT_FOOTER))
        self.assertIn("Firma ELEKTRO", second_message.body)
        self.assertNotIn("Firma MG_STAV", second_message.body)

    def test_sender_and_reply_to_cannot_be_overridden_by_message_headers(self):
        message = self.message(
            sender="other@example.test", reply_to="wrong@example.test",
            extra_headers={"From": "wrong@example.test", "Reply-To": "wrong@example.test", "In-Reply-To": "<prior@example.test>"},
        )
        with patch("services.sender_profiles.smtplib.SMTP") as smtp:
            send_profile_message(message, self.profile)
        sent = email.message_from_bytes(smtp.return_value.sendmail.call_args.args[2], policy=email.policy.default)
        self.assertEqual(sent.get_all("From"), ["Firma MG_STAV <stav@example.test>"])
        self.assertEqual(sent.get_all("Reply-To"), ["stav@example.test"])
        self.assertEqual(sent["In-Reply-To"], "<prior@example.test>")

    def test_legacy_send_keeps_existing_sender_path(self):
        message = self.message()
        original_body = message.body
        with patch("services.sender_profiles.mail.send") as legacy_send:
            send_profile_message(message)
        legacy_send.assert_called_once_with(message)
        self.assertEqual(message.body, original_body)

    def test_subject_and_recipient_header_injection_are_blocked_before_transport(self):
        malicious_messages = [
            Message(
                subject="Ponuka\r\nBcc: hidden@example.test",
                recipients=["recipient@example.test"],
                body="Text",
            ),
            Message(
                subject="Ponuka",
                recipients=["recipient@example.test\r\nBcc: hidden@example.test"],
                body="Text",
            ),
        ]
        with patch("services.sender_profiles.mail.send") as legacy_send:
            for message in malicious_messages:
                with self.subTest(message=message), self.assertRaises(SenderProfileError):
                    send_profile_message(message)
        legacy_send.assert_not_called()

    def test_signatures_are_idempotent_and_precede_opt_out(self):
        message = self.message(html=f"<html><body><p>Ponuka.</p><p>{OPT_OUT_FOOTER}</p></body></html>")
        self.profile.signature = "Firma <Stav>\nKontakt"
        with patch("services.sender_profiles.smtplib.SMTP"):
            send_profile_message(message, self.profile)
            send_profile_message(message, self.profile)
        self.assertEqual(message.body.count(self.profile.signature), 1)
        self.assertLess(message.body.index(self.profile.signature), message.body.index(OPT_OUT_FOOTER))
        self.assertEqual(message.html.count("data-sender-profile-signature"), 1)
        self.assertIn("Firma &lt;Stav&gt;<br>Kontakt", message.html)
        self.assertLess(message.html.index("data-sender-profile-signature"), message.html.index(OPT_OUT_FOOTER))
        self.assertEqual(append_profile_signature("", "Podpis"), "Podpis")

    def test_disabled_profile_refuses_send_and_inbox_before_connecting(self):
        self.profile.enabled = False
        with patch("services.sender_profiles.smtplib.SMTP") as smtp, patch("services.sender_profiles.imaplib.IMAP4_SSL") as imap:
            with self.assertRaisesRegex(SenderProfileError, "vypnutý"):
                send_profile_message(self.message(), self.profile)
            with self.assertRaisesRegex(SenderProfileError, "vypnutý"):
                fetch_profile_messages(self.profile)
        smtp.assert_not_called()
        imap.assert_not_called()

    def test_missing_profile_credentials_never_use_legacy_configuration(self):
        self.app.config["SENDER_PROFILE_SETTINGS"][self.profile.config_key] = {}
        with patch("services.sender_profiles.smtplib.SMTP") as smtp:
            with self.assertRaisesRegex(SenderProfileError, "MAIL_PASSWORD"):
                send_profile_message(self.message(), self.profile)
        smtp.assert_not_called()
        self.assertIn("Chýba MAIL_PASSWORD.", profile_readiness(self.profile))

    def test_environment_settings_are_profile_specific_and_loaded_at_runtime(self):
        settings = self.settings().copy()
        self.app.config["SENDER_PROFILE_SETTINGS"] = {}
        values = {f"SENDER_MG_STAV_{key}": value for key, value in settings.items()}
        with patch.dict(os.environ, values):
            self.assertEqual(profile_readiness(self.profile, require_imap=True), [])
            with patch("services.sender_profiles.smtplib.SMTP") as smtp:
                send_profile_message(self.message(), self.profile)
                os.environ["SENDER_MG_STAV_MAIL_PASSWORD"] = "rotated-test-secret"
                send_profile_message(self.message(), self.profile)
            self.assertEqual(smtp.return_value.login.call_args.args[1], "rotated-test-secret")

    def test_mismatched_mailbox_identity_is_rejected(self):
        self.settings()["IMAP_USERNAME"] = "another@example.test"
        self.assertEqual(profile_readiness(self.profile), [])
        self.assertTrue(any("IMAP_USERNAME musí" in issue for issue in profile_readiness(self.profile, require_imap=True)))
        with patch("services.sender_profiles.imaplib.IMAP4_SSL") as imap:
            with self.assertRaisesRegex(SenderProfileError, "IMAP_USERNAME musí"):
                fetch_profile_messages(self.profile)
        imap.assert_not_called()
        self.settings()["MAIL_USERNAME"] = "another@example.test"
        self.assertTrue(any("MAIL_USERNAME musí" in issue for issue in profile_readiness(self.profile)))

    def test_readiness_rejects_header_injection_bad_ports_and_unencrypted_smtp(self):
        self.profile.sender_name = "Firma\r\nBcc: hidden@example.test"
        self.profile.sender_email = "stav@example.test\r\n"
        # Leading/trailing whitespace is normalized; embedded header content is not.
        self.profile.sender_email += "Bcc: hidden@example.test"
        self.settings()["MAIL_PORT"] = "not-a-port-with-secret"
        self.settings()["MAIL_USE_TLS"] = "false"
        issues = " ".join(profile_readiness(self.profile))
        self.assertIn("meno odosielateľa", issues)
        self.assertIn("e-mail odosielateľa", issues)
        self.assertIn("MAIL_PORT", issues)
        self.assertIn("TLS alebo SSL", issues)
        self.assertNotIn("not-a-port-with-secret", issues)

    def test_ssl_profile_uses_verified_ssl_and_no_starttls(self):
        self.settings().update(MAIL_USE_SSL="true", MAIL_USE_TLS="false", MAIL_PORT="465")
        with patch("services.sender_profiles.smtplib.SMTP_SSL") as smtp_ssl, patch("services.sender_profiles.smtplib.SMTP") as smtp:
            send_profile_message(self.message(), self.profile)
        smtp.assert_not_called()
        smtp_ssl.assert_called_once()
        self.assertEqual(smtp_ssl.call_args.args, ("smtp-mg_stav.example.test", 465))
        self.assertTrue(smtp_ssl.call_args.kwargs["context"].check_hostname)
        smtp_ssl.return_value.starttls.assert_not_called()

    def test_tls_failure_is_sanitized_and_closes_connection_without_sending(self):
        with patch("services.sender_profiles.smtplib.SMTP") as smtp:
            smtp.return_value.starttls.side_effect = RuntimeError("MG_STAV-smtp-test-secret")
            with self.assertRaises(SenderProfileError) as raised:
                send_profile_message(self.message(), self.profile)
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        smtp.return_value.close.assert_called_once()
        smtp.return_value.login.assert_not_called()
        smtp.return_value.sendmail.assert_not_called()

    def test_smtp_timeout_keeps_uncertain_outcome_and_hides_error_details(self):
        with patch("services.sender_profiles.smtplib.SMTP") as smtp:
            smtp.return_value.sendmail.side_effect = TimeoutError("provider leaked MG_STAV-smtp-test-secret")
            with self.assertRaisesRegex(SenderProfileError, "Pred opakovaním skontroluj") as raised:
                send_profile_message(self.message(), self.profile)
        self.assertNotIn("test-secret", str(raised.exception))

    def test_fetch_reads_complete_matching_set_readonly_without_filtering_date_headers(self):
        connection = self.imap({b"101": self.inbox_message("101"), b"105": self.inbox_message("105", date=None)})
        since = datetime(2026, 9, 6, 1, 30, tzinfo=timezone(timedelta(hours=2)))
        with patch("services.sender_profiles.imaplib.IMAP4_SSL", return_value=connection) as imap:
            result = fetch_profile_messages(self.profile, since_datetime=since, max_messages=2)
        imap.assert_called_once()
        self.assertEqual(imap.call_args.kwargs["timeout"], NETWORK_TIMEOUT_SECONDS)
        self.assertTrue(imap.call_args.kwargs["ssl_context"].check_hostname)
        connection.login.assert_called_once_with("stav@example.test", "MG_STAV-imap-test-secret")
        connection.select.assert_called_once_with("inbox", readonly=True)
        connection.uid.assert_any_call("search", None, "SINCE", "04-Sep-2026")
        connection.uid.assert_any_call("fetch", b"101", "(BODY.PEEK[])")
        connection.uid.assert_any_call("fetch", b"105", "(BODY.PEEK[])")
        connection.logout.assert_called_once()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["received_at"], datetime(2026, 9, 6, 8, 30))
        self.assertIsNone(result[1]["received_at"])
        self.assertEqual(result[0]["thread_message_ids"], ["<sent@example.test>"])

    def test_empty_successful_search_is_the_only_empty_batch(self):
        connection = self.imap({})
        with patch("services.sender_profiles.imaplib.IMAP4_SSL", return_value=connection):
            self.assertEqual(fetch_profile_messages(self.profile), [])
        connection.uid.assert_called_once_with("search", None, "ALL")

    def test_cap_refuses_partial_sync_before_any_fetch(self):
        connection = self.imap({b"1": self.inbox_message("1"), b"2": self.inbox_message("2")})
        with patch("services.sender_profiles.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaisesRegex(SenderProfileError, "limit úplnej"):
                fetch_profile_messages(self.profile, max_messages=1)
        connection.uid.assert_called_once_with("search", None, "ALL")
        connection.logout.assert_called_once()

    def test_failed_or_missing_search_response_cannot_look_like_empty_mailbox(self):
        for response in (("NO", [b""]), ("OK", []), ("OK", [None]), ("OK", [b"1 junk"]), ("OK", [b"1 1"])):
            with self.subTest(response=response):
                connection = self.imap({})
                connection.uid.side_effect = None
                connection.uid.return_value = response
                with patch("services.sender_profiles.imaplib.IMAP4_SSL", return_value=connection):
                    with self.assertRaises(SenderProfileError):
                        fetch_profile_messages(self.profile)
                connection.logout.assert_called_once()

    def test_partial_fetch_failure_raises_instead_of_returning_earlier_messages(self):
        connection = self.imap({b"1": self.inbox_message("1"), b"2": self.inbox_message("2")})
        normal_uid = connection.uid.side_effect

        def uid(command, *args):
            if command == "fetch" and args[0] == b"2":
                return "NO", []
            return normal_uid(command, *args)

        connection.uid.side_effect = uid
        with patch("services.sender_profiles.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(SenderProfileError):
                fetch_profile_messages(self.profile)
        connection.logout.assert_called_once()

    def test_corrupt_fetch_size_and_wrong_uid_fail_closed(self):
        raw = self.inbox_message("1")
        for data in (None, [None], [(b"1 (UID 1 BODY[] {999999}", raw), b")"], [(f"1 (UID 99 BODY[] {{{len(raw)}}}".encode(), raw), b")"]):
            with self.subTest(data_kind=type(data).__name__):
                connection = self.imap({b"1": raw})
                connection.uid.side_effect = [("OK", [b"1"]), ("OK", data)]
                with patch("services.sender_profiles.imaplib.IMAP4_SSL", return_value=connection):
                    with self.assertRaises(SenderProfileError):
                        fetch_profile_messages(self.profile)

    def test_uid_after_literal_is_accepted_when_full_payload_matches(self):
        raw = self.inbox_message("1")
        connection = self.imap({b"1": raw})
        connection.uid.side_effect = [
            ("OK", [b"1"]),
            ("OK", [(f"1 (BODY[] {{{len(raw)}}}".encode(), raw), b" UID 1)"]),
        ]
        with patch("services.sender_profiles.imaplib.IMAP4_SSL", return_value=connection):
            self.assertEqual(len(fetch_profile_messages(self.profile)), 1)

    def test_parse_failure_never_returns_partial_batch(self):
        connection = self.imap({b"1": self.inbox_message("1"), b"2": b"This is not a complete email"})
        with patch("services.sender_profiles.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(SenderProfileError):
                fetch_profile_messages(self.profile)
        connection.logout.assert_called_once()

    def test_imap_login_and_parser_errors_do_not_expose_secrets(self):
        connection = self.imap({b"1": self.inbox_message("1")})
        connection.login.side_effect = RuntimeError("MG_STAV-imap-test-secret")
        with patch("services.sender_profiles.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(SenderProfileError) as raised:
                fetch_profile_messages(self.profile)
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        connection.logout.assert_called_once()

    def test_incomplete_imap_profile_cannot_fall_back_to_global_mailbox(self):
        self.app.config.update(IMAP_SERVER="legacy-imap.example.test", IMAP_USERNAME="legacy@example.test", IMAP_PASSWORD="legacy-imap-secret")
        self.settings()["IMAP_PASSWORD"] = None
        with patch("services.sender_profiles.imaplib.IMAP4_SSL") as imap:
            with self.assertRaisesRegex(SenderProfileError, "IMAP_PASSWORD"):
                fetch_profile_messages(self.profile)
        imap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
