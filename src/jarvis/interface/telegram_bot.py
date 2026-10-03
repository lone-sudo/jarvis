"""
Telegram interface (read-only first slice). A REMOTE TERMINAL for a fixed
set of existing read-only Jarvis commands -- not a separate assistant.

Frozen boundaries (Lone + ChatGPT + Gemini, see build-log 0014):
- Foreground only. Runs while `jarvis telegram-start` runs; Ctrl+C stops it.
  No daemon, no boot persistence, no state written to disk between runs.
- Read-only. Only ActionTier.OBSERVE commands are registered. No writes,
  no fetches, no confirmations -- every [y/N] in Jarvis is a blocking
  input() in one process, which cannot be safely remapped to a remote
  message without a new single-use, action-bound, expiring token
  mechanism. That mechanism is deliberately NOT built here.
- One authorized sender (numeric user id), private chats only. Anyone
  else gets no reply at all.
- No free-text passthrough. A fixed command table; message text never
  reaches argparse, a shell, or input().
- stdlib only (urllib). Token from environment only, scrubbed from every
  error and log line.
"""

import argparse
import builtins
import contextlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from jarvis.policy.rules import ActionTier
from jarvis.policy.validator import PolicyValidator, PolicyViolation

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_SECONDS = 30
ERROR_BACKOFF_SECONDS = 5
MAX_RETRY_AFTER_SECONDS = 60
MAX_REPLY_CHARS = 3900  # Telegram's cap is 4096; stay safely under it

# The Telegram interface may only ever expose these tiers. Anything else
# is refused at dispatch time, not just by convention.
ALLOWED_TIERS = frozenset({ActionTier.OBSERVE})

_TOKEN_SHAPE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")


class TelegramConfigError(Exception):
    """Missing or invalid configuration. Never includes the token itself."""


class TelegramAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    allowed_user_id: int


def load_config_from_env() -> TelegramConfig:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not token:
        raise TelegramConfigError(
            "TELEGRAM_BOT_TOKEN is not set. Set it in this PowerShell session only; "
            "never put it in a file this repo tracks."
        )
    if not raw_id:
        raise TelegramConfigError("TELEGRAM_ALLOWED_USER_ID is not set (your numeric Telegram user id).")
    try:
        user_id = int(raw_id)
    except ValueError:
        raise TelegramConfigError("TELEGRAM_ALLOWED_USER_ID must be a number (not a @username).") from None
    if user_id <= 0:
        raise TelegramConfigError("TELEGRAM_ALLOWED_USER_ID must be a positive number.")
    return TelegramConfig(token=token, allowed_user_id=user_id)


def redact(text: str, token: str | None = None) -> str:
    """Scrub the token (exact value AND anything token-shaped) from text."""
    if token:
        text = text.replace(token, "[REDACTED]")
    return _TOKEN_SHAPE.sub("[REDACTED]", text)


