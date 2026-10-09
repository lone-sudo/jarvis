import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis.core.engine import ExecutionEngine, PlanStore
from jarvis.core.plan import (
    ExecutionPlan,
    ExecutionStep,
    PlanStatus,
    StepStatus,
    VerificationContext,
    VerificationResult,
)
from jarvis.core.verifier import (
    VERIFIERS,
    FileContentVerifier,
    GitCommitVerifier,
    PytestTargetVerifier,
)
from jarvis.policy.rules import SecurityPolicy
from jarvis.state.tracker import StateTracker


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.name=tester", "-c", "user.email=tester@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(ws))

    proj = ws / "my_project"
    proj.mkdir()
    git(proj, "init", "-b", "main")
    (proj / "readme.txt").write_text("initial readme\n", encoding="utf-8")
    git(proj, "add", "-A")
    git(proj, "commit", "-m", "initial commit")

    tracker = StateTracker(db_path=tmp_path / "state.db")
    tracker.register_project("my_project", "my_project")
    session_id = tracker.create_session("verifier test session")
    task_id = tracker.create_task(session_id, "test task", "my_project")
    store = PlanStore(tracker.db_path)

    ctx = VerificationContext(
        project_root=proj,
        workspace_root=ws,
        time_remaining=60.0,
    )
    return {
        "ws": ws,
        "proj": proj,
        "tracker": tracker,
        "session_id": session_id,
        "task_id": task_id,
        "store": store,
        "ctx": ctx,
    }


# ==============================================================================
# 1. FileContentVerifier Tests
# ==============================================================================

def test_file_content_verifier_happy_path(test_env):
    proj = test_env["proj"]
    ctx = test_env["ctx"]

    (proj / "output.txt").write_text("Hello Jarvis\nLine 2", encoding="utf-8")

    # Exact content match
    v1 = FileContentVerifier({"type": "file_content", "path": "output.txt", "content": "Hello Jarvis\nLine 2"})
    res1 = v1.verify(ExecutionStep(tool="write_file"), ctx)
    assert res1.ok is True
    assert "verified" in res1.detail

    # Substring contains match
    v2 = FileContentVerifier({"type": "file_content", "path": "output.txt", "contains": ["Hello", "Line 2"]})
    res2 = v2.verify(ExecutionStep(tool="write_file"), ctx)
    assert res2.ok is True

    # Not empty check
    v3 = FileContentVerifier({"type": "file_content", "path": "output.txt", "not_empty": True})
    res3 = v3.verify(ExecutionStep(tool="write_file"), ctx)
    assert res3.ok is True

    # Default to step params content
    step_with_params = ExecutionStep(tool="write_file", params={"path": "output.txt", "content": "Hello Jarvis\nLine 2"})
    v4 = FileContentVerifier({"type": "file_content"})
    res4 = v4.verify(step_with_params, ctx)
    assert res4.ok is True


def test_file_content_verifier_failures_and_diagnostics(test_env):
    proj = test_env["proj"]
    ctx = test_env["ctx"]

    # File missing
    v1 = FileContentVerifier({"type": "file_content", "path": "nonexistent.txt"})
    res1 = v1.verify(ExecutionStep(tool="write_file"), ctx)
    assert res1.ok is False
    assert "File does not exist: nonexistent.txt" in res1.detail

    # Content mismatch preserves diagnostics
    (proj / "wrong.txt").write_text("actual text", encoding="utf-8")
    v2 = FileContentVerifier({"type": "file_content", "path": "wrong.txt", "content": "expected text"})
    res2 = v2.verify(ExecutionStep(tool="write_file"), ctx)
    assert res2.ok is False
    assert "Content mismatch in 'wrong.txt'" in res2.detail
    assert "expected 13 chars, got 11 chars" in res2.detail

    # Substring missing preserves diagnostic
    v3 = FileContentVerifier({"type": "file_content", "path": "wrong.txt", "contains": "missing_keyword"})
    res3 = v3.verify(ExecutionStep(tool="write_file"), ctx)
    assert res3.ok is False
    assert "missing expected substring" in res3.detail
    assert "missing_keyword" in res3.detail

    # Empty file failure
    (proj / "empty.txt").write_text("   \n", encoding="utf-8")
    v4 = FileContentVerifier({"type": "file_content", "path": "empty.txt", "not_empty": True})
    res4 = v4.verify(ExecutionStep(tool="write_file"), ctx)
    assert res4.ok is False
    assert "File 'empty.txt' is empty" in res4.detail


