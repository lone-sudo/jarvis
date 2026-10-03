"""
Telegram interface tests. The client is tested against a REAL local HTTP
server that imitates Telegram's API (actual sockets, actual request
bodies), and the routing/allowlist logic against a fake client.

Honest scope: nothing here proves the real Telegram service accepts our
requests as written (URL shape, the link-preview fields). That needs a
live round trip on the ZBook with a real bot -- see build-log 0014.
"""

import builtins
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis.interface import telegram_bot as tb
from jarvis.policy.rules import ActionTier
from jarvis.state.tracker import StateTracker

TOKEN = "123456789:AAFakeTokenValueForTestsOnly_abcdefghijk"
OWNER = 4242


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


class FakeClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def _config():
    return tb.TelegramConfig(token=TOKEN, allowed_user_id=OWNER)


def _update(text, user_id=OWNER, chat_id=None, chat_type="private", update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "from": {"id": user_id},
            "chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
        },
    }


# --- Configuration ---

def test_config_requires_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "1")
    with pytest.raises(tb.TelegramConfigError, match="TELEGRAM_BOT_TOKEN"):
        tb.load_config_from_env()


def test_config_rejects_username_instead_of_numeric_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "@lone")
    with pytest.raises(tb.TelegramConfigError, match="number"):
        tb.load_config_from_env()


def test_config_error_never_contains_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "not-a-number")
    with pytest.raises(tb.TelegramConfigError) as exc:
        tb.load_config_from_env()
    assert TOKEN not in str(exc.value)


def test_config_valid(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "4242")
    config = tb.load_config_from_env()
    assert config.allowed_user_id == 4242


# --- Redaction ---

def test_redact_removes_exact_token_and_token_shaped_strings():
    text = f"failed calling https://api.telegram.org/bot{TOKEN}/getUpdates and 999999:ZZZZZZZZZZZZZZZZZZZZZZZZ"
    cleaned = tb.redact(text, TOKEN)
    assert TOKEN not in cleaned
    assert "999999:ZZZZ" not in cleaned
    assert "[REDACTED]" in cleaned


def test_redact_exact_match_works_even_for_a_token_with_an_unexpected_shape():
    """The exact-value branch must stand on its own if Telegram ever changes the token format."""
    odd_token = "totally-different-format-token"
    cleaned = tb.redact(f"url was /bot{odd_token}/getUpdates", odd_token)
    assert odd_token not in cleaned


def test_redact_shape_match_works_even_when_the_token_value_is_unknown():
    cleaned = tb.redact(f"leaked /bot{TOKEN}/getUpdates", token=None)
    assert TOKEN not in cleaned


# --- Authorization: who gets a reply ---

def test_authorized_help_gets_reply():
    client = FakeClient()
    tb.handle_update(_update("/help"), _config(), client)
    assert len(client.sent) == 1
    assert client.sent[0][0] == OWNER
    assert "read-only" in client.sent[0][1]


def test_stranger_gets_no_reply_at_all():
    client = FakeClient()
    tb.handle_update(_update("/resume", user_id=999, chat_id=999), _config(), client)
    assert client.sent == []


def test_group_chat_ignored_even_from_owner():
    client = FakeClient()
    tb.handle_update(_update("/help", chat_type="group"), _config(), client)
    assert client.sent == []


def test_owner_id_in_someone_elses_chat_ignored():
    """sender id matches but the chat id doesn't -- must not be treated as authorized."""
    client = FakeClient()
    tb.handle_update(_update("/help", user_id=OWNER, chat_id=777), _config(), client)
    assert client.sent == []


def test_stranger_message_text_never_logged(capsys):
    client = FakeClient()
    tb.handle_update(_update("SECRET-PAYLOAD", user_id=999, chat_id=999), _config(), client)
    err = capsys.readouterr().err
    assert "999" in err  # sender id is logged locally
    assert "SECRET-PAYLOAD" not in err


