"""
URL-level policy validation: everything checkable BEFORE any DNS
resolution or network activity happens. Separate from
network_policy.py's destination-address classification (which can only
run after resolution) and from tools/network.py's actual fetch
mechanics -- kept deliberately separate per Gemini's "policy decisions
in policy/, mechanics in tools/" boundary.
"""

from urllib.parse import urlsplit


class URLPolicyViolation(Exception):
    """Raised when a URL fails validation before any network activity occurs."""


def normalize_and_validate_url(url: str, allowed_domains: set[str]) -> tuple[str, str]:
    """
    Validates a URL against every string-level policy rule, before any
    DNS lookup happens. Returns (normalized_hostname, path_and_query) on
    success. Raises URLPolicyViolation with a specific reason on failure.

    Checks, in order:
      - scheme must be exactly "https"
      - no embedded userinfo (user:pass@host) -- rejected outright, not
        stripped and continued, per the review: a saved URL with
        embedded credentials is itself a signal something's off
      - no explicit port, or explicit port must be exactly 443
      - hostname normalized (lowercase, trailing dot stripped) and
        checked for EXACT match against allowed_domains -- no automatic
        subdomain matching; "example.com" allowed does not imply
        "sub.example.com" is allowed
    """
    parts = urlsplit(url)

    if parts.scheme != "https":
        raise URLPolicyViolation(f"Only https:// URLs are permitted in V3 (got scheme '{parts.scheme}').")

    if parts.username is not None or parts.password is not None:
        raise URLPolicyViolation("URLs with embedded credentials (user:pass@host) are rejected outright.")

    if not parts.hostname:
        raise URLPolicyViolation("URL has no hostname.")

    if parts.port is not None and parts.port != 443:
        raise URLPolicyViolation(f"Only the default HTTPS port (443) is permitted (got explicit port {parts.port}).")

    hostname = parts.hostname.lower().rstrip(".")

    if hostname not in allowed_domains:
        raise URLPolicyViolation(
            f"'{hostname}' is not on the network allowlist. "
            f"Run `jarvis network-allow {hostname}` first if this is intentional."
        )

    path_and_query = parts.path or "/"
    if parts.query:
        path_and_query += f"?{parts.query}"

    return hostname, path_and_query
