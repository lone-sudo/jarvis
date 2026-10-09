"""
CLI-level tests for M002: plan-create / plan-run / plan-show / rollback.
Engine mechanics are covered in test_engine_core.py; these check the
wiring, the messages a human actually sees, and the full
block -> inspect -> roll back loop through main().
"""

import json
import re
import subprocess
import sys

import pytest

from jarvis.core import engine as engine_mod
from jarvis.core.plan import VerificationResult
from jarvis.interface.cli import main
from jarvis.state.plan_store import PlanStore
from jarvis.state.tracker import StateTracker


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def run_cli(argv, capsys, monkeypatch=None, answer=None):
    if monkeypatch is not None and answer is not None:
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    old = sys.argv
    sys.argv = ["jarvis"] + argv
    try:
        main()
    finally:
        sys.argv = old
    return capsys.readouterr().out


class _Verifier:
    def __init__(self, ok, detail):
        self.ok, self.detail = ok, detail

    def verify(self, step, ctx):
        return VerificationResult(self.ok, self.detail)


@pytest.fixture
def env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    repo = workspace / "proj"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial")

    tracker = StateTracker()
    tracker.register_project("proj", "proj")
    task = tracker.create_task(tracker.create_session("goal"), "t", "proj")
    (workspace / "plans").mkdir()

    monkeypatch.setattr(engine_mod, "load_default_verifiers", lambda: {
        "pass": lambda spec: _Verifier(True, "ok"),
        "fail": lambda spec: _Verifier(False, "expected 2 got 3\n1 failed, 4 passed"),
    })
    return type("Env", (), {"workspace": workspace, "repo": repo, "task": task, "tracker": tracker})


def write_plan(env, name, steps, **extra):
    path = env.workspace / "plans" / name
    path.write_text(json.dumps({"steps": steps, **extra}))
    return f"plans/{name}"


def create(env, capsys, steps, **kw):
    rel = write_plan(env, "plan.json", steps, **kw.pop("extra", {}))
    out = run_cli(["plan-create", "--task", env.task, "--file", rel, *kw.pop("flags", [])], capsys)
    m = re.search(r"PLN-[0-9A-F]{8}", out)
    return (m.group(0) if m else None), out


READ = [{"tool": "read_file", "params": {"path": "a.txt"}}]
WRITE_FAILING = [{"tool": "write_file", "params": {"path": "a.txt", "content": "v2\n"},
                  "verify": {"type": "fail"}, "description": "bump a"}]


def test_plan_create_stores_plan_and_runs_nothing(env, capsys):
    plan_id, out = create(env, capsys, READ + [{"tool": "list_directory"}])
    assert plan_id and "Created plan" in out and "2 steps" in out and "Nothing has run" in out
    stored = PlanStore(env.tracker.db_path).load(plan_id)
    assert stored.status.value == "PENDING" and len(stored.steps) == 2


def test_plan_create_flags_override_limits(env, capsys):
    many = [{"tool": "list_directory"}] * 6
    _, out = create(env, capsys, many)
    assert "ERROR: plan rejected" in out and "max_steps is 5" in out
    plan_id, out = create(env, capsys, many, flags=["--max-steps", "6", "--timeout", "300"])
    stored = PlanStore(env.tracker.db_path).load(plan_id)
    assert stored.max_steps == 6 and stored.timeout_seconds == 300


@pytest.mark.parametrize("steps, text", [
    ([{"tool": "format_disk", "params": {}}], "unknown tool"),
    ([{"tool": "write_file", "params": {"path": "x", "content": "y"}}], "needs a 'verify'"),
    ([], "at least one step"),
])
def test_plan_create_rejects_bad_plans(env, capsys, steps, text):
    plan_id, out = create(env, capsys, steps)
    assert plan_id is None and "ERROR: plan rejected" in out and text in out


def test_plan_create_rejects_file_outside_workspace(env, capsys, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"steps": READ}))
    out = run_cli(["plan-create", "--task", env.task, "--file", str(outside)], capsys)
    assert "rejected by policy" in out
    assert "PLN-" not in out


def test_plan_create_input_errors(env, capsys):
    (env.workspace / "plans" / "bad.json").write_text("{nope")
    assert "could not read plan file as JSON" in run_cli(
        ["plan-create", "--task", env.task, "--file", "plans/bad.json"], capsys)
    assert "is not a file" in run_cli(
        ["plan-create", "--task", env.task, "--file", "plans/missing.json"], capsys)
    rel = write_plan(env, "mismatch.json", READ, task_id="TSK-OTHER000")
    assert "names task TSK-OTHER000" in run_cli(["plan-create", "--task", env.task, "--file", rel], capsys)
    rel = write_plan(env, "ok.json", READ)
    assert "No task found" in run_cli(["plan-create", "--task", "TSK-NOPE0000", "--file", rel], capsys)


def test_plan_create_accepts_a_file_with_a_utf8_bom(env, capsys):
    # Windows PowerShell 5.1 `Set-Content -Encoding utf8` writes a BOM.
    path = env.workspace / "plans" / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"steps": READ}).encode("utf-8"))
    out = run_cli(["plan-create", "--task", env.task, "--file", "plans/bom.json"], capsys)
    assert "Created plan" in out and "ERROR" not in out