def test_non_message_update_ignored():
    client = FakeClient()
    tb.handle_update({"update_id": 5, "edited_message": {}}, _config(), client)
    assert client.sent == []


# --- Command surface: fixed table, no passthrough ---

def test_unknown_command_gets_generic_reply_not_executed():
    client = FakeClient()
    tb.handle_update(_update("/rm -rf /"), _config(), client)
    assert client.sent[0][1] == "Unknown command. Try /help."


def test_free_text_is_not_a_command():
    client = FakeClient()
    tb.handle_update(_update("please delete my inbox"), _config(), client)
    assert client.sent[0][1] == "Unknown command. Try /help."


def test_botname_suffix_is_stripped():
    client = FakeClient()
    tb.handle_update(_update("/help@JarvisBot"), _config(), client)
    assert "read-only" in client.sent[0][1]


def test_every_registered_command_is_observe_tier():
    """Guard against someone later registering a write command."""
    assert all(cmd.tier == ActionTier.OBSERVE for cmd in tb.COMMANDS.values())
    assert set(tb.COMMANDS) == {"/resume", "/inbox", "/help", "/start"}


def test_non_observe_command_refused_at_dispatch(monkeypatch):
    monkeypatch.setitem(
        tb.COMMANDS, "/danger",
        tb.Command(lambda args: "should never run", ActionTier.SAFE_WRITE, "/danger"),
    )
    client = FakeClient()
    tb.handle_update(_update("/danger"), _config(), client)
    assert "isn't available over Telegram" in client.sent[0][1]
    assert "should never run" not in client.sent[0][1]


def test_input_is_unavailable_to_handlers(monkeypatch):
    """
    A command that tries to ask for confirmation must fail, not proceed.
    The stand-in input() below answers "y" -- so if the guard were missing,
    the handler would succeed and the reply would be "y". (An earlier
    version of this test passed for the wrong reason: pytest's own stdin
    capture makes a raw input() fail regardless of our guard.)
    """
    def fake_user_input(prompt=""):
        return "y"

    monkeypatch.setattr(builtins, "input", fake_user_input)

    def asks_for_confirmation(_args):
        return tb._run_captured(lambda: print(builtins.input("Proceed? [y/N]: ")))

    monkeypatch.setitem(
        tb.COMMANDS, "/ask",
        tb.Command(asks_for_confirmation, ActionTier.OBSERVE, "/ask"),
    )
    client = FakeClient()
    tb.handle_update(_update("/ask"), _config(), client)
    assert "command failed" in client.sent[0][1].lower()
    assert client.sent[0][1].strip() != "y"
    assert builtins.input is fake_user_input  # and the real input() is restored afterward


def test_handler_exception_does_not_leak_details_or_crash():
    def boom(_args):
        raise RuntimeError(f"secret detail {TOKEN}")

    tb.COMMANDS["/boom"] = tb.Command(boom, ActionTier.OBSERVE, "/boom")
    try:
        client = FakeClient()
        tb.handle_update(_update("/boom"), _config(), client)
    finally:
        del tb.COMMANDS["/boom"]
    assert TOKEN not in client.sent[0][1]
    assert "secret detail" not in client.sent[0][1]


def test_long_reply_truncated():
    tb.COMMANDS["/long"] = tb.Command(lambda a: "x" * 10000, ActionTier.OBSERVE, "/long")
    try:
        client = FakeClient()
        tb.handle_update(_update("/long"), _config(), client)
    finally:
        del tb.COMMANDS["/long"]
    assert len(client.sent[0][1]) < 4096
    assert client.sent[0][1].endswith("[truncated]")


# --- Real existing commands, read-only ---

def test_resume_runs_existing_command_with_no_active_tasks():
    client = FakeClient()
    tb.handle_update(_update("/resume"), _config(), client)
    assert "starting fresh" in client.sent[0][1].lower()


