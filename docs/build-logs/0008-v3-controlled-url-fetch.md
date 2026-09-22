# 0008 — V3: Controlled URL Fetch

**Date:** 2026-09-22
**Contributors:** ChatGPT, Gemini (threat model + design), Claude (build + verify)

## Scope (as converged and frozen)
```
explicit --fetch -> policy validation -> user confirmation -> bounded HTTPS request -> UNPROCESSED inbox item
```
No crawling, no auto-fetch on plain `--url`, no retries, no auto-classification, no auto-task-creation,
no browsing beyond the requested URL/redirect chain.

## What was built
- `policy/network_policy.py` — `is_public_fetch_destination(ip)`: the single centralized SSRF
  boundary, per Gemini's explicit request that this not be scattered through NetworkInspector.
- `policy/url_validation.py` — scheme/userinfo/port/hostname-normalization/exact-domain-allowlist
  checks, all string-level, all before any DNS lookup.
- `tools/network.py` (`NetworkInspector`'s mechanics) — resolve once, validate the IP, connect to
  that exact validated IP, stream with a real byte cap, per-hop redirect revalidation, content-type
  enforcement, no retries.
- `state/migrations/003_network_allowlist.sql` — deny-by-default allowlist table (via the migration
  system built last round — the first real use of it beyond the initial schema).
- CLI: `network-allow`, `network-list`, `jarvis save --url ... --fetch`.
- New `ActionTier.EXTERNAL_NETWORK_READ`.

## Two real bugs found during implementation, not assumed away

**1. TLS SNI/hostname-verification bug in the first draft.** The initial version connected to the
resolved IP via `http.client.HTTPSConnection(resolved_ip, ...)` without overriding
`server_hostname` — which meant the TLS layer would have used the IP itself for SNI and certificate
verification, not the real hostname. This wouldn't have silently weakened security so much as
broken every real fetch outright (certificates are issued for hostnames, not IPs) — but if it had
degraded gracefully instead of failing loudly, it could have masked a real verification gap. Fixed
with `_PinnedHTTPSConnection`, a small `HTTPSConnection` subclass that connects the raw socket to
the pre-validated IP while explicitly passing the real hostname as `server_hostname` to the TLS
wrap — this is the actual mechanism that closes the DNS-rebinding gap ChatGPT flagged: the address
connected to is fixed at the value already validated, with no second, uncontrolled resolution at
connect time.

**2. Relative redirect `Location` headers weren't resolved before revalidation.** Caught immediately
by testing against a real local server (not a mock) that sends a redirect to `/ok` rather than a
full URL — exactly how many real servers behave. `normalize_and_validate_url` correctly rejected
the bare path as an invalid URL (no scheme), which is the right behavior for an actually-malformed
input, but the redirect handler needed to resolve relative `Location` headers against the current
URL first, per RFC 7231, before handing the result to validation. Fixed with `urljoin`.

## Verified — against a real local server, not mocks, wherever possible
- **IP classification (`is_public_fetch_destination`):** 23 cases — loopback, RFC1918, link-local
  (including the cloud-metadata address, confirmed already caught by `is_link_local`), CGNAT
  (confirmed NOT covered by stdlib `is_private`, explicit check added and verified), multicast,
  unspecified, reserved, and IPv4-mapped IPv6 forms of all of the above — all correctly rejected;
  real public IPv4/IPv6 addresses correctly accepted.
- **URL validation:** 10 cases — scheme, userinfo, port, exact-vs-subdomain matching, lookalike
  domains (`example.com.evil.com`), trailing-dot and case normalization — all correct.
- **NetworkInspector mechanics, against a real local HTTP server on 127.0.0.1** (via `connect_fn`
  injection, not mocking): successful fetch, single redirect followed and recorded, redirect loop
  correctly aborts at the 3-hop limit, oversized response aborted against **actual streamed bytes**
  with no `Content-Length` header present at all (proving the cap isn't dependent on a header that
  could be absent or lie), declared-oversized `Content-Length` rejected before reading any body,
  disallowed content-type rejected, empty-allowlist rejection before any connection is attempted,
  and — the specific case that matters for the "no approval inherited between hops" requirement —
  a redirect to a *different, non-allowlisted* domain correctly rejected on the second hop even
  though the first hop was allowed.
- **Structural proof of the DNS-rebinding defense:** a spy `connect_fn` confirms the exact resolved
  IP that was validated is what gets passed to the connection step — no second resolution occurs
  between validation and connect.
- **CLI wiring, live-run and automated:** empty allowlist blocks everything by default; plain
  `jarvis save --url` (no `--fetch`) confirmed via mock assertion to never call the fetch function
  at all; a disallowed domain is rejected with zero inbox item created; a declined `[y/N]`
  confirmation results in zero network calls and zero inbox item; a confirmed, successful
  (mocked) fetch lands as a plain `UNPROCESSED` inbox item indistinguishable from manual capture —
  no auto-classification, no auto-task, no auto-link; a fetch failure creates no item either.
- 104 tests total (98 previous + 6 new CLI-level), all passing.

## Honest scope limits — sandbox-verified, not real-network-verified
Per ChatGPT's explicit instruction to distinguish these: this sandbox has no general outbound
internet access (network egress is restricted to package-registry domains only), so the following
are **sandbox-verified at the unit/mechanics level only** and still need real confirmation:
- An actual DNS resolution against a real public hostname, followed by a genuine TLS handshake
  against a real certificate, over the real internet.
- Real DNS-rebinding behavior end-to-end (the structural test above proves the code *can't*
  re-resolve, which is the actual defense — but hasn't been exercised against a real rebinding DNS
  server).
- Proxy-environment-variable behavior on real Windows (whether Python's `http.client`/`ssl` stack
  picks up `HTTP_PROXY`/`HTTPS_PROXY` was not explicitly re-verified this round — worth an explicit
  check).
- General Windows socket/TLS behavior, same lesson as the path-boundary regression: sandbox-clean
  code has been wrong before.

**This needs Lone's real-ZBook verification before V3 is considered closed** — specifically:
`jarvis network-allow` against a real, genuinely-owned test domain, then `jarvis save --url ...
--fetch` against it for real, confirming the fetch succeeds, the content lands correctly in the
inbox, and nothing unexpected happens with proxy settings or Windows-specific networking behavior.

## Not built (per the frozen scope)
`http://`, non-standard ports, binary/other content types, auto-retry, browser automation,
crawling, auto-classification of fetched content, auto-task-creation, auto-fetch on plain
`jarvis save --url`.