def test_file_content_verifier_policy_breach(test_env, tmp_path):
    ctx = test_env["ctx"]

    # Traversal in spec path
    with pytest.raises(PermissionError, match="attempts path traversal or is absolute"):
        FileContentVerifier({"type": "file_content", "path": "../outside.txt"})

    # Absolute path in spec path (platform-independent)
    abs_outside = str((tmp_path / "outside_calc.txt").resolve())
    with pytest.raises(PermissionError, match="attempts path traversal or is absolute"):
        FileContentVerifier({"type": "file_content", "path": abs_outside})

    # Traversal via step params at verify time
    v = FileContentVerifier({"type": "file_content"})
    malicious_step = ExecutionStep(tool="write_file", params={"path": "../../escape.txt"})
    with pytest.raises(PermissionError, match="file_content policy breach"):
        v.verify(malicious_step, ctx)


def test_file_content_verifier_spec_validation():
    with pytest.raises(ValueError, match="spec must be a dictionary"):
        FileContentVerifier("not-a-dict")  # type: ignore

    with pytest.raises(ValueError, match="unexpected spec key"):
        FileContentVerifier({"type": "file_content", "invalid_key": 123})

    with pytest.raises(ValueError, match="'path' must be a non-empty string"):
        FileContentVerifier({"type": "file_content", "path": ""})


# ==============================================================================
# 2. PytestTargetVerifier Tests
# ==============================================================================

def test_pytest_target_verifier_happy_path(test_env):
    proj = test_env["proj"]
    ctx = test_env["ctx"]

    # Create passing test
    tests_dir = proj / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ok.py").write_text(
        "def test_passing():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    v = PytestTargetVerifier({"type": "pytest_target", "target": "tests/test_ok.py"})
    res = v.verify(ExecutionStep(tool="write_file"), ctx)
    assert res.ok is True
    assert "passed" in res.detail.lower()


def test_pytest_target_verifier_failure_diagnostics(test_env):
    proj = test_env["proj"]
    ctx = test_env["ctx"]

    # Create failing test
    tests_dir = proj / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_fail.py").write_text(
        "def test_broken():\n    expected = 'alpha'\n    actual = 'beta'\n    assert expected == actual\n",
        encoding="utf-8",
    )

    v = PytestTargetVerifier({"type": "pytest_target", "target": "tests/test_fail.py"})
    res = v.verify(ExecutionStep(tool="write_file"), ctx)
    assert res.ok is False
    # CRITICAL: Evidence preservation
    assert "pytest failed with exit code 1" in res.detail
    assert "assert expected == actual" in res.detail
    assert "AssertionError" in res.detail
    assert "FAILED tests/test_fail.py::test_broken" in res.detail


def test_pytest_target_verifier_timeout(test_env):
    proj = test_env["proj"]

    tests_dir = proj / "tests"
    tests_dir.mkdir(exist_ok=True)
    # Slow test
    (tests_dir / "test_slow.py").write_text(
        "import time\ndef test_slow():\n    time.sleep(2.0)\n",
        encoding="utf-8",
    )

    # 1. Spec timeout exceeded
    v_slow = PytestTargetVerifier({"type": "pytest_target", "target": "tests/test_slow.py", "timeout": 0.5})
    ctx_ample = VerificationContext(
        project_root=proj,
        workspace_root=test_env["ws"],
        time_remaining=30.0,
    )
    res_timeout = v_slow.verify(ExecutionStep(tool="write_file"), ctx_ample)
    assert res_timeout.ok is False
    assert "timed out after 0.5s" in res_timeout.detail

    # 2. Plan remaining time budget exhausted
    ctx_exhausted = VerificationContext(
        project_root=proj,
        workspace_root=test_env["ws"],
        time_remaining=0.0,
    )
    v_normal = PytestTargetVerifier({"type": "pytest_target", "target": "tests/test_slow.py"})
    res_exhausted = v_normal.verify(ExecutionStep(tool="write_file"), ctx_exhausted)
    assert res_exhausted.ok is False
    assert "Timeout budget exhausted" in res_exhausted.detail


def test_pytest_target_verifier_policy_breach(test_env, tmp_path):
    ctx = test_env["ctx"]

    # Traversal in target spec
    with pytest.raises(PermissionError, match="attempts path traversal or is absolute"):
        PytestTargetVerifier({"type": "pytest_target", "target": "../../tests"})

    # Absolute path in target spec (platform-independent)
    abs_target = str((tmp_path / "outside_tests").resolve())
    with pytest.raises(PermissionError, match="attempts path traversal or is absolute"):
        PytestTargetVerifier({"type": "pytest_target", "target": abs_target})


