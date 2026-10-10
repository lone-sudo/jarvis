import importlib
import json
import sqlite3
import subprocess
import types
from types import SimpleNamespace

import pytest

from jarvis.core import engine as engine_mod
from jarvis.core.engine import (
    EngineError,
    ExecutionEngine,
    TOOL_REGISTRY,
    _classify,
    load_default_verifiers,
    validate_plan,
)
from jarvis.core.plan import (
    ExecutionPlan,
    ExecutionStep,
    PlanFormatError,
    PlanStatus,
    StepStatus,
    VerificationResult,
)
from jarvis.policy.validator import PolicyViolation
from jarvis.state.plan_store import PlanNotFound, PlanStore, get_task_row
from jarvis.state.tracker import StateTracker
from jarvis.tools.fs import FileSystemInspector


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


# ---- fake verifiers (the real ones are Antigravity's, core/verifier.py) ----

class _V:
    def __init__(self, fn):
        self.fn = fn

    def verify(self, step, ctx):
        return self.fn(step, ctx)


class FileHasContent:
    def __init__(self, spec):
        self.spec = spec

    def verify(self, step, ctx):
        p = ctx.project_root / self.spec["path"]
        ok = p.is_file() and p.read_text() == self.spec["content"]
        return VerificationResult(ok, f"{p} content ok={ok}")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


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
    plain = workspace / "plain"
    plain.mkdir()
    (plain / "p.txt").write_text("plain\n")

    tracker = StateTracker(db_path=tmp_path / "s.db")
    tracker.register_project("proj", "proj")
    tracker.register_project("plain", "plain")
    session = tracker.create_session("goal")
    store = PlanStore(tracker.db_path)
    lines = []
    clock = FakeClock()
    seen_ctx = []

    def capture(step, ctx):
        seen_ctx.append(ctx)
        return VerificationResult(True, "captured")

    factories = {
        "file_content": FileHasContent,
        "ok": lambda spec: _V(lambda s, c: VerificationResult(True, "fine")),
        "fail": lambda spec: _V(lambda s, c: VerificationResult(False, "expected X\nbut got Y\n1 failed")),
        "crash": lambda spec: _V(lambda s, c: (_ for _ in ()).throw(RuntimeError("verifier exploded"))),
        "slow": lambda spec: _V(lambda s, c: (setattr(clock, "now", clock.now + 100), VerificationResult(True, "slow"))[1]),
        "capture": lambda spec: _V(capture),
    }
    return SimpleNamespace(
        tracker=tracker, store=store, repo=repo, plain=plain, workspace=workspace, lines=lines,
        clock=clock, factories=factories, seen_ctx=seen_ctx,
        task=tracker.create_task(session, "git task", "proj"),
        plain_task=tracker.create_task(session, "plain task", "plain"),
    )


def make_engine(env, auto_confirm=True, **kw):
    return ExecutionEngine(
        env.tracker, env.store, verifier_factories=kw.pop("factories", env.factories),
        auto_confirm=auto_confirm, time_fn=env.clock, out=env.lines.append, **kw,
    )


def make_plan(env, steps, task=None, **limits):
    plan = ExecutionPlan.from_dict({"task_id": task or env.task, "steps": steps, **limits}, require_state=False)
    validate_plan(plan)
    return env.store.create(plan)


def write(path, content, verify=None, **extra):
    step = {"tool": "write_file", "params": {"path": path, "content": content},
            "verify": verify or {"type": "file_content", "path": path, "content": content}}
    step.update(extra)
    return step


def rows(env, task=None):
    conn = sqlite3.connect(env.tracker.db_path)
    return conn.execute(
        "SELECT tool_name, status FROM executions WHERE task_id=? ORDER BY rowid", (task or env.task,)
    ).fetchall()


def diag(env, plan):
    return env.store.get_failure_diagnostics(plan.plan_id, plan.task_id)


# ---------------------------------------------------------------- happy path

