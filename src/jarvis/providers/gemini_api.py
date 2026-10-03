"""
Gemini API provider: a second implementation of the same Provider
Protocol (dispatch_prompt(str) -> str) used by ManualClipboardProvider.
Stdlib-only per the converged review -- urllib.request, no vendor SDK.

Endpoint/request format confirmed directly against Google's own current
documentation (ai.google.dev/api, dated 2026-09-04) at the time this was
written: the "generateContent" endpoint is legacy but explicitly still
"fully supported" by Google, and is the right fit here since Jarvis
only needs single-turn request/response, not the newer stateful
Interactions API's session management.

WHAT IS NOT VERIFIED, stated plainly rather than assumed: whether
Lone's specific Google account/project has free-tier access, what its
exact rate limits are, and whether that free tier remains stable over
time. Real developer reports (checked at write time) show the same
supposedly-supported endpoint returning 404/403 for some accounts even
with valid keys and confirmed quota -- so this adapter treats every
call as something that can fail in ways worth surfacing clearly, not
as a guaranteed-working integration.
"""

import json
import os
import urllib.error
import urllib.request

from jarvis.policy.validator import PolicyValidator, PolicyViolation

_ENDPOINT_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_DEFAULT_MODEL = "gemini-2.5-flash"
_TIMEOUT_SECONDS = 30


class GeminiAPIError(Exception):
    """Raised for any Gemini API failure -- missing key, HTTP error, malformed response."""


class GeminiAPIProvider:
    """
    Real network calls. Every call goes through
    PolicyValidator.authorize_provider(expects_cost=False) first --
    per the project's $0 policy, this provider is only usable at all
    because it is declared free-tier. That declaration is NOT
    independently verified by this code (see module docstring); it
    relies on Lone having confirmed his own account's free-tier
    eligibility before ever using --backend gemini. This is a known,
    named limitation of the current policy gate, not something this
    adapter can fix by itself.
    """

    def __init__(self, model: str = _DEFAULT_MODEL, provider_name: str = "Gemini_API"):
        self.model = model
        self.provider_name = provider_name

    def dispatch_prompt(self, prompt_text: str) -> str:
        try:
            PolicyValidator.authorize_provider(self.provider_name, expects_cost=False)
        except PolicyViolation as e:
            return f"ERROR: Access denied by policy. {e}"

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return (
                "ERROR: GEMINI_API_KEY environment variable is not set. "
                "Set it before using --backend gemini; the key is never read from "
                "source, config files, or committed anywhere."
            )

        url = _ENDPOINT_TEMPLATE.format(model=self.model)
        payload = json.dumps({"contents": [{"parts": [{"text": prompt_text}]}]}).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        )

        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            # Real reports (checked before writing this) show 404 occurring
            # even with a valid key and confirmed quota for some accounts --
            # naming that possibility explicitly rather than implying it
            # always means "wrong URL."
            hint = ""
            if e.code == 404:
                hint = " (some accounts see this even with a valid key and confirmed quota -- verify model access in Google AI Studio)"
            elif e.code == 429:
                hint = " (rate limit or quota exhausted -- Jarvis will not retry or fall back automatically)"
            elif e.code == 403:
                hint = " (permission denied -- check the key's project/API is enabled for this model)"
            return f"ERROR: Gemini API returned HTTP {e.code}{hint}. Response: {body[:500]}"
        except urllib.error.URLError as e:
            return f"ERROR: could not reach the Gemini API (network issue): {e.reason}"
        except TimeoutError:
            return f"ERROR: Gemini API request timed out after {_TIMEOUT_SECONDS}s."

        try:
            parsed = json.loads(raw)
            return parsed["candidates"][0]["content"]["parts"][0]["text"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            return f"ERROR: could not parse Gemini API response ({e}). Raw response: {raw[:500]}"