def test_resume_shows_active_task_and_changes_nothing():
    """
    The realistic common case: a registered project whose directory
    actually exists on disk (and is a real git repo, since cmd_resume
    queries git status/branch/log).
    """
    import subprocess
    from jarvis.policy.rules import SecurityPolicy

    tracker = StateTracker()
    tracker.register_project("proj", "proj")
    project_dir = SecurityPolicy.get_workspace_root() / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=project_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project_dir, check=True)

    session_id = tracker.create_session("goal")
    task_id = tracker.create_task(session_id, "Fix the thing", "proj")

    client = FakeClient()
    tb.handle_update(_update("/resume"), _config(), client)
    assert "Fix the thing" in client.sent[0][1]

    active = tracker.get_active_tasks()
    assert [t["task_id"] for t in active] == [task_id]
    assert active[0]["status"] == "PENDING"  # untouched


def test_resume_handles_registered_project_whose_directory_does_not_exist_on_disk():
    """
    Regression test for the real bug Lone's Windows run caught (build-log
    0015): a project can be registered (its row exists) without its
    directory ever being created on disk -- e.g. before `git clone`, or
    if it was moved/deleted afterward. cmd_resume must degrade
    gracefully (shows the task, reports the git context as unavailable)
    rather than crashing the whole reply. Deliberately does NOT create
    the "proj" directory.
    """
    tracker = StateTracker()
    tracker.register_project("proj", "proj")  # directory is never created
    session_id = tracker.create_session("goal")
    task_id = tracker.create_task(session_id, "Fix the thing", "proj")

    client = FakeClient()
    tb.handle_update(_update("/resume"), _config(), client)
    reply = client.sent[0][1]
    assert "Fix the thing" in reply  # task info still shown
    assert "does not exist" in reply  # git context fails gracefully, not a crash
    assert "command failed" not in reply.lower()  # proves it didn't crash


def test_inbox_lists_items_and_filters_by_status():
    from jarvis.state.inbox import InboxStore
    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    store.create_item("A saved note", None, "", "inbox/x.md")

    client = FakeClient()
    tb.handle_update(_update("/inbox"), _config(), client)
    assert "A saved note" in client.sent[0][1]

    tb.handle_update(_update("/inbox archived"), _config(), client)
    assert "A saved note" not in client.sent[1][1]


def test_inbox_bad_argument_gets_usage_not_passed_through():
    client = FakeClient()
    tb.handle_update(_update("/inbox ; drop table"), _config(), client)
    assert client.sent[0][1].startswith("Usage:")


# --- Client against a real local HTTP server imitating Telegram ---

class _FakeTelegram(BaseHTTPRequestHandler):
    requests_seen = []
    responses = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        _FakeTelegram.requests_seen.append((self.path, body))
        method = self.path.rsplit("/", 1)[-1]
        status, payload = _FakeTelegram.responses.get(method, (200, {"ok": True, "result": []}))
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_telegram():
    _FakeTelegram.requests_seen = []
    _FakeTelegram.responses = {}
    server = HTTPServer(("127.0.0.1", 0), _FakeTelegram)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_client_get_updates_request_shape(fake_telegram):
    _FakeTelegram.responses["getUpdates"] = (200, {"ok": True, "result": [{"update_id": 7}]})
    client = tb.TelegramClient(TOKEN, base_url=fake_telegram)
    result = client.get_updates(offset=8, timeout=0)
    assert result == [{"update_id": 7}]
    path, body = _FakeTelegram.requests_seen[0]
    assert path == f"/bot{TOKEN}/getUpdates"
    assert body["offset"] == 8
    assert body["allowed_updates"] == ["message"]  # only messages are ever requested


def test_client_send_message_plain_text_no_previews(fake_telegram):
    client = tb.TelegramClient(TOKEN, base_url=fake_telegram)
    client.send_message(OWNER, "hello")
    _, body = _FakeTelegram.requests_seen[0]
    assert body["chat_id"] == OWNER
    assert body["text"] == "hello"
    assert "parse_mode" not in body
    assert body["link_preview_options"] == {"is_disabled": True}