def test_plan_create_refuses_engine_owned_fields(env, capsys):
    plan_id, out = create(env, capsys, READ, extra={"status": "COMPLETED"})
    assert plan_id is None and "engine-owned" in out


def test_plan_run_read_only_completes_and_plan_show_reflects_it(env, capsys):
    plan_id, _ = create(env, capsys, READ + [{"tool": "list_directory"}])
    out = run_cli(["plan-run", plan_id], capsys)
    assert "COMPLETED (2/2 steps)" in out
    shown = run_cli(["plan-show", plan_id], capsys)
    assert "status=COMPLETED" in shown and "[SUCCEEDED] read_file" in shown and "[SUCCEEDED] list_directory" in shown


def test_plan_show_does_not_dump_file_content(env, capsys):
    secret = "TOP-SECRET-PAYLOAD " * 20
    plan_id, _ = create(env, capsys, [{"tool": "write_file", "params": {"path": "s.txt", "content": secret},
                                       "verify": {"type": "pass"}}])
    shown = run_cli(["plan-show", plan_id], capsys)
    assert "TOP-SECRET-PAYLOAD" not in shown and f"content=<{len(secret)} chars>" in shown


def test_blocked_plan_is_inspectable_with_saved_diagnostics(env, capsys, monkeypatch):
    plan_id, _ = create(env, capsys, WRITE_FAILING)
    out = run_cli(["plan-run", plan_id], capsys, monkeypatch, "y")
    assert "BLOCKED at step 1/1 (write_file)" in out and "Nothing was rolled back or deleted" in out

    shown = run_cli(["plan-show", plan_id], capsys)
    assert "status=BLOCKED" in shown and "[FAILED] write_file" in shown
    assert "expected 2 got 3" in shown and "+v2" in shown          # evidence, from SQLite
    assert f"jarvis plan-run {plan_id}" in shown and "jarvis rollback --checkpoint CHK-" in shown
    assert (env.repo / "a.txt").read_text() == "v2\n"               # and still on disk


def test_plan_show_clips_long_diagnostics_unless_full(env, capsys, monkeypatch):
    long = "line\n" * 2000
    plan_id, _ = create(env, capsys, [{"tool": "write_file", "params": {"path": "a.txt", "content": long},
                                       "verify": {"type": "fail"}}])
    run_cli(["plan-run", plan_id], capsys, monkeypatch, "y")
    assert "clipped, use --full" in run_cli(["plan-show", plan_id], capsys)
    assert "clipped" not in run_cli(["plan-show", plan_id, "--full"], capsys)


def test_full_loop_block_then_rollback_through_the_cli(env, capsys, monkeypatch):
    plan_id, _ = create(env, capsys, WRITE_FAILING)
    run_cli(["plan-run", plan_id], capsys, monkeypatch, "y")
    checkpoint = PlanStore(env.tracker.db_path).load(plan_id).checkpoint_id
    assert (env.repo / "a.txt").read_text() == "v2\n"

    out = run_cli(["rollback", "--checkpoint", checkpoint], capsys, monkeypatch, "y")
    assert "Proposed rollback" in out and "a.txt" in out and "Rolled back" in out
    assert (env.repo / "a.txt").read_text() == "v1\n"
    assert "status=PENDING" in run_cli(["plan-show", plan_id], capsys)   # must not resume past undone work


def test_rollback_declined_leaves_changes(env, capsys, monkeypatch):
    plan_id, _ = create(env, capsys, WRITE_FAILING)
    run_cli(["plan-run", plan_id], capsys, monkeypatch, "y")
    checkpoint = PlanStore(env.tracker.db_path).load(plan_id).checkpoint_id
    out = run_cli(["rollback", "--checkpoint", checkpoint], capsys, monkeypatch, "n")
    assert "declined" in out.lower()
    assert (env.repo / "a.txt").read_text() == "v2\n"


def test_user_declining_the_write_prompt_blocks_the_plan(env, capsys, monkeypatch):
    plan_id, _ = create(env, capsys, [{"tool": "write_file", "params": {"path": "n.txt", "content": "n"},
                                       "verify": {"type": "pass"}}])
    out = run_cli(["plan-run", plan_id], capsys, monkeypatch, "n")
    assert "BLOCKED" in out and "DECLINED" in out
    assert not (env.repo / "n.txt").exists()


def test_unknown_ids_report_errors_instead_of_crashing(env, capsys):
    assert "ERROR: No plan found" in run_cli(["plan-run", "PLN-NOPE0000"], capsys)
    assert "ERROR: No plan found" in run_cli(["plan-show", "PLN-NOPE0000"], capsys)
    assert "ERROR: No checkpoint found" in run_cli(["rollback", "--checkpoint", "CHK-NOPE0000"], capsys)


def test_plan_run_without_verifier_blocks_cleanly_before_any_write(env, capsys, monkeypatch):
    monkeypatch.setattr(engine_mod, "load_default_verifiers", lambda: {})  # e.g. verifier.py not merged yet
    plan_id, _ = create(env, capsys, [{"tool": "write_file", "params": {"path": "n.txt", "content": "n"},
                                       "verify": {"type": "file_content", "path": "n.txt", "content": "n"}}])
    out = run_cli(["plan-run", plan_id], capsys)
    assert "BLOCKED before any step ran" in out and "no verifier is installed for type 'file_content'" in out
    assert not (env.repo / "n.txt").exists()
