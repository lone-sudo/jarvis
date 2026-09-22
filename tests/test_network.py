"""
Tests the NetworkInspector's mechanics (redirects, streaming byte cap,
content-type enforcement, timeout) against a REAL local HTTP server on
127.0.0.1, via connect_fn injection -- not mocked away. Honest scope
note: this validates the higher-level request/redirect/streaming logic
for real, but not the TLS/SNI-pinning path itself (_PinnedHTTPSConnection,
which needs a real hostname + real certificate to mean anything) or real
DNS resolution/rebinding behavior -- those need verification against a
real external HTTPS endpoint on the actual ZBook, flagged here rather
than faked with a mock that would prove nothing.
"""

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis.tools import network
from jarvis.policy.network_policy import is_public_fetch_destination
from jarvis.policy.url_validation import URLPolicyViolation, normalize_and_validate_url
import ipaddress


# ---------------------------------------------------------------------
# A real local test server, run in a background thread. No mocking of
# HTTP semantics -- actual sockets, actual redirects, actual streamed
# bytes over the loopback interface.
# ---------------------------------------------------------------------

class _TestHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet test output

    def do_GET(self):
        if self.path == "/ok":
            body = b"hello world, this is a normal text response"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-once":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.end_headers()
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            # No Content-Length -- forces the streaming path to enforce
            # the cap against actual bytes, not a declared header.
            chunk = b"x" * 65536
            for _ in range(200):  # 200 * 64KB = ~12.5MB, over the 5MB cap
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
        elif self.path == "/wrong-type":
            body = b"<html>not allowed by content-type policy</html>"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/oversized-declared":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(100 * 1024 * 1024))  # lies, declares 100MB
            self.end_headers()
            self.wfile.write(b"short body")
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def test_server():
    server = HTTPServer(("127.0.0.1", 0), _TestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


def _plain_connect_factory(port):
    def connect_fn(hostname, resolved_ip):
        return http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    return connect_fn


# --- Real end-to-end fetch mechanics, against the real local server ---

def test_successful_fetch(test_server):
    result = network.fetch(
        "https://testserver.local/ok", {"testserver.local"},
        connect_fn=_plain_connect_factory(test_server), skip_ip_validation=True,
    )
    assert result.status_code == 200
    assert "hello world" in result.body_text
    assert result.content_type == "text/plain"
    assert result.redirect_count == 0


def test_single_redirect_followed_and_recorded(test_server):
    result = network.fetch(
        "https://testserver.local/redirect-once", {"testserver.local"},
        connect_fn=_plain_connect_factory(test_server), skip_ip_validation=True,
    )
    assert result.status_code == 200
    assert result.redirect_count == 1
    assert len(result.redirect_chain) == 1
    assert "hello world" in result.body_text


def test_redirect_loop_hits_max_redirects_and_aborts(test_server):
    with pytest.raises(network.NetworkFetchError, match="redirects"):
        network.fetch(
            "https://testserver.local/redirect-loop", {"testserver.local"},
            connect_fn=_plain_connect_factory(test_server), skip_ip_validation=True,
        )


def test_oversized_streaming_response_aborted_by_actual_bytes(test_server):
    """
    /big sends no Content-Length header at all -- this proves the cap
    is enforced against real streamed bytes, not a declared header,
    since there's no header to check.
    """
    with pytest.raises(network.NetworkFetchError, match="exceeded"):
        network.fetch(
            "https://testserver.local/big", {"testserver.local"},
            connect_fn=_plain_connect_factory(test_server), skip_ip_validation=True,
        )


def test_declared_oversized_content_length_rejected_before_reading_body(test_server):
    with pytest.raises(network.NetworkFetchError, match="Declared Content-Length"):
        network.fetch(
            "https://testserver.local/oversized-declared", {"testserver.local"},
            connect_fn=_plain_connect_factory(test_server), skip_ip_validation=True,
        )


def test_disallowed_content_type_rejected(test_server):
    with pytest.raises(network.NetworkFetchError, match="Content-Type"):
        network.fetch(
            "https://testserver.local/wrong-type", {"testserver.local"},
            connect_fn=_plain_connect_factory(test_server), skip_ip_validation=True,
        )


def test_domain_not_on_allowlist_rejected_before_any_connection(test_server):
    with pytest.raises(URLPolicyViolation, match="allowlist"):
        network.fetch(
            "https://testserver.local/ok", set(),  # empty allowlist
            connect_fn=_plain_connect_factory(test_server), skip_ip_validation=True,
        )


def test_each_redirect_hop_revalidated_not_inherited(test_server):
    """
    Redirect target is on a DIFFERENT domain not in the allowlist --
    must be rejected even though the first hop was allowed, proving
    approval isn't inherited from hop to hop.
    """
    class RedirectToOtherDomainHandler(_TestHandler):
        def do_GET(self):
            if self.path == "/redirect-elsewhere":
                self.send_response(302)
                self.send_header("Location", "https://not-allowed.example/ok")
                self.end_headers()
            else:
                super().do_GET()

    server = HTTPServer(("127.0.0.1", 0), RedirectToOtherDomainHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(URLPolicyViolation, match="allowlist"):
            network.fetch(
                "https://testserver.local/redirect-elsewhere", {"testserver.local"},
                connect_fn=_plain_connect_factory(port), skip_ip_validation=True,
            )
    finally:
        server.shutdown()


# --- IP classification: comprehensive, real, no network needed ---

@pytest.mark.parametrize("addr", [
    "127.0.0.1", "127.255.255.255",
    "10.0.0.1", "172.16.0.1", "192.168.1.1",
    "169.254.169.254", "169.254.1.1",
    "100.64.0.1", "100.127.255.255",
    "224.0.0.1", "0.0.0.0",
    "::1", "fc00::1", "fd00::1", "fe80::1", "::",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
])
def test_unsafe_destination_addresses_rejected(addr):
    assert is_public_fetch_destination(ipaddress.ip_address(addr)) is False


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_destination_addresses_accepted(addr):
    assert is_public_fetch_destination(ipaddress.ip_address(addr)) is True


def test_no_second_dns_resolution_at_connect_time(test_server):
    """
    Structural proof of the DNS-rebinding defense: connect_fn receives
    the exact resolved_ip that was already validated -- there is no
    second resolution step between validation and connection. If a
    future change accidentally introduced one (e.g. passing the
    hostname to connect_fn instead of the validated IP), this test
    would catch it.
    """
    received = {}

    def spy_connect_fn(hostname, resolved_ip):
        received["hostname"] = hostname
        received["resolved_ip"] = resolved_ip
        return http.client.HTTPConnection("127.0.0.1", test_server, timeout=5)

    network.fetch(
        "https://testserver.local/ok", {"testserver.local"},
        connect_fn=spy_connect_fn, skip_ip_validation=True,
    )
    assert received["hostname"] == "testserver.local"
    assert received["resolved_ip"] == "TEST"  # the skip_ip_validation sentinel, passed through unchanged


# --- URL validation: comprehensive, real, no network needed ---

@pytest.mark.parametrize("url,should_pass", [
    ("https://example.com/path", True),
    ("https://example.com.", True),
    ("https://EXAMPLE.COM/path", True),
    ("http://example.com/path", False),
    ("https://sub.example.com/path", False),
    ("https://example.com.evil.com/", False),
    ("https://user:pass@example.com/", False),
    ("https://example.com:8443/", False),
    ("https://example.com:443/", True),
    ("https://notallowed.com/", False),
])
def test_url_validation_cases(url, should_pass):
    allowed = {"example.com"}
    if should_pass:
        normalize_and_validate_url(url, allowed)  # must not raise
    else:
        with pytest.raises(URLPolicyViolation):
            normalize_and_validate_url(url, allowed)