def test_client_http_error_never_leaks_token(fake_telegram):
    # The fake server ECHOES the token in its error text, so this only
    # passes if redaction is genuinely applied to error descriptions.
    _FakeTelegram.responses["getUpdates"] = (401, {"ok": False, "description": f"Unauthorized for bot{TOKEN}"})
    client = tb.TelegramClient(TOKEN, base_url=fake_telegram)
    with pytest.raises(tb.TelegramAPIError) as exc:
        client.get_updates(offset=None, timeout=0)
    assert exc.value.status_code == 401
    assert TOKEN not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_client_ok_false_response_redacts_token(fake_telegram):
    _FakeTelegram.responses["getUpdates"] = (200, {"ok": False, "description": f"bad bot{TOKEN}"})
    client = tb.TelegramClient(TOKEN, base_url=fake_telegram)
    with pytest.raises(tb.TelegramAPIError) as exc:
        client.get_updates(offset=None, timeout=0)
    assert TOKEN not in str(exc.value)


def test_client_non_json_response_suppresses_original_exception(fake_telegram):
    _FakeTelegram.responses["getUpdates"] = (200, b"<html>not json</html>")
    client = tb.TelegramClient(TOKEN, base_url=fake_telegram)
    with pytest.raises(tb.TelegramAPIError) as exc:
        client.get_updates(offset=None, timeout=0)
    assert exc.value.__suppress_context__  # no chained traceback carrying request details


def test_client_network_error_never_leaks_token():
    client = tb.TelegramClient(TOKEN, base_url="http://127.0.0.1:1")  # nothing listening
    with pytest.raises(tb.TelegramAPIError) as exc:
        client.get_updates(offset=None, timeout=0)
    assert TOKEN not in str(exc.value)
    assert exc.value.__suppress_context__