def test_happy_path_runs_every_step_and_persists(env):
    plan = make_plan(env, [
        {"tool": "read_file", "params": {"path": "a.txt"}},
        write("new.txt", "hello\n"),
        {"tool": "list_directory"},
    ])
    done = make_engine(env).run(plan.plan_id)

    assert done.status is PlanStatus.COMPLETED
    assert (env.repo / "new.txt").read_text() == "hello\n"
    reloaded = env.store.load(plan.plan_id)
    assert reloaded.status is PlanStatus.COMPLETED and reloaded.current_step_index == 3
    assert all(s.status is StepStatus.SUCCEEDED for s in reloaded.steps)
    assert reloaded.checkpoint_id and reloaded.checkpoint_id.startswith("CHK-")
    assert get_task_row(env.tracker.db_path, env.task)["status"] == "IN_PROGRESS"  # engine never auto-DONEs a task
    assert rows(env) == [
        ("read_file", "SUCCESS"), ("write_file", "SUCCESS"), ("verify:file_content", "SUCCESS"),
        ("list_directory", "SUCCESS"),
    ]


def test_write_then_commit_through_the_engine(env):
    plan = make_plan(env, [
        write("c.txt", "committed\n"),
        {"tool": "git_commit", "params": {"message": "engine commit"}, "verify": {"type": "ok"}},
    ])
    assert make_engine(env).run(plan.plan_id).status is PlanStatus.COMPLETED
    assert git(env.repo, "log", "-1", "--format=%s") == "engine commit"


def test_verifier_receives_absolute_paths_and_remaining_budget(env):
    plan = make_plan(env, [write("n.txt", "x", verify={"type": "capture"})], timeout_seconds=90)
    make_engine(env).run(plan.plan_id)
    ctx = env.seen_ctx[0]
    assert ctx.project_root == env.repo.resolve() and ctx.project_root.is_absolute()
    assert ctx.workspace_root == env.workspace.resolve()
    assert 0 <= ctx.time_remaining <= 90


# ---------------------------------------------------- per-step authorization

@pytest.mark.parametrize("bad_path", ["../../outside.txt", "../other/escape.txt", "/etc/passwd-copy"])
def test_step_outside_workspace_is_denied_and_nothing_is_written(env, bad_path, tmp_path):
    plan = make_plan(env, [
        write("ok.txt", "fine\n"),
        write(bad_path, "evil\n", verify={"type": "ok"}),
        write("never.txt", "later\n"),
    ])
    blocked = make_engine(env).run(plan.plan_id)

    assert blocked.status is PlanStatus.BLOCKED and blocked.blocked_reason.startswith("POLICY")
    assert [s.status for s in blocked.steps] == [StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.PENDING]
    assert (env.repo / "ok.txt").exists() and not (env.repo / "never.txt").exists()
    assert not (tmp_path / "outside.txt").exists()
    assert not (env.workspace / "other").exists()
    assert ("write_file", "REJECTED_BY_POLICY") in rows(env)
    assert diag(env, blocked)["kind"] == "POLICY"


def test_engine_authorizes_every_step_itself_not_only_via_the_tools(env):
    # The tools also check paths, but the engine's check must stand on its own
    # (decision 1: per-step re-authorization), so call it directly.
    eng = make_engine(env)
    for tool, params in [
        ("write_file", {"path": "../../escape.txt", "content": "x"}),
        ("read_file", {"path": "../../escape.txt"}),
        ("list_directory", {"path": "../.."}),
        ("write_file", {"path": "/etc/hosts", "content": "x"}),
    ]:
        with pytest.raises(PolicyViolation):
            eng._authorize_step(ExecutionStep(tool=tool, params=params), "proj")
    # and the legitimate forms pass
    assert eng._authorize_step(ExecutionStep(tool="write_file", params={"path": "sub/ok.txt", "content": ""}), "proj")
    assert eng._authorize_step(ExecutionStep(tool="git_commit", params={"message": "m"}), "proj")