@pytest.mark.parametrize("bad_arg", [
    "--basetemp",
    "--junitxml",
    "--cache-dir",
    "-o",
    "-p",
    "-c",
    "--rootdir",
    "--confcutdir",
    "--import-mode",
])
def test_pytest_target_verifier_rejected_args(bad_arg):
    with pytest.raises(PermissionError, match="forbidden by policy"):
        PytestTargetVerifier({"type": "pytest_target", "args": [bad_arg]})

    with pytest.raises(PermissionError, match="forbidden by policy"):
        PytestTargetVerifier({"type": "pytest_target", "args": [f"{bad_arg}=some_val"]})


def test_pytest_target_verifier_basetemp_outside_project_refused(tmp_path):
    outside_dir = str((tmp_path / "outside_basetemp").resolve())
    # Separate arg
    with pytest.raises(PermissionError, match="forbidden by policy|absolute path"):
        PytestTargetVerifier({"type": "pytest_target", "args": ["--basetemp", outside_dir]})

    # Equals arg
    with pytest.raises(PermissionError, match="forbidden by policy|absolute path"):
        PytestTargetVerifier({"type": "pytest_target", "args": [f"--basetemp={outside_dir}"]})


def test_pytest_target_verifier_absolute_path_arg_refused(tmp_path):
    abs_arg = str((tmp_path / "somedir").resolve())
    with pytest.raises(PermissionError, match="absolute path"):
        PytestTargetVerifier({"type": "pytest_target", "args": [abs_arg]})

    with pytest.raises(PermissionError, match="absolute path"):
        PytestTargetVerifier({"type": "pytest_target", "args": ["-k", abs_arg]})


# ==============================================================================
# 3. GitCommitVerifier Tests
# ==============================================================================

def test_git_commit_verifier_happy_path(test_env):
    proj = test_env["proj"]
    ctx = test_env["ctx"]

    (proj / "new_file.txt").write_text("hello\n", encoding="utf-8")
    git(proj, "add", "-A")
    git(proj, "commit", "-m", "feat: add new file")

    # Match exact message
    v1 = GitCommitVerifier({"type": "git_commit", "message": "feat: add new file"})
    res1 = v1.verify(ExecutionStep(tool="git_commit"), ctx)
    assert res1.ok is True
    assert "Commit verified" in res1.detail
    assert "feat: add new file" in res1.detail

    # Match substring
    v2 = GitCommitVerifier({"type": "git_commit", "message_contains": "add new"})
    res2 = v2.verify(ExecutionStep(tool="git_commit"), ctx)
    assert res2.ok is True

    # Match step params message
    step = ExecutionStep(tool="git_commit", params={"message": "feat: add new file"})
    v3 = GitCommitVerifier({"type": "git_commit"})
    res3 = v3.verify(step, ctx)
    assert res3.ok is True

    # Expect clean working directory
    v4 = GitCommitVerifier({"type": "git_commit", "expect_clean": True})
    res4 = v4.verify(step, ctx)
    assert res4.ok is True


