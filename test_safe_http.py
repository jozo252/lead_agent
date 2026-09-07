import unittest
import time
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services.safe_http import SafeHttpError, normalize_http_url, safe_http_get
from services.rpo_sync import (
    RPO_BASE_URL,
    RpoSyncError,
    extract_next_url,
    fetch_validated_website_result,
    request_page,
)


class FakeResponse:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status = status
        self._body = body
        self._headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.headers = Message()
        for key, value in self._headers.items():
            self.headers[key] = value
        self._offset = 0

    def getheaders(self):
        return list(self._headers.items())

    def read(self, amount=None):
        if amount is None:
            result = self._body[self._offset:]
            self._offset = len(self._body)
            return result
        result = self._body[self._offset:self._offset + amount]
        self._offset += len(result)
        return result

    def read1(self, amount=None):
        return self.read(amount)


class SlowDripFakeResponse(FakeResponse):
    def read1(self, amount=None):
        return self.read(1 if amount else amount)


class FakeConnection:
    instances = []
    responses = []

    def __init__(self, hostname, connect_ip, port, timeout):
        self.hostname = hostname
        self.connect_ip = connect_ip
        self.port = port
        self.timeout = timeout
        self.request_args = None
        self.__class__.instances.append(self)

    def request(self, *args, **kwargs):
        self.request_args = (args, kwargs)

    def getresponse(self):
        return self.__class__.responses.pop(0)

    def close(self):
        pass


class SafeHttpTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances = []
        FakeConnection.responses = []

    def test_private_metadata_userinfo_and_nonstandard_ports_are_blocked(self):
        for url in (
            "http://127.0.0.1/admin",
            "http://[::1]/",
            "http://169.254.169.254/latest/meta-data/",
            "https://user:password@example.com/",
            "https://example.com:8443/",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(SafeHttpError):
                safe_http_get(url)

    @patch("services.safe_http.socket.getaddrinfo")
    def test_dns_resolution_is_bounded_by_total_timeout(self, resolve):
        def delayed_resolution(*_args, **_kwargs):
            time.sleep(0.5)
            return [(2, 1, 6, "", ("93.184.216.34", 443))]

        resolve.side_effect = delayed_resolution
        started = time.monotonic()
        with self.assertRaisesRegex(SafeHttpError, "DNS preklad"):
            safe_http_get("https://slow-dns.example/", timeout=0.05)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.3)

    def test_link_normalizer_allows_only_canonical_public_http_urls(self):
        self.assertEqual(
            normalize_http_url("HTTPS://Example.COM/path?q=1#fragment"),
            "https://example.com/path?q=1",
        )
        for value in (
            "javascript:alert(1)",
            "data:text/html,attack",
            "http://127.0.0.1/private",
            "https://user:secret@example.com/",
            "https://example.com:8443/",
            "https://example.com/\r\nattack",
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalize_http_url(value))

    @patch("services.safe_http._PinnedHTTPSConnection", FakeConnection)
    @patch("services.safe_http.socket.getaddrinfo")
    def test_request_connects_to_the_once_validated_public_ip(self, resolve):
        resolve.side_effect = [
            [(2, 1, 6, "", ("93.184.216.34", 443))],
            [(2, 1, 6, "", ("127.0.0.1", 443))],
        ]
        FakeConnection.responses = [FakeResponse(body=b"public")]

        response = safe_http_get("https://example.com/path")

        self.assertEqual(response.text, "public")
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(FakeConnection.instances[0].connect_ip, "93.184.216.34")
        self.assertEqual(FakeConnection.instances[0].hostname, "example.com")

    @patch("services.safe_http._PinnedHTTPSConnection", FakeConnection)
    @patch("services.safe_http.socket.getaddrinfo")
    def test_ipv4_is_preferred_when_dns_returns_both_address_families(self, resolve):
        resolve.return_value = [
            (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]
        FakeConnection.responses = [FakeResponse(body=b"public")]

        safe_http_get("https://example.com/")

        self.assertEqual(FakeConnection.instances[0].connect_ip, "93.184.216.34")

    @patch("services.safe_http._PinnedHTTPSConnection", FakeConnection)
    @patch("services.safe_http.socket.getaddrinfo")
    def test_redirect_target_is_validated_before_second_connection(self, resolve):
        def resolve_address(hostname, port, **_kwargs):
            address = "127.0.0.1" if hostname == "127.0.0.1" else "93.184.216.34"
            return [(2, 1, 6, "", (address, port))]

        resolve.side_effect = resolve_address
        FakeConnection.responses = [FakeResponse(
            status=302,
            body=b"",
            headers={"Location": "http://127.0.0.1/private"},
        )]

        with self.assertRaises(SafeHttpError):
            safe_http_get("https://example.com/start")

        self.assertEqual(len(FakeConnection.instances), 1)

    @patch("services.safe_http._PinnedHTTPSConnection", FakeConnection)
    @patch("services.safe_http.socket.getaddrinfo")
    @patch("services.safe_http.time.monotonic", side_effect=[0.0, 0.0, 2.0])
    def test_total_deadline_stops_a_slow_response(self, _clock, resolve):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        FakeConnection.responses = [FakeResponse(body=b"public")]

        with self.assertRaisesRegex(SafeHttpError, "celkový časový limit"):
            safe_http_get("https://example.com/", timeout=1)

    @patch("services.safe_http._PinnedHTTPSConnection", FakeConnection)
    @patch("services.safe_http.socket.getaddrinfo")
    @patch(
        "services.safe_http.time.monotonic",
        side_effect=[0.0, 0.0, 0.1, 0.2, 0.3, 0.6],
    )
    def test_deadline_is_checked_between_slow_drip_chunks(self, _clock, resolve):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        response = SlowDripFakeResponse(body=b"many bytes")
        FakeConnection.responses = [response]

        with self.assertRaisesRegex(SafeHttpError, "celkový časový limit"):
            safe_http_get("https://example.com/", timeout=0.5)

        self.assertLess(response._offset, len(response._body))

    @patch("services.safe_http._PinnedHTTPSConnection", FakeConnection)
    @patch("services.safe_http.socket.getaddrinfo")
    def test_oversize_and_binary_responses_are_rejected(self, resolve):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        for response in (
            FakeResponse(body=b"x" * 11),
            FakeResponse(body=b"x", headers={"Content-Type": "application/octet-stream"}),
        ):
            with self.subTest(content_type=response._headers.get("Content-Type")):
                FakeConnection.responses = [response]
                with self.assertRaises(SafeHttpError):
                    safe_http_get("https://example.com/", max_bytes=10)

    @patch("services.rpo_sync.safe_http_get", side_effect=SafeHttpError("blocked"))
    def test_rpo_website_validation_uses_the_ssrf_safe_client(self, safe_get):
        company = SimpleNamespace(ico="12345678", official_name="Test Firma s.r.o.")

        result = fetch_validated_website_result(
            company,
            "https://public.example/redirect-to-private",
        )

        self.assertIsNone(result)
        safe_get.assert_called_once()

    def test_rpo_pagination_and_redirects_cannot_leave_official_origin(self):
        malicious_page = SimpleNamespace(
            links={"next": {"url": "http://127.0.0.1/private"}}
        )
        with self.assertRaises(RpoSyncError):
            extract_next_url(malicious_page)

        redirect = SimpleNamespace(
            status_code=302,
            headers={"Location": "https://attacker.example/private"},
            ok=False,
        )
        session = SimpleNamespace(get=Mock(return_value=redirect))
        with self.assertRaises(RpoSyncError):
            request_page(session, f"{RPO_BASE_URL}/api/data")
        session.get.assert_called_once_with(
            f"{RPO_BASE_URL}/api/data",
            timeout=(10, 60),
            allow_redirects=False,
        )


if __name__ == "__main__":
    unittest.main()
