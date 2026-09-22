"""
NetworkInspector: deterministic mechanics only. Policy decisions (is
this domain allowed, is this destination address safe) live in
policy/url_validation.py and policy/network_policy.py and are called
from here, not reimplemented here -- per Gemini's separation-of-
concerns requirement.

Resolver-to-connection binding (ChatGPT's point #1): resolves the
hostname to an IP once, validates that IP, then opens the raw TCP
connection to that EXACT validated IP -- never re-resolving the
hostname -- while the TLS handshake still uses the ORIGINAL hostname for
SNI and certificate verification via _PinnedHTTPSConnection below. This
is what actually closes the DNS-rebinding gap; a second, uncontrolled
resolution at connect time is exactly the vulnerability being defended
against. TLS verification is never weakened to make this work.
"""

import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin

from jarvis.policy.network_policy import is_public_fetch_destination
from jarvis.policy.url_validation import normalize_and_validate_url

MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 10
TOTAL_TIMEOUT_SECONDS = 20
MAX_REDIRECTS = 3
ALLOWED_CONTENT_TYPES = ("text/html", "text/plain")


class NetworkFetchError(Exception):
    """Raised for any fetch failure -- policy violation, timeout, oversized response, etc."""


@dataclass
class FetchResult:
    original_url: str
    final_url: str
    resolved_ip: str
    status_code: int
    content_type: str
    bytes_received: int
    redirect_count: int
    body_text: str
    redirect_chain: list = field(default_factory=list)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """
    Connects the raw TCP socket to a pre-resolved, pre-validated IP
    (passed as `host` to the constructor -- that's what the socket
    actually connects to), while forcing TLS SNI and certificate
    hostname verification to use the ORIGINAL hostname via
    server_hostname. Without this override, http.client's default
    behavior derives server_hostname from self.host, which would be
    the IP -- either breaking verification against real certificates
    (issued for hostnames, not IPs) or silently skipping meaningful
    hostname verification. Neither is acceptable.
    """

    def __init__(self, resolved_ip, real_hostname, **kwargs):
        super().__init__(resolved_ip, **kwargs)
        self._real_hostname = real_hostname

    def connect(self):
        self.sock = self._create_connection((self.host, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._real_hostname)


def _resolve_and_validate_ip(hostname: str) -> str:
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise NetworkFetchError(f"DNS resolution failed for '{hostname}': {e}")
    if not infos:
        raise NetworkFetchError(f"DNS resolution returned no addresses for '{hostname}'.")

    resolved_ip = infos[0][4][0]
    ip_obj = ipaddress.ip_address(resolved_ip)
    if not is_public_fetch_destination(ip_obj):
        raise NetworkFetchError(
            f"'{hostname}' resolved to '{resolved_ip}', which is not a permitted public destination."
        )
    return resolved_ip


def _default_connect(hostname, resolved_ip):
    """Production connection: TLS, pinned to the validated IP, real-hostname SNI/verification."""
    context = ssl.create_default_context()
    conn = _PinnedHTTPSConnection(
        resolved_ip, hostname, port=443, timeout=CONNECT_TIMEOUT_SECONDS, context=context,
    )
    conn.connect()
    conn.sock.settimeout(READ_TIMEOUT_SECONDS)
    return conn


def _do_request_and_stream(conn, hostname, path_and_query, deadline):
    """
    Sends one GET request on an already-connected connection and streams
    the response with the byte cap enforced against bytes actually
    received (not the declared Content-Length, which can be absent or
    lie), plus a wall-clock deadline covering the whole read so a slow-
    drip response can't outlast TOTAL_TIMEOUT_SECONDS via many small
    reads that individually stay under the per-read timeout.
    """
    headers = {
        "Host": hostname,
        "User-Agent": "Jarvis/1 (+controlled-fetch)",
        "Accept": "text/html,text/plain",
        "Connection": "close",
    }
    conn.request("GET", path_and_query, headers=headers)
    response = conn.getresponse()

    status = response.status
    resp_headers = {k.lower(): v for k, v in response.getheaders()}

    declared_length = resp_headers.get("content-length")
    if declared_length is not None and int(declared_length) > MAX_RESPONSE_BYTES:
        raise NetworkFetchError(
            f"Declared Content-Length ({declared_length} bytes) exceeds the {MAX_RESPONSE_BYTES}-byte limit -- rejected before reading body."
        )

    chunks = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise NetworkFetchError(f"Total operation time exceeded {TOTAL_TIMEOUT_SECONDS}s -- aborted mid-stream.")
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise NetworkFetchError(f"Response exceeded the {MAX_RESPONSE_BYTES}-byte limit while streaming -- aborted.")
        chunks.append(chunk)

    return status, resp_headers, b"".join(chunks)


def fetch(url, allowed_domains, connect_fn=_default_connect, skip_ip_validation=False):
    """
    The full controlled-fetch flow: validate -> resolve -> validate IP
    -> request -> (repeat per redirect, each hop independently
    revalidated from scratch) -> enforce content-type and size -> return
    result. No automatic retries: any failure at any stage raises
    immediately.

    connect_fn is injectable so tests can substitute a plain local
    connection without real TLS/DNS. skip_ip_validation exists ONLY for
    tests that exercise redirect/streaming/content-type logic against a
    local test server on 127.0.0.1 (which is intentionally rejected by
    is_public_fetch_destination in production) -- never used by any
    production code path, and never reachable via the CLI.
    """
    redirect_chain = []
    current_url = url
    redirects_followed = 0
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS

    while True:
        hostname, path_and_query = normalize_and_validate_url(current_url, allowed_domains)
        if skip_ip_validation:
            resolved_ip = "TEST"
        else:
            resolved_ip = _resolve_and_validate_ip(hostname)

        conn = connect_fn(hostname, resolved_ip)
        try:
            status, headers, body = _do_request_and_stream(conn, hostname, path_and_query, deadline)
        finally:
            conn.close()

        if status in (301, 302, 303, 307, 308):
            if redirects_followed >= MAX_REDIRECTS:
                raise NetworkFetchError(f"Exceeded max redirects ({MAX_REDIRECTS}).")
            location = headers.get("location")
            if not location:
                raise NetworkFetchError(f"Redirect status {status} with no Location header.")
            redirect_chain.append(current_url)
            # Location headers may legally be relative (RFC 7231) --
            # resolve against the current URL before the next loop
            # iteration re-validates it from scratch.
            current_url = urljoin(current_url, location)
            redirects_followed += 1
            continue

        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise NetworkFetchError(f"Content-Type '{content_type}' is not permitted (only {ALLOWED_CONTENT_TYPES}).")

        try:
            body_text = body.decode("utf-8", errors="replace")
        except Exception as e:
            raise NetworkFetchError(f"Could not decode response body as text: {e}")

        return FetchResult(
            original_url=url, final_url=current_url, resolved_ip=resolved_ip,
            status_code=status, content_type=content_type, bytes_received=len(body),
            redirect_count=redirects_followed, body_text=body_text, redirect_chain=redirect_chain,
        )