class TelegramClient:
    """Minimal stdlib client: getUpdates + sendMessage only."""

    def __init__(self, token: str, base_url: str = API_BASE, opener=None):
        self._token = token
        self._base_url = base_url
        # Ignore ambient HTTP(S)_PROXY settings, same stance as the V3 fetch.
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, params: dict, timeout: float) -> object:
        url = f"{self._base_url}/bot{self._token}/{method}"
        request = urllib.request.Request(
            url, data=json.dumps(params).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        )
        # Every failure below is re-raised as TelegramAPIError with the
        # token scrubbed and the original exception dropped (`from None`),
        # because the token lives inside the request URL and Python's own
        # exception text/tracebacks could otherwise carry it.
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            status, retry_after, description = e.code, None, "HTTP error"
            try:
                body = json.loads(e.read().decode("utf-8", errors="replace"))
                description = str(body.get("description", description))
                retry_after = (body.get("parameters") or {}).get("retry_after")
            except Exception:
                pass
            raise TelegramAPIError(
                redact(f"Telegram API returned HTTP {status}: {description}", self._token),
                status_code=status, retry_after=retry_after,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramAPIError(redact(f"could not reach Telegram ({type(e).__name__})", self._token)) from None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise TelegramAPIError("Telegram returned a non-JSON response.") from None
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            raise TelegramAPIError(redact(f"Telegram reported failure: {str(parsed)[:200]}", self._token))
        return parsed.get("result")

    def get_updates(self, offset: int | None, timeout: int) -> list:
        params: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", params, timeout=timeout + 10)
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str) -> None:
        self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                # Plain text on purpose (no parse_mode): inbox titles/URLs
                # contain characters that break Markdown/HTML. Previews are
                # disabled so Telegram's servers never fetch saved URLs on
                # our behalf. Both field names are sent because which one
                # Telegram currently honors is unverified -- see build-log.
                "disable_web_page_preview": True,
                "link_preview_options": {"is_disabled": True},
            },
            timeout=15,
        )

    def drain_backlog(self, max_rounds: int = 20) -> int | None:
        """
        Discard everything queued while the bot was offline, so an old
        command is never executed late. Uses the documented negative
        offset (returns the newest update and forgets earlier ones), then
        confirms it. Returns the offset to resume from.
        """
        offset: int | None = None
        newest = self.get_updates(offset=-1, timeout=0)
        if newest:
            offset = newest[-1]["update_id"] + 1
        for _ in range(max_rounds):
            leftovers = self.get_updates(offset=offset, timeout=0)
            if not leftovers:
                break
            offset = leftovers[-1]["update_id"] + 1
        return offset


# ---------------------------------------------------------------------
# Read-only command table
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    handler: Callable[[list[str]], str]
    tier: ActionTier
    usage: str


def _no_input(*_args, **_kwargs):
    raise RuntimeError("input() is not available over Telegram (read-only interface).")


def _run_captured(fn: Callable, *args) -> str:
    """
    Runs an existing CLI command function and returns what it printed.
    input() is replaced with a function that raises, so a command that
    tried to ask for confirmation would fail loudly instead of hanging
    the bot or silently proceeding.
    """
    buffer = io.StringIO()
    real_input = builtins.input
    builtins.input = _no_input
    try:
        with contextlib.redirect_stdout(buffer):
            fn(*args)
    finally:
        builtins.input = real_input
    return buffer.getvalue().strip() or "(no output)"


def _cmd_resume(_args: list[str]) -> str:
    from jarvis.interface.cli import cmd_resume
    return _run_captured(cmd_resume, argparse.Namespace())


_INBOX_STATUSES = {"unprocessed": "UNPROCESSED", "processed": "PROCESSED", "archived": "ARCHIVED"}


def _cmd_inbox(args: list[str]) -> str:
    from jarvis.interface.cli import cmd_inbox
    status = None
    if args:
        status = _INBOX_STATUSES.get(args[0].lower())
        if status is None or len(args) > 1:
            return "Usage: /inbox [unprocessed|processed|archived]"
    return _run_captured(cmd_inbox, argparse.Namespace(status=status))


def _cmd_help(_args: list[str]) -> str:
    return (
        "Jarvis (read-only over Telegram)\n"
        "/resume - where you left off\n"
        "/inbox [unprocessed|processed|archived] - saved items\n"
        "/help - this message\n\n"
        "Nothing here can change anything. Confirmations and edits stay on the ZBook."
    )


COMMANDS: dict[str, Command] = {
    "/resume": Command(_cmd_resume, ActionTier.OBSERVE, "/resume"),
    "/inbox": Command(_cmd_inbox, ActionTier.OBSERVE, "/inbox [status]"),
    "/help": Command(_cmd_help, ActionTier.OBSERVE, "/help"),
    "/start": Command(_cmd_help, ActionTier.OBSERVE, "/start"),
}


def _truncate(text: str) -> str:
    if len(text) <= MAX_REPLY_CHARS:
        return text
    return text[:MAX_REPLY_CHARS] + "\n...[truncated]"


def _log(message: str, token: str | None = None) -> None:
    print(f"[telegram] {redact(message, token)}", file=sys.stderr)