def test_mutations_are_confined_to_the_project_but_reads_may_cross_projects(env):
    # Rollback only covers the task's own repo, so writes must not leave it,
    # even for a path that is still inside the workspace.
    (env.plain / "sib.txt").write_text("sibling\n")
    eng = make_engine(env)
    read_plan = make_plan(env, [{"tool": "read_file", "params": {"path": "../plain/sib.txt"}}])
    assert eng.run(read_plan.plan_id).status is PlanStatus.COMPLETED

    write_plan = make_plan(env, [write("../plain/sib.txt", "overwritten\n", verify={"type": "ok"})])
    blocked = eng.run(write_plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED and "outside the task's project" in blocked.blocked_reason
    assert (env.plain / "sib.txt").read_text() == "sibling\n"


def test_symlink_inside_project_cannot_smuggle_a_write_out(env):
    (env.workspace / "elsewhere").mkdir()
    try:
        (env.repo / "link").symlink_to(env.workspace / "elsewhere", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this platform")
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-m", "link")
    plan = make_plan(env, [write("link/pwn.txt", "x", verify={"type": "ok"})])
    blocked = make_engine(env).run(plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED
    assert not (env.workspace / "elsewhere" / "pwn.txt").exists()


def test_approving_a_plan_does_not_skip_per_step_confirmation(env, monkeypatch):
    plan = make_plan(env, [write("one.txt", "1\n"), write("two.txt", "2\n")])
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "y")
    done = make_engine(env, auto_confirm=None).run(plan.plan_id)
    assert done.status is PlanStatus.COMPLETED
    assert len(prompts) == 2 and all("[y/N]" in p for p in prompts)  # one real prompt per mutating step


def test_user_declining_a_step_blocks_the_plan_and_writes_nothing(env):
    plan = make_plan(env, [write("declined.txt", "no\n")])
    blocked = make_engine(env, auto_confirm=False).run(plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED and blocked.blocked_reason.startswith("DECLINED")
    assert not (env.repo / "declined.txt").exists()
    assert ("write_file", "REJECTED_BY_POLICY") in rows(env)


def test_unregistered_tool_is_denied_by_default(env):
    eng = make_engine(env)
    with pytest.raises(PolicyViolation, match="deny by default"):
        eng._authorize_step(ExecutionStep(tool="rm_rf", params={"path": "x"}), "proj")

    # Defense in depth: even if validation were bypassed and such a plan reached storage.
    raw = env.store.create(ExecutionPlan(task_id=env.task, steps=[ExecutionStep(tool="rm_rf", params={"path": "a.txt"})]))
    blocked = eng.run(raw.plan_id)
    assert blocked.status is PlanStatus.BLOCKED and diag(env, blocked)["kind"] == "PREFLIGHT"
    assert (env.repo / "a.txt").read_text() == "v1\n"


# ----------------------------------------------------------------- pre-flight

def test_mutating_step_without_verify_is_rejected_at_creation_and_at_run(env):
    unverified = {"tool": "write_file", "params": {"path": "x.txt", "content": "x"}}
    with pytest.raises(PlanFormatError, match="needs a 'verify'"):
        make_plan(env, [unverified])

    raw = env.store.create(ExecutionPlan(
        task_id=env.task, steps=[ExecutionStep(tool="write_file", params={"path": "x.txt", "content": "x"})],
    ))
    blocked = make_engine(env).run(raw.plan_id)
    assert blocked.status is PlanStatus.BLOCKED and not (env.repo / "x.txt").exists()


@pytest.mark.parametrize("step, message", [
    ({"tool": "nope"}, "unknown tool"),
    ({"tool": "read_file", "params": {}}, "missing param"),
    ({"tool": "read_file", "params": {"path": "a", "extra": "z"}}, "unexpected param"),
    ({"tool": "read_file", "params": {"path": 5}}, "must be a string"),
    ({"tool": "read_file", "params": {"path": "  "}}, "must not be empty"),
    ({"tool": "git_commit", "params": {"message": " "}, "verify": {"type": "ok"}}, "must not be empty"),
])
def test_validate_plan_rejects_bad_steps(env, step, message):
    with pytest.raises(PlanFormatError, match=message):
        make_plan(env, [step])


def test_max_steps_boundary_enforced(env):
    too_many = [{"tool": "list_directory"}] * 6
    with pytest.raises(PlanFormatError, match="max_steps is 5"):
        make_plan(env, too_many)
    ok = make_plan(env, too_many, max_steps=6)
    assert make_engine(env).run(ok.plan_id).status is PlanStatus.COMPLETED


def test_missing_verifier_blocks_before_any_step_runs(env):
    plan = make_plan(env, [
        write("first.txt", "1\n"),
        write("second.txt", "2\n", verify={"type": "pytest_target"}),  # not installed
    ])
    blocked = make_engine(env).run(plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED
    assert "pytest_target" in blocked.blocked_reason and "No step was run" in blocked.blocked_reason
    assert not (env.repo / "first.txt").exists()  # even the FIRST step did not run
    assert blocked.current_step_index == 0


def test_verifier_that_rejects_its_spec_blocks_preflight(env):
    def strict(spec):
        raise ValueError("needs a 'target'")
    plan = make_plan(env, [write("s.txt", "s", verify={"type": "strict"})])
    blocked = make_engine(env, factories={"strict": strict}).run(plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED and "needs a 'target'" in blocked.blocked_reason
    assert not (env.repo / "s.txt").exists()


def test_write_plan_on_non_git_project_is_blocked_before_writing(env):
    plan = make_plan(env, [write("w.txt", "w\n")], task=env.plain_task)
    blocked = make_engine(env).run(plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED and "rollback point" in blocked.blocked_reason
    assert not (env.plain / "w.txt").exists()


def test_read_only_plan_on_non_git_project_still_runs(env):
    plan = make_plan(env, [{"tool": "read_file", "params": {"path": "p.txt"}}], task=env.plain_task)
    done = make_engine(env).run(plan.plan_id)
    assert done.status is PlanStatus.COMPLETED and done.checkpoint_id is None
    assert any("no checkpoint taken" in l for l in env.lines)


def test_write_plan_on_dirty_tracked_tree_is_blocked_before_writing(env):
    (env.repo / "a.txt").write_text("my wip\n")
    plan = make_plan(env, [write("w.txt", "w\n")])
    blocked = make_engine(env).run(plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED and "uncommitted" in blocked.blocked_reason
    assert not (env.repo / "w.txt").exists()
    assert (env.repo / "a.txt").read_text() == "my wip\n"  # the user's work is untouched
    assert sqlite3.connect(env.tracker.db_path).execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0


# ------------------------------------------------- failure = evidence, not undo

def test_verification_failure_preserves_everything_and_blocks(env):
    plan = make_plan(env, [
        write("a.txt", "changed\n", verify={"type": "fail"}),
        write("later.txt", "never\n"),
    ])
    blocked = make_engine(env).run(plan.plan_id)

    assert blocked.status is PlanStatus.BLOCKED and blocked.blocked_reason.startswith("VERIFICATION")
    # evidence preserved: the change the failed step made is still on disk
    assert (env.repo / "a.txt").read_text() == "changed\n"
    assert not (env.repo / "later.txt").exists()
    # no stash, no extra branches, no new commits
    assert git(env.repo, "stash", "list") == ""
    assert git(env.repo, "branch", "--format=%(refname:short)") == "main"
    assert git(env.repo, "rev-list", "--count", "HEAD") == "1"
    # state
    assert blocked.steps[0].status is StepStatus.FAILED and blocked.steps[1].status is StepStatus.PENDING
    assert blocked.current_step_index == 0
    assert get_task_row(env.tracker.db_path, env.task)["status"] == "BLOCKED"
    # diagnostics in SQLite, retrievable
    d = diag(env, env.store.load(plan.plan_id))
    assert d["kind"] == "VERIFICATION" and d["step_number"] == 1 and d["tool"] == "write_file"
    assert "expected X" in d["verifier_detail"] and "1 failed" in d["verifier_detail"]
    assert "+changed" in d["git_diff"] and "a.txt" in d["git_status"]
    assert d["checkpoint_id"] == blocked.checkpoint_id
    # the human is told what happened and what they can do
    shown = "\n".join(env.lines)
    assert "BLOCKED" in shown and "Nothing was rolled back or deleted" in shown
    assert f"jarvis rollback --checkpoint {blocked.checkpoint_id}" in shown
    assert ("verify:fail", "FAILURE") in rows(env) and ("engine:failure", "FAILURE") in rows(env)


def test_tool_exception_is_captured_with_traceback(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(FileSystemInspector, "write_file", staticmethod(boom))
    plan = make_plan(env, [write("x.txt", "x")])
    blocked = make_engine(env).run(plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED and blocked.blocked_reason.startswith("EXCEPTION")
    d = diag(env, blocked)
    assert "RuntimeError: disk on fire" in d["message"] and "Traceback" in d["traceback"]
    assert ("write_file", "FAILURE") in rows(env)


def test_verifier_crash_is_captured_separately_from_verification_failure(env):
    plan = make_plan(env, [write("v.txt", "v", verify={"type": "crash"})])
    blocked = make_engine(env).run(plan.plan_id)
    d = diag(env, blocked)
    assert d["kind"] == "VERIFIER_ERROR" and "verifier exploded" in d["traceback"]
    assert (env.repo / "v.txt").read_text() == "v"  # still preserved


def test_tool_error_string_is_a_failure(env):
    plan = make_plan(env, [{"tool": "read_file", "params": {"path": "missing.txt"}}])
    blocked = make_engine(env).run(plan.plan_id)
    d = diag(env, blocked)
    assert d["kind"] == "TOOL_ERROR" and "not a file" in d["message"]
    assert ("read_file", "FAILURE") in rows(env)


def test_diagnostic_params_are_truncated_but_the_file_is_not(env):
    big = "x" * 5000
    plan = make_plan(env, [write("big.txt", big, verify={"type": "fail"})])
    blocked = make_engine(env).run(plan.plan_id)
    preview = diag(env, blocked)["params"]["content"]
    assert len(preview) < 600 and "5000 chars" in preview
    assert (env.repo / "big.txt").read_text() == big


def test_file_content_that_mentions_policy_denial_is_not_misread_as_denial(env):
    (env.repo / "doc.md").write_text("ERROR-free doc that says: Access denied by policy is a message.\n")
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-m", "doc")
    plan = make_plan(env, [{"tool": "read_file", "params": {"path": "doc.md"}}])
    assert make_engine(env).run(plan.plan_id).status is PlanStatus.COMPLETED
    assert rows(env) == [("read_file", "SUCCESS")]


def test_classify():
    assert _classify("Wrote 3 chars to x") == "OK"
    assert _classify("ERROR: Access denied by policy. (declined by user)") == "DECLINED"
    assert _classify("ERROR: Access denied by policy. outside workspace") == "POLICY"
    assert _classify("ERROR: Nothing to commit") == "TOOL_ERROR"


# --------------------------------------------------- timeout and resumption

def test_timeout_blocks_before_the_next_step_and_resume_finishes(env):
    plan = make_plan(env, [
        write("t1.txt", "1\n", verify={"type": "slow"}),   # advances the fake clock by 100s
        write("t2.txt", "2\n", verify={"type": "ok"}),
    ], timeout_seconds=60)
    eng = make_engine(env)

    blocked = eng.run(plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED and blocked.blocked_reason.startswith("TIMEOUT")
    assert blocked.steps[0].status is StepStatus.SUCCEEDED
    assert blocked.steps[1].status is StepStatus.PENDING   # never started, so not 'failed'
    assert blocked.current_step_index == 1 and not (env.repo / "t2.txt").exists()

    done = eng.run(plan.plan_id)  # fresh budget for the new run
    assert done.status is PlanStatus.COMPLETED and (env.repo / "t2.txt").read_text() == "2\n"
    assert [r for r in rows(env) if r[0] == "write_file"].__len__() == 2  # t1 was NOT redone


def test_blocked_plan_resumes_at_the_failed_step_only(env):
    attempts = {"n": 0}

    def flaky(spec):
        def check(step, ctx):
            attempts["n"] += 1
            return VerificationResult(attempts["n"] > 1, f"attempt {attempts['n']}")
        return _V(check)

    plan = make_plan(env, [
        write("r1.txt", "1\n"),
        write("r2.txt", "2\n", verify={"type": "flaky"}),
        write("r3.txt", "3\n"),
    ])
    eng = make_engine(env, factories={**env.factories, "flaky": flaky})

    first = eng.run(plan.plan_id)
    assert first.status is PlanStatus.BLOCKED and first.current_step_index == 1
    checkpoint = first.checkpoint_id
    assert get_task_row(env.tracker.db_path, env.task)["status"] == "BLOCKED"

    second = eng.run(plan.plan_id)
    assert second.status is PlanStatus.COMPLETED
    assert second.checkpoint_id == checkpoint  # the pre-plan rollback point is kept, not replaced
    assert get_task_row(env.tracker.db_path, env.task)["status"] == "IN_PROGRESS"
    writes = [r for r in rows(env) if r[0] == "write_file"]
    assert len(writes) == 4  # r1, r2 (failed verify), r2 again, r3 -- r1 not repeated
    assert all(s.status is StepStatus.SUCCEEDED for s in second.steps)


def test_interrupted_running_plan_resumes_with_a_notice(env):
    plan = make_plan(env, [
        {"tool": "read_file", "params": {"path": "a.txt"}},
        {"tool": "list_directory"},
    ])
    plan.status, plan.current_step_index = PlanStatus.RUNNING, 1
    plan.steps[0].status, plan.steps[1].status = StepStatus.SUCCEEDED, StepStatus.RUNNING
    env.store.save(plan)

    done = make_engine(env).run(plan.plan_id)
    assert done.status is PlanStatus.COMPLETED
    assert any("interrupted" in l for l in env.lines)
    assert rows(env) == [("list_directory", "SUCCESS")]  # step 1 was not redone


# ------------------------------------------------------------- refusals

def test_engine_refuses_completed_plan_and_closed_task_and_unknown_plan(env):
    plan = make_plan(env, [{"tool": "list_directory"}])
    eng = make_engine(env)
    eng.run(plan.plan_id)
    with pytest.raises(EngineError, match="already COMPLETED"):
        eng.run(plan.plan_id)

    other = make_plan(env, [{"tool": "list_directory"}])
    env.tracker.update_task_status(env.task, "ABORTED")
    with pytest.raises(EngineError, match="ABORTED"):
        eng.run(other.plan_id)
    assert env.store.load(other.plan_id).status is PlanStatus.PENDING  # untouched

    with pytest.raises(PlanNotFound):
        eng.run("PLN-NOPE0000")


# ---------------------------------------------------- default verifier loading

def test_load_default_verifiers_absent_module_is_empty(monkeypatch):
    def fake_import(name, *a, **k):
        raise ModuleNotFoundError("No module named 'jarvis.core.verifier'", name="jarvis.core.verifier")
    monkeypatch.setattr(importlib, "import_module", fake_import)
    assert load_default_verifiers() == {}


def test_load_default_verifiers_real_import_errors_propagate(monkeypatch):
    def fake_import(name, *a, **k):
        raise ModuleNotFoundError("No module named 'somelib'", name="somelib")
    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(ModuleNotFoundError):
        load_default_verifiers()


def test_load_default_verifiers_reads_VERIFIERS(monkeypatch):
    fake = types.SimpleNamespace(VERIFIERS={"file_content": FileHasContent})
    monkeypatch.setattr(importlib, "import_module", lambda name, *a, **k: fake)
    assert load_default_verifiers() == {"file_content": FileHasContent}


def test_registry_is_exactly_the_agreed_tools():
    assert sorted(TOOL_REGISTRY) == ["git_commit", "list_directory", "read_file", "write_file"]


# ------------------------------------------- plan commit scope (ADR-0005 h)
# Regression: a plan's git_commit used `git add -A`, swept unrelated untracked
# files into the commit, and a later rollback then deleted them from disk.

def commit_step(message="engine commit"):
    return {"tool": "git_commit", "params": {"message": message}, "verify": {"type": "ok"}}


def test_plan_commit_includes_only_files_the_plan_wrote(env):
    (env.repo / "stray.txt").write_text("not part of the plan\n")      # untracked, pre-existing
    (env.repo / "a.txt").write_text("v1\n")                              # tracked, unchanged
    plan = make_plan(env, [write("c.txt", "committed\n"), commit_step()])

    assert make_engine(env).run(plan.plan_id).status is PlanStatus.COMPLETED

    assert git(env.repo, "show", "--name-only", "--format=", "HEAD").splitlines() == ["c.txt"]
    assert git(env.repo, "ls-files", "--others", "--exclude-standard") == "stray.txt"
    assert (env.repo / "stray.txt").read_text() == "not part of the plan\n"


def test_rollback_after_plan_commit_keeps_strays_and_names_what_it_deletes(env):
    from jarvis.core import checkpoint as cp

    (env.repo / "stray.txt").write_text("keep me\n")
    plan = make_plan(env, [write("c.txt", "committed\n"), commit_step()])
    done = make_engine(env).run(plan.plan_id)

    lines = []
    cp.rollback(env.tracker, done.checkpoint_id, auto_confirm=True, out=lines.append)
    text = "\n".join(lines)

    assert (env.repo / "stray.txt").read_text() == "keep me\n"          # the bug: this used to be deleted
    assert not (env.repo / "c.txt").exists()                             # plan-created, so undone
    assert "DELETED from disk" in text and "c.txt" in text
    deleted_block = text.split("DELETED from disk")[1].split("Changes to tracked")[0]
    assert "c.txt" in deleted_block and "stray.txt" not in deleted_block
    assert "Untracked files (NOT touched" in text and "stray.txt" in text.split("Untracked files (NOT touched")[1]


def test_plan_commit_treats_glob_characters_literally(env):
    (env.repo / "a1.txt").write_text("innocent\n")                       # would match 'a[1].txt' as a glob
    plan = make_plan(env, [write("a[1].txt", "literal\n"), commit_step()])

    assert make_engine(env).run(plan.plan_id).status is PlanStatus.COMPLETED
    assert git(env.repo, "show", "--name-only", "--format=", "HEAD").splitlines() == ["a[1].txt"]
    assert git(env.repo, "ls-files", "--others", "--exclude-standard") == "a1.txt"


def test_plan_commit_with_other_files_already_staged_still_commits_only_plan_files(env):
    # The engine's pre-flight refuses to start with staged changes, so this guards the
    # commit function itself (defence in depth), called directly.
    from jarvis.core.plan_commit import commit_plan_paths
    (env.repo / "staged.txt").write_text("staged by someone\n")
    git(env.repo, "add", "staged.txt")
    (env.repo / "c.txt").write_text("plan file\n")

    out = commit_plan_paths("proj", "only c", ["c.txt"], auto_confirm=True)

    assert not out.startswith("ERROR"), out
    assert git(env.repo, "show", "--name-only", "--format=", "HEAD").splitlines() == ["c.txt"]
    assert "staged.txt" in git(env.repo, "diff", "--cached", "--name-only")


def test_commit_step_needs_an_earlier_write_step(env):
    with pytest.raises(PlanFormatError, match="written by earlier write_file"):
        make_plan(env, [commit_step()])
    with pytest.raises(PlanFormatError, match="written by earlier write_file"):
        make_plan(env, [commit_step(), write("late.txt", "x\n")])


def test_a_plan_cannot_supply_its_own_commit_paths(env):
    step = {"tool": "git_commit", "params": {"message": "m", "_paths": ["a.txt"]}, "verify": {"type": "ok"}}
    with pytest.raises(PlanFormatError, match="unexpected param"):
        make_plan(env, [write("c.txt", "x\n"), step])


def test_commit_only_includes_writes_that_succeeded_before_it(env):
    from jarvis.core.engine import ExecutionEngine as E
    plan = ExecutionPlan.from_dict({"task_id": env.task, "steps": [
        write("one.txt", "1\n"), write("two.txt", "2\n"), write("one.txt", "again\n"), commit_step(),
    ]}, require_state=False)
    plan.steps[0].status = StepStatus.SUCCEEDED
    plan.steps[2].status = StepStatus.SUCCEEDED         # two.txt never succeeded; one.txt listed twice
    assert E._invoke_params(plan, 3, plan.steps[3])["_paths"] == ["one.txt"]
    assert "_paths" not in E._invoke_params(plan, 0, plan.steps[0])


@pytest.mark.parametrize("paths", [[], ["../x.txt"], ["/etc/passwd"], ["a/../../x"]])
def test_commit_plan_paths_refuses_empty_and_unsafe_paths(env, paths):
    from jarvis.core.plan_commit import commit_plan_paths
    assert commit_plan_paths("proj", "m", paths, auto_confirm=True).startswith("ERROR")


def test_declined_plan_commit_changes_nothing(env):
    from jarvis.core.plan_commit import commit_plan_paths
    (env.repo / "c.txt").write_text("x\n")
    before = git(env.repo, "rev-parse", "HEAD")
    out = commit_plan_paths("proj", "m", ["c.txt"], auto_confirm=False)
    assert "declined by user" in out
    assert git(env.repo, "rev-parse", "HEAD") == before
    assert git(env.repo, "diff", "--cached", "--name-only") == ""
