"""Small HTTP client for fetching untrusted public websites without SSRF.

DNS is resolved once, every returned address must be public, and the socket is
connected to the validated address directly. Redirects are followed manually
and validated again. Responses are streamed into a bounded buffer.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_MAX_REDIRECTS = 5
ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
}


class SafeHttpError(RuntimeError):
    pass


@dataclass(frozen=True)
class SafeHttpResponse:
    url: str
    status_code: int
    text: str
    headers: dict[str, str]


def normalize_http_url(value, *, default_scheme=False, max_length=1500):
    """Canonicalize a link for storage/rendering without doing network I/O."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > max_length:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        return None
    if default_scheme and "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    port = port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        return None
    hostname = parsed.hostname.rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if not hostname or hostname.casefold() == "localhost":
        return None
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        return None
    normalized_host = f"[{hostname}]" if literal_address and literal_address.version == 6 else hostname
    if port != (443 if scheme == "https" else 80):
        normalized_host = f"{normalized_host}:{port}"
    normalized = urlunsplit((
        scheme,
        normalized_host,
        parsed.path or "/",
        parsed.query,
        "",
    ))
    return normalized if len(normalized) <= max_length else None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname, connect_ip, port, timeout):
        self._connect_ip = connect_ip
        super().__init__(hostname, port=port, timeout=timeout)

    def connect(self):
        self.sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self._abortable_socket = self.sock

    def abort(self):
        sock = getattr(self, "_abortable_socket", None) or self.sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, connect_ip, port, timeout):
        self._connect_ip = connect_ip
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )

    def connect(self):
        raw_socket = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
            self._abortable_socket = self.sock
        except Exception:
            raw_socket.close()
            raise

    def abort(self):
        sock = getattr(self, "_abortable_socket", None) or self.sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class _DeadlineWatchdog:
    """Interrupt blocking socket reads when the request deadline expires."""

    def __init__(self, connection, timeout):
        self.connection = connection
        self.expired = False
        self.timer = threading.Timer(timeout, self._expire)
        self.timer.daemon = True

    def _expire(self):
        self.expired = True
        abort = getattr(self.connection, "abort", None)
        if callable(abort):
            abort()
        else:
            self.connection.close()

    def start(self):
        self.timer.start()

    def cancel(self):
        self.timer.cancel()


def _validated_target(url):
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise SafeHttpError("Neplatná URL webu.") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        raise SafeHttpError("Povolené sú iba verejné HTTP a HTTPS adresy.")
    if parsed.username is not None or parsed.password is not None:
        raise SafeHttpError("URL nesmie obsahovať prihlasovacie údaje.")
    port = port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise SafeHttpError("URL používa nepovolený port.")

    hostname = parsed.hostname.rstrip(".")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SafeHttpError("Neplatný názov hostiteľa.") from exc
    if not hostname or hostname.casefold() == "localhost":
        raise SafeHttpError("Lokálne a súkromné adresy nie sú povolené.")

    try:
        resolved = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError) as exc:
        raise SafeHttpError("Webovú adresu sa nepodarilo bezpečne preložiť.") from exc
    if not resolved or any(not address.is_global for address in resolved):
        raise SafeHttpError("Lokálne a súkromné adresy nie sú povolené.")

    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&?/:;+,%@!$'()*-._~")
    target = path + (f"?{query}" if query else "")
    normalized_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = normalized_host
    if port != (443 if scheme == "https" else 80):
        netloc = f"{netloc}:{port}"
    normalized_url = urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))
    # Prefer IPv4 when both families are advertised. Some production hosts do
    # not have a working IPv6 route even though DNS returns AAAA records.
    connect_address = sorted(
        resolved,
        key=lambda address: (address.version, str(address)),
    )[0]
    return scheme, hostname, port, str(connect_address), target, normalized_url


def _safe_headers(headers):
    result = {"User-Agent": "LeadAgent-PublicFetcher/1.0", "Accept": "text/html,text/plain;q=0.9"}
    for name, value in (headers or {}).items():
        name, value = str(name), str(value)
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            raise SafeHttpError("Neplatná HTTP hlavička.")
        if name.casefold() not in {"user-agent", "accept", "accept-language"}:
            raise SafeHttpError("Nepovolená HTTP hlavička.")
        result[name] = value
    return result


def safe_http_get(
    url,
    *,
    headers=None,
    timeout=15,
    max_bytes=DEFAULT_MAX_BYTES,
    max_redirects=DEFAULT_MAX_REDIRECTS,
):
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise SafeHttpError("Neplatný limit veľkosti odpovede.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise SafeHttpError("Neplatný časový limit požiadavky.")
    request_headers = _safe_headers(headers)
    current_url = str(url or "").strip()
    deadline = time.monotonic() + float(timeout)

    def remaining_timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SafeHttpError("Web prekročil celkový časový limit.")
        return max(0.1, remaining)

    for redirect_number in range(max_redirects + 1):
        scheme, hostname, port, connect_ip, target, normalized_url = _validated_target(current_url)
        request_timeout = remaining_timeout()
        connection_class = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
        connection = connection_class(hostname, connect_ip, port, request_timeout)
        watchdog = _DeadlineWatchdog(connection, request_timeout)
        watchdog.start()
        try:
            connection.request("GET", target, headers=request_headers)
            response = connection.getresponse()
            response_headers = {key.casefold(): value for key, value in response.getheaders()}
            if response.status in REDIRECT_STATUSES:
                location = response_headers.get("location")
                if getattr(connection, "sock", None) is not None:
                    connection.sock.settimeout(remaining_timeout())
                response.read(1)
                if not location:
                    raise SafeHttpError("Presmerovanie nemá cieľovú URL.")
                if redirect_number >= max_redirects:
                    raise SafeHttpError("Web prekročil limit presmerovaní.")
                current_url = urljoin(normalized_url, location)
                continue

            content_type_header = response_headers.get("content-type", "")
            content_type = content_type_header.split(";", 1)[0].strip().casefold()
            if content_type and content_type not in ALLOWED_CONTENT_TYPES:
                raise SafeHttpError("Web nevrátil podporovaný textový obsah.")
            payload = bytearray()
            while len(payload) <= max_bytes:
                if getattr(connection, "sock", None) is not None:
                    connection.sock.settimeout(remaining_timeout())
                else:
                    remaining_timeout()
                amount = min(64 * 1024, max_bytes + 1 - len(payload))
                read_chunk = getattr(response, "read1", response.read)
                chunk = read_chunk(amount)
                if not chunk:
                    break
                payload.extend(chunk)
                remaining_timeout()
            if len(payload) > max_bytes:
                raise SafeHttpError("Web prekročil povolenú veľkosť odpovede.")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                text = bytes(payload).decode(charset, errors="replace")
            except LookupError:
                text = bytes(payload).decode("utf-8", errors="replace")
            return SafeHttpResponse(
                url=normalized_url,
                status_code=response.status,
                text=text,
                headers=response_headers,
            )
        except SafeHttpError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            if watchdog.expired:
                raise SafeHttpError("Web prekročil celkový časový limit.") from exc
            raise SafeHttpError("Web sa nepodarilo bezpečne načítať.") from exc
        finally:
            watchdog.cancel()
            connection.close()

    raise SafeHttpError("Web prekročil limit presmerovaní.")
