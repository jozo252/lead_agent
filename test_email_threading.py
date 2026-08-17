import unittest
from email.message import EmailMessage

from email_checker_service import (
    extract_thread_message_ids,
    find_thread_email_ids,
)


class FakeImapConnection:
    def __init__(self):
        self.calls = []

    def search(self, *args):
        self.calls.append(args)
        header = args[2]

        if header == "In-Reply-To":
            return "OK", [b"2 5"]

        return "OK", [b"5"]


class EmailThreadingTests(unittest.TestCase):
    def test_extracts_message_ids_from_reply_headers(self):
        message = EmailMessage()
        message["In-Reply-To"] = "<first@example.test>"
        message["References"] = (
            "<older@example.test> <first@example.test>"
        )

        self.assertEqual(
            extract_thread_message_ids(message),
            {"<first@example.test>", "<older@example.test>"},
        )

    def test_finds_and_sorts_imap_messages_referencing_thread(self):
        connection = FakeImapConnection()

        email_ids = find_thread_email_ids(
            connection,
            ["<outbound@example.test>"],
        )

        self.assertEqual(email_ids, [b"5", b"2"])
        self.assertEqual(len(connection.calls), 2)


if __name__ == "__main__":
    unittest.main()