def test_client_ignores_proxy_environment(fake_telegram, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    client = tb.TelegramClient(TOKEN, base_url=fake_telegram)
    assert client.get_updates(offset=None, timeout=0) == []  # reached the server directly


def test_drain_backlog_discards_old_updates(fake_telegram):
    calls = {"n": 0}

    class Scripted(tb.TelegramClient):
        def get_updates(self, offset, timeout):
            calls["n"] += 1
            if offset == -1:
                return [{"update_id": 50}]
            return []

    assert Scripted(TOKEN, base_url=fake_telegram).drain_backlog() == 51


# --- The polling loop ---

class ScriptedClient(FakeClient):
    def __init__(self, batches, drain_offset=None):
        super().__init__()
        self.batches = list(batches)
        self.offsets = []
        self.drained = False
        self._drain_offset = drain_offset

    def drain_backlog(self):
        self.drained = True
        return self._drain_offset

    def get_updates(self, offset, timeout):
        self.offsets.append(offset)
        item = self.batches.pop(0) if self.batches else []
        if isinstance(item, Exception):
            raise item
        return item


def test_loop_drains_backlog_first_then_handles_messages(capsys):
    client = ScriptedClient([[_update("/help", update_id=10)]], drain_offset=10)
    code = tb.run_bot(_config(), client=client, max_polls=1, sleep=lambda s: None)
    assert code == 0
    assert client.drained
    assert client.offsets == [10]
    assert len(client.sent) == 1


def test_loop_does_not_replay_update_when_reply_fails_to_send():
    """The real replay risk: the reply can't be sent (network blip)."""
    class FailingSend(ScriptedClient):
        def send_message(self, chat_id, text):
            raise tb.TelegramAPIError("send failed")

    client = FailingSend([[_update("/help", update_id=30)], []])
    tb.run_bot(_config(), client=client, max_polls=2, sleep=lambda s: None)
    assert client.offsets[1] == 31  # moved past it; not fetched again


def test_loop_survives_unexpected_exception_and_does_not_replay(monkeypatch):
    def explode(update, config, client):
        raise ValueError("unexpected bug")

    monkeypatch.setattr(tb, "handle_update", explode)
    client = ScriptedClient([[_update("/help", update_id=40)], []])
    assert tb.run_bot(_config(), client=client, max_polls=2, sleep=lambda s: None) == 0
    assert client.offsets[1] == 41


def test_loop_skips_malformed_update_without_crashing():
    client = ScriptedClient([[{"no_update_id": True}, "garbage", _update("/help", update_id=50)]])
    assert tb.run_bot(_config(), client=client, max_polls=1, sleep=lambda s: None) == 0
    assert len(client.sent) == 1  # the valid one after the junk was still handled


def test_loop_advances_offset_even_if_handler_fails():
    tb.COMMANDS["/boom"] = tb.Command(lambda a: 1 / 0, ActionTier.OBSERVE, "/boom")
    try:
        client = ScriptedClient([[_update("/boom", update_id=20)], []])
        tb.run_bot(_config(), client=client, max_polls=2, sleep=lambda s: None)
    finally:
        del tb.COMMANDS["/boom"]
    assert client.offsets[1] == 21  # not replayed


def test_loop_stops_on_rejected_token(capsys):
    client = ScriptedClient([tb.TelegramAPIError("bad", status_code=401)])
    assert tb.run_bot(_config(), client=client, max_polls=5, sleep=lambda s: None) == 1
    assert TOKEN not in capsys.readouterr().out


def test_loop_stops_on_webhook_or_second_poller_conflict(capsys):
    client = ScriptedClient([tb.TelegramAPIError("conflict", status_code=409)])
    assert tb.run_bot(_config(), client=client, max_polls=5, sleep=lambda s: None) == 1
    assert "webhook" in capsys.readouterr().out.lower()


def test_loop_backs_off_on_transient_error_then_continues():
    delays = []
    client = ScriptedClient([tb.TelegramAPIError("down"), [_update("/help", update_id=1)]])
    tb.run_bot(_config(), client=client, max_polls=2, sleep=delays.append)
    assert delays == [tb.ERROR_BACKOFF_SECONDS]
    assert len(client.sent) == 1


def test_loop_respects_retry_after_but_caps_it():
    delays = []
    client = ScriptedClient([tb.TelegramAPIError("slow down", status_code=429, retry_after=9999), []])
    tb.run_bot(_config(), client=client, max_polls=2, sleep=delays.append)
    assert delays == [tb.MAX_RETRY_AFTER_SECONDS]


def test_startup_failure_reports_and_exits(capsys):
    class Failing(ScriptedClient):
        def drain_backlog(self):
            raise tb.TelegramAPIError("nope", status_code=401)

    assert tb.run_bot(_config(), client=Failing([]), max_polls=1) == 1
    assert "token was rejected" in capsys.readouterr().out.lower()


def test_ctrl_c_stops_cleanly(capsys):
    class Interrupting(ScriptedClient):
        def get_updates(self, offset, timeout):
            raise KeyboardInterrupt

    assert tb.run_bot(_config(), client=Interrupting([]), sleep=lambda s: None) == 0
    assert "Stopped" in capsys.readouterr().out


def test_nothing_is_written_to_disk_by_the_loop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(p.name for p in tmp_path.iterdir())
    client = ScriptedClient([[_update("/help", update_id=1)]])
    tb.run_bot(_config(), client=client, max_polls=1, sleep=lambda s: None)
    assert set(p.name for p in tmp_path.iterdir()) == before


# --- CLI wiring ---

def test_cli_telegram_start_without_env_gives_clear_error(monkeypatch, capsys):
    import sys
    from jarvis.interface.cli import main
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["jarvis", "telegram-start"])
    main()
    out = capsys.readouterr().out
    assert "TELEGRAM_BOT_TOKEN is not set" in out
