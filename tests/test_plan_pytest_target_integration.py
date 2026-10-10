"""
End-to-end integration tests for the `pytest_target` verifier inside real
`ExecutionEngine` plan runs (M002, ADR-0005).

Every test builds a temporary git repository with one initial commit, creates a
real `ExecutionPlan` validated via `validate_plan`, persists it in `PlanStore`,
and executes it through `ExecutionEngine` using the real auto-discovered
`VERIFIERS` from `jarvis.core.verifier`.
"""

import ctypes
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.core.checkpoint import get_checkpoint
from jarvis.core.engine import ExecutionEngine, validate_plan
from jarvis.core.plan import ExecutionPlan, PlanStatus, StepStatus
from jarvis.state.plan_store import PlanStore
from jarvis.state.tracker import StateTracker


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform check for whether a process with `pid` is still running."""
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok and exit_code.value == still_active)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    """
    Sets up a workspace with a registered git repository (`proj`) containing
    one initial commit, plus an `outside` directory outside the project root
    for containment checks.
    """
    workspace = (tmp_path / "workspace").resolve()
    outside = (tmp_path / "outside").resolve()
    workspace.mkdir(parents=True)
    outside.mkdir(parents=True)

    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))

    repo = workspace / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    initial_head = _git(repo, "rev-parse", "HEAD")

    tracker = StateTracker(db_path=tmp_path / "state.db")
    tracker.register_project("proj", "proj")
    session_id = tracker.create_session("m002 pytest_target integration")
    task_id = tracker.create_task(session_id, "verify pytest_target in real plan", "proj")
    store = PlanStore(tracker.db_path)
    output_lines: list[str] = []

    return SimpleNamespace(
        workspace=workspace,
        outside=outside,
        repo=repo,
        initial_head=initial_head,
        tracker=tracker,
        store=store,
        task_id=task_id,
        output_lines=output_lines,
    )


def _create_plan(env, steps: list[dict], **limits) -> ExecutionPlan:
    plan_data = {"task_id": env.task_id, "steps": steps, **limits}
    plan = ExecutionPlan.from_dict(plan_data, require_state=False)
    validate_plan(plan)
    return env.store.create(plan)


def _run_plan(env, plan: ExecutionPlan) -> ExecutionPlan:
    # Omit verifier_factories so ExecutionEngine loads real VERIFIERS from jarvis.core.verifier
    engine = ExecutionEngine(
        env.tracker,
        env.store,
        auto_confirm=True,
        out=env.output_lines.append,
    )
    return engine.run(plan.plan_id)


def _execution_rows(env) -> list[tuple[str, str]]:
    with sqlite3.connect(env.tracker.db_path) as conn:
        return conn.execute(
            "SELECT tool_name, status FROM executions WHERE task_id = ? ORDER BY rowid",
            (env.task_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# 1. Happy path: write_file writes a test file, pytest_target verifies it,
#    and the plan reaches COMPLETED.
# ---------------------------------------------------------------------------

def test_write_file_verified_by_pytest_target_completes_plan(plan_env):
    test_code = (
        "from calc import add\n\n"
        "def test_add_numbers():\n"
        "    assert add(3, 4) == 7\n"
    )
    plan = _create_plan(
        plan_env,
        [
            {
                "tool": "write_file",
                "description": "Write unit test for calc.add",
                "params": {"path": "test_calc.py", "content": test_code},
                "verify": {
                    "type": "pytest_target",
                    "target": "test_calc.py",
                    "args": ["-q"],
                },
            }
        ],
    )

    result = _run_plan(plan_env, plan)

    assert result.status is PlanStatus.COMPLETED
    assert result.current_step_index == 1
    assert result.blocked_reason == ""
    assert result.checkpoint_id is not None
    assert result.steps[0].status is StepStatus.SUCCEEDED
    assert (plan_env.repo / "test_calc.py").read_text(encoding="utf-8") == test_code
    assert not (plan_env.repo / ".pytest_cache").exists()
    assert _execution_rows(plan_env) == [
        ("write_file", "SUCCESS"),
        ("verify:pytest_target", "SUCCESS"),
    ]


# ---------------------------------------------------------------------------
# 2. Failing target: makes the plan BLOCKED, saves diagnostics, and takes a
#    checkpoint.
# ---------------------------------------------------------------------------

def test_failing_pytest_target_blocks_plan_saves_diagnostics_and_takes_checkpoint(plan_env):
    failing_test_code = (
        "from calc import add\n\n"
        "def test_add_intentional_failure():\n"
        "    assert add(2, 2) == 5, 'CALC_REGRESSION_DETECTED'\n"
    )
    plan = _create_plan(
        plan_env,
        [
            {
                "tool": "write_file",
                "description": "Write failing test",
                "params": {"path": "test_fail.py", "content": failing_test_code},
                "verify": {
                    "type": "pytest_target",
                    "target": "test_fail.py",
                },
            }
        ],
    )

    result = _run_plan(plan_env, plan)

    # Plan is BLOCKED and step is FAILED
    assert result.status is PlanStatus.BLOCKED
    assert result.steps[0].status is StepStatus.FAILED
    assert "VERIFICATION" in result.blocked_reason
    assert "pytest_target" in result.blocked_reason

    # Checkpoint was taken before the step mutated the project
    assert result.checkpoint_id is not None
    cp = get_checkpoint(plan_env.tracker.db_path, result.checkpoint_id)
    assert cp is not None
    assert cp["git_commit_hash"] == plan_env.initial_head
    assert cp["git_branch"] == "main"

    # Evidence is preserved on disk and in stored diagnostics
    assert (plan_env.repo / "test_fail.py").read_text(encoding="utf-8") == failing_test_code
    diag = plan_env.store.get_failure_diagnostics(plan.plan_id, plan_env.task_id)
    assert diag is not None
    assert diag["kind"] == "VERIFICATION"
    assert diag["step_index"] == 0
    assert diag["tool"] == "write_file"
    assert diag["checkpoint_id"] == result.checkpoint_id
    assert "CALC_REGRESSION_DETECTED" in diag["verifier_detail"]
    assert "pytest failed with exit code" in diag["verifier_detail"]

    assert _execution_rows(plan_env) == [
        ("write_file", "SUCCESS"),
        ("verify:pytest_target", "FAILURE"),
        ("engine:failure", "FAILURE"),
    ]


# ---------------------------------------------------------------------------
# 3. Rejected args: --basetemp, --junitxml and an absolute path in args are
#    rejected, and nothing is created or deleted outside the project.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "arg_builder",
    [
        lambda outside: ["--basetemp", str((outside / "basetemp_dir").resolve())],
        lambda outside: [f"--basetemp={(outside / 'basetemp_eq').resolve()}"],
        lambda outside: ["--junitxml", str((outside / "junit_report.xml").resolve())],
        lambda outside: [f"--junitxml={(outside / 'junit_eq.xml').resolve()}"],
        lambda outside: [str((outside / "abs_target.py").resolve())],
        lambda outside: ["-k", str((outside / "abs_option_val").resolve())],
    ],
    ids=[
        "basetemp-separate",
        "basetemp-equals",
        "junitxml-separate",
        "junitxml-equals",
        "abs-path-positional",
        "abs-path-option-value",
    ],
)
def test_unsafe_pytest_args_rejected_and_outside_directory_untouched(plan_env, arg_builder):
    canary = plan_env.outside / "canary.txt"
    canary.write_text("do not delete or modify\n", encoding="utf-8")
    before_snapshot = {
        p.relative_to(plan_env.outside): p.read_bytes()
        for p in plan_env.outside.rglob("*")
        if p.is_file()
    }
    before_dirs = {
        p.relative_to(plan_env.outside)
        for p in plan_env.outside.rglob("*")
        if p.is_dir()
    }

    bad_args = arg_builder(plan_env.outside)
    plan = _create_plan(
        plan_env,
        [
            {
                "tool": "write_file",
                "params": {
                    "path": "test_should_not_run.py",
                    "content": "def test_ok():\n    assert True\n",
                },
                "verify": {
                    "type": "pytest_target",
                    "target": "test_should_not_run.py",
                    "args": bad_args,
                },
            }
        ],
    )

    result = _run_plan(plan_env, plan)

    # Rejected in pre-flight before any step or subprocess runs
    assert result.status is PlanStatus.BLOCKED
    assert result.blocked_reason.startswith("PREFLIGHT:")
    assert "PermissionError" in result.blocked_reason
    assert result.steps[0].status is StepStatus.PENDING
    assert not (plan_env.repo / "test_should_not_run.py").exists()

    # Nothing created, modified, or deleted outside the project
    after_snapshot = {
        p.relative_to(plan_env.outside): p.read_bytes()
        for p in plan_env.outside.rglob("*")
        if p.is_file()
    }
    after_dirs = {
        p.relative_to(plan_env.outside)
        for p in plan_env.outside.rglob("*")
        if p.is_dir()
    }
    assert after_snapshot == before_snapshot
    assert after_dirs == before_dirs
    assert canary.read_text(encoding="utf-8") == "do not delete or modify\n"


# ---------------------------------------------------------------------------
# 4. Timeout: a hung target is killed at the timeout.
# ---------------------------------------------------------------------------

def test_hung_pytest_target_is_killed_at_timeout(plan_env):
    hung_test_code = (
        "import os\n"
        "import time\n\n"
        "def test_hang_forever():\n"
        "    print(f'HUNG_PID={os.getpid()}', flush=True)\n"
        "    time.sleep(60)\n"
    )
    plan = _create_plan(
        plan_env,
        [
            {
                "tool": "write_file",
                "description": "Write a test that hangs",
                "params": {"path": "test_hang.py", "content": hung_test_code},
                "verify": {
                    "type": "pytest_target",
                    "target": "test_hang.py",
                    "args": ["-s"],
                },
            }
        ],
        timeout_seconds=2,
    )

    start = time.monotonic()
    result = _run_plan(plan_env, plan)
    elapsed = time.monotonic() - start

    # Must terminate promptly around the 2s plan timeout budget, not wait 60s
    assert elapsed < 15.0
    assert result.status is PlanStatus.BLOCKED
    assert result.steps[0].status is StepStatus.FAILED
    assert result.checkpoint_id is not None

    diag = plan_env.store.get_failure_diagnostics(plan.plan_id, plan_env.task_id)
    assert diag is not None
    assert diag["kind"] == "VERIFICATION"
    assert "pytest timed out after" in diag["verifier_detail"]

    match = re.search(r"HUNG_PID=(\d+)", diag["verifier_detail"])
    assert match is not None, f"Expected HUNG_PID in verifier_detail, got: {diag['verifier_detail']}"
    hung_pid = int(match.group(1))
    assert not _pid_is_alive(hung_pid), f"Hung pytest subprocess (PID {hung_pid}) was not killed!"