def handle_update(update: dict, config: TelegramConfig, client) -> None:
    """
    Processes one update. Strangers and non-private chats are ignored
    with NO reply (so a probing account can't tell a bot is listening);
    only a local log line (sender id + time, never message text) is kept.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return

    chat = message.get("chat") or {}
    sender = message.get("from") or {}

    if chat.get("type") != "private":
        return  # groups/channels: dropped untouched

    if sender.get("id") != config.allowed_user_id or chat.get("id") != config.allowed_user_id:
        _log(f"ignored message from unauthorized sender id={sender.get('id')} at {time.strftime('%H:%M:%S')}", config.token)
        return

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        client.send_message(config.allowed_user_id, "Only text commands are supported. Try /help.")
        return

    parts = text.strip().split()
    name = parts[0].split("@", 1)[0].lower()  # "/resume@MyBot" -> "/resume"
    command = COMMANDS.get(name)
    if command is None:
        client.send_message(config.allowed_user_id, "Unknown command. Try /help.")
        return

    try:
        if command.tier not in ALLOWED_TIERS:
            raise PolicyViolation(f"'{name}' is not read-only; refused over Telegram.")
        PolicyValidator.authorize_tool(f"telegram:{name}", command.tier)
        reply = command.handler(parts[1:])
    except PolicyViolation:
        _log(f"refused {name}: tier not permitted over Telegram", config.token)
        reply = "That command isn't available over Telegram."
    except Exception as e:  # never let one bad command kill the loop
        _log(f"{name} failed: {type(e).__name__}", config.token)
        reply = "That command failed. Check the terminal running Jarvis."

    _log(f"handled {name}", config.token)
    client.send_message(config.allowed_user_id, _truncate(reply))


def run_bot(
    config: TelegramConfig, client=None, poll_timeout: int = POLL_TIMEOUT_SECONDS,
    max_polls: int | None = None, sleep: Callable[[float], None] = time.sleep,
) -> int:
    """
    Foreground long-polling loop. Returns an exit code. Stops on Ctrl+C.
    Nothing is persisted: the offset lives in memory only, and startup
    always discards the offline backlog.
    """
    client = client or TelegramClient(config.token)
    print("Jarvis Telegram interface starting (read-only). Press Ctrl+C to stop.")

    try:
        offset = client.drain_backlog()
    except TelegramAPIError as e:
        print(f"ERROR: could not start: {e}")
        if e.status_code == 401:
            print("The bot token was rejected -- check TELEGRAM_BOT_TOKEN.")
        elif e.status_code == 409:
            print("Another poller or an active webhook holds this bot; stop the other one first.")
        return 1

    print("Listening. Messages sent while Jarvis was offline were discarded.")
    polls = 0
    try:
        while max_polls is None or polls < max_polls:
            polls += 1
            try:
                updates = client.get_updates(offset=offset, timeout=poll_timeout)
            except TelegramAPIError as e:
                if e.status_code in (401, 404):
                    print(f"ERROR: {e}\nToken rejected -- stopping.")
                    return 1
                if e.status_code == 409:
                    print(f"ERROR: {e}\nAnother poller or webhook is active -- stopping.")
                    return 1
                delay = ERROR_BACKOFF_SECONDS
                if e.status_code == 429 and e.retry_after:
                    delay = min(int(e.retry_after), MAX_RETRY_AFTER_SECONDS)
                _log(f"poll failed ({e}); retrying in {delay}s", config.token)
                sleep(delay)
                continue

            for update in updates:
                update_id = update.get("update_id") if isinstance(update, dict) else None
                if not isinstance(update_id, int):
                    continue  # malformed; nothing safe to do with it
                # Advance the offset BEFORE handling, so nothing that goes
                # wrong while handling (including a failed reply) can
                # cause the same update to be replayed.
                offset = update_id + 1
                try:
                    handle_update(update, config, client)
                except TelegramAPIError as e:
                    _log(f"could not send reply: {e}", config.token)
                except Exception as e:  # one bad update must not stop the bot
                    _log(f"update handling failed: {type(e).__name__}", config.token)
    except KeyboardInterrupt:
        print("\nStopped. Nothing was saved; messages sent while stopped will be discarded on next start.")
    return 0
