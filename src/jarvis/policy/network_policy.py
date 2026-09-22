"""
The one security boundary for outbound network fetches: is this
resolved IP address safe to connect to?

Deliberately centralized per Gemini's review — every SSRF check in
NetworkInspector routes through this single function, so it can be
reviewed and tested independently rather than scattered across the
fetch code.
"""

import ipaddress

# Carrier-Grade NAT range (RFC 6598) — NOT covered by ipaddress.is_private
# in the standard library (confirmed empirically: ipaddress.ip_address(
# "100.64.0.1").is_private is False). Explicit check required.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def is_public_fetch_destination(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    True only if this address is safe for Jarvis to connect to on the
    user's behalf. Rejects (for both IPv4 and IPv6, including IPv4-mapped
    IPv6 unwrapped to its IPv4 form first):
      - loopback (127.0.0.0/8, ::1)
      - private/RFC1918 (10/8, 172.16/12, 192.168/16, fc00::/7)
      - link-local (169.254.0.0/16, fe80::/10) -- this already covers the
        cloud-metadata endpoint 169.254.169.254, confirmed via stdlib
      - multicast
      - unspecified (0.0.0.0, ::)
      - reserved (stdlib-flagged, e.g. certain IPv6 reserved blocks)
      - CGNAT (100.64.0.0/10) -- explicit check, not stdlib-covered
    Everything else is allowed through to the domain-allowlist check
    (this function alone does not grant access — it only rules out the
    unsafe destinations; the caller still requires an allowlisted domain).
    """
    # Unwrap IPv4-mapped IPv6 addresses (::ffff:a.b.c.d) to their IPv4
    # form before classifying, so a mapped private/loopback address
    # can't slip past IPv6-only checks.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast:
        return False
    if ip.is_unspecified or ip.is_reserved:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_V4:
        return False

    return True