def test_git_commit_verifier_failure_diagnostics(test_env):
    proj = test_env["proj"]
    ctx = test_env["ctx"]

    (proj / "f.txt").write_text("f\n", encoding="utf-8")
    git(proj, "add", "-A")
    git(proj, "commit", "-m", "fix: small fix")

    # Message mismatch
    v1 = GitCommitVerifier({"type": "git_commit", "message": "feat: something else"})
    res1 = v1.verify(ExecutionStep(tool="git_commit"), ctx)
    assert res1.ok is False
    assert "Commit message mismatch" in res1.detail
    assert "expected 'feat: something else', got 'fix: small fix'" in res1.detail

    # Working tree dirty when expect_clean is True
    (proj / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    git(proj, "add", "dirty.txt")  # staged but not committed
    v2 = GitCommitVerifier({"type": "git_commit", "message": "fix: small fix", "expect_clean": True})
    res2 = v2.verify(ExecutionStep(tool="git_commit"), ctx)
    assert res2.ok is False
    assert "working directory is dirty" in res2.detail

    # Git status returning ERROR with expect_clean must fail, not pass
    from unittest.mock import patch
    def fake_git(args, cwd):
        if "status" in args:
            return "ERROR: git status timed out."
        return "abc12345\x00fix: small fix"

    with patch("jarvis.tools.git.GitInspector._run", side_effect=fake_git):
        v3 = GitCommitVerifier({"type": "git_commit", "expect_clean": True})
        res3 = v3.verify(ExecutionStep(tool="git_commit"), ctx)
        assert res3.ok is False
        assert "Git status failed: ERROR: git status timed out." in res3.detail


def test_git_commit_verifier_non_git_repo(tmp_path):
    plain_dir = tmp_path / "not_git"
    plain_dir.mkdir()
    ctx = VerificationContext(project_root=plain_dir, workspace_root=tmp_path, time_remaining=10.0)

    v = GitCommitVerifier({"type": "git_commit"})
    res = v.verify(ExecutionStep(tool="git_commit"), ctx)
    assert res.ok is False
    assert "Not a git repository" in res.detail


# ==============================================================================
# 4. Engine End-to-End Integration with Real Verifiers
# ==============================================================================

def test_engine_executes_plan_with_real_verifiers(test_env, monkeypatch):
    proj = test_env["proj"]
    tracker = test_env["tracker"]
    store = test_env["store"]
    task_id = test_env["task_id"]

    # Ensure engine auto-discovers our real verifiers
    eng = ExecutionEngine(tracker, store, auto_confirm=True)
    assert "file_content" in eng.verifier_factories
    assert "pytest_target" in eng.verifier_factories
    assert "git_commit" in eng.verifier_factories

    # Plan with write_file verified by file_content, then git_commit verified by git_commit
    plan = ExecutionPlan(
        task_id=task_id,
        steps=[
            ExecutionStep(
                tool="write_file",
                params={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                verify={"type": "file_content", "path": "calc.py", "contains": "return a + b"},
                description="Write calculator function",
            ),
            ExecutionStep(
                tool="git_commit",
                params={"message": "feat: add calculator function"},
                verify={"type": "git_commit", "message": "feat: add calculator function", "expect_clean": True},
                description="Commit calculator function",
            ),
        ],
    )
    store.create(plan)

    finished = eng.run(plan.plan_id)
    assert finished.status == PlanStatus.COMPLETED
    assert finished.current_step_index == 2
    assert (proj / "calc.py").is_file()
    assert "return a + b" in (proj / "calc.py").read_text()


def test_engine_preserves_diagnostics_on_verifier_failure(test_env):
    proj = test_env["proj"]
    tracker = test_env["tracker"]
    store = test_env["store"]
    task_id = test_env["task_id"]

    eng = ExecutionEngine(tracker, store, auto_confirm=True)

    # Step writes code, but pytest verifier fails
    tests_dir = proj / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_math.py").write_text("def test_check():\n    assert False, 'MATH REGRESSION'\n")

    plan = ExecutionPlan(
        task_id=task_id,
        steps=[
            ExecutionStep(
                tool="write_file",
                params={"path": "feature.txt", "content": "some content\n"},
                verify={"type": "pytest_target", "target": "tests/test_math.py"},
                description="Write feature that breaks tests",
            ),
        ],
    )
    store.create(plan)

    blocked = eng.run(plan.plan_id)
    assert blocked.status == PlanStatus.BLOCKED
    assert "step 1 ran, but its postcondition ('pytest_target') was not met" in blocked.blocked_reason

    # Diagnostic preservation:
    # 1. File remains on disk (evidence preserved)
    assert (proj / "feature.txt").exists()

    # 2. SQLite records failure execution and BLOCKED task status
    from jarvis.state.plan_store import get_task_row
    task_row = get_task_row(tracker.db_path, task_id)
    assert task_row is not None
    assert task_row["status"] == "BLOCKED"

    from jarvis.state.database import get_connection
    with get_connection(tracker.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE tool_name = 'verify:pytest_target' ORDER BY executed_at DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["status"] == "FAILURE"
        assert "MATH REGRESSION" in row["output_payload"]


# ==============================================================================
# 5. Mutation Tests (Verifying Quality & Negative Invariants)
# ==============================================================================

def test_mutation_altered_file_is_detected(test_env):
    """Mutation: If disk contents diverge after writing, verifier must fail."""
    proj = test_env["proj"]
    ctx = test_env["ctx"]

    (proj / "doc.txt").write_text("original text", encoding="utf-8")
    v = FileContentVerifier({"type": "file_content", "path": "doc.txt", "content": "original text"})
    assert v.verify(ExecutionStep(tool="write_file"), ctx).ok is True

    # Mutate disk
    (proj / "doc.txt").write_text("tampered text", encoding="utf-8")
    assert v.verify(ExecutionStep(tool="write_file"), ctx).ok is False


def test_mutation_verifier_does_not_mutate_project(test_env):
    """Invariant: Verifiers must be strictly read-only."""
    proj = test_env["proj"]
    ctx = test_env["ctx"]

    (proj / "immut.txt").write_text("constant", encoding="utf-8")
    mtime_before = (proj / "immut.txt").stat().st_mtime_ns

    v = FileContentVerifier({"type": "file_content", "path": "immut.txt", "content": "constant"})
    v.verify(ExecutionStep(tool="write_file"), ctx)

    mtime_after = (proj / "immut.txt").stat().st_mtime_ns
    assert mtime_before == mtime_after
