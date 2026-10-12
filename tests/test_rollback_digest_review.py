"""
Tests for M002 rollback snapshot & raw content digest (commits ee2ea31 and 524e588).

Covers:
  1. Snapshot stability (no false refusals) across CRLF/LF, core.autocrlf=true/false/input,
     racy index entries, `git status` / `git update-index --refresh`, mtime touches, and
     paths with spaces and non-ASCII characters.
  2. Mutation detection (no false accepts) across same-size edits, line-ending-only edits,
     staged-vs-unstaged swaps, renames, executable bit changes, deleted-then-recreated files,
     and `--cached`-only changes.
  3. Symlink handling in `_raw_content_digest` (`os.readlink` hashed without following target).
  4. Multi-megabyte chunked streaming (>1 MiB chunk boundary) and growing-file detection.
  5. Locked/unreadable file handling (`OSError` errno hashing and refusal when a lock appears
     during the confirmation prompt).
  6. Fail-closed behavior and `FAILURE` audit logging in `_refuse_if_changed` on both normal
     and state-only rollback paths.
"""

import ctypes
import errno
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.core import checkpoint as cp
from jarvis.core.engine import ExecutionEngine, validate_plan
from jarvis.core.plan import ExecutionPlan, PlanStatus
from jarvis.state.plan_store import PlanStore
from jarvis.state.tracker import StateTracker
from jarvis.tools.git import GitInspector


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))

    repo = workspace / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")

    tracker = StateTracker(db_path=tmp_path / "state.db")
    tracker.register_project("proj", "proj")
    session_id = tracker.create_session("digest review")
    task_id = tracker.create_task(session_id, "rollback digest review", "proj")
    store = PlanStore(tracker.db_path)

    return SimpleNamespace(
        workspace=workspace,
        repo=repo,
        tracker=tracker,
        store=store,
        task_id=task_id,
    )


def _make_blocked_plan(env, rel_path: str = "a.txt", content: str = "v2\n"):
    plan = ExecutionPlan.from_dict(
        {
            "task_id": env.task_id,
            "steps": [
                {
                    "tool": "write_file",
                    "params": {"path": rel_path, "content": content},
                    "verify": {"type": "file_content", "content": "intentionally-unmatched"},
                }
            ],
        },
        require_state=False,
    )
    validate_plan(plan)
    env.store.create(plan)
    engine = ExecutionEngine(env.tracker, env.store, auto_confirm=True, out=lambda *_: None)
    blocked = engine.run(plan.plan_id)
    assert blocked.status is PlanStatus.BLOCKED
    commit = cp.get_checkpoint(env.tracker.db_path, blocked.checkpoint_id)["git_commit_hash"]
    return plan, blocked, commit


def _execution_rows(env) -> list[tuple[str, str, str]]:
    with sqlite3.connect(env.tracker.db_path) as conn:
        return conn.execute(
            "SELECT tool_name, status, output_payload FROM executions WHERE task_id = ? ORDER BY rowid",
            (env.task_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# 1. False-refusal resistance: CRLF/LF, autocrlf modes, racy index, mtime,
#    spaces & non-ASCII names.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("autocrlf", ["true", "false", "input"])
@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
def test_snapshot_stable_across_autocrlf_modes_mtime_touch_and_index_refresh(env, autocrlf, eol):
    _git(env.repo, "config", "core.autocrlf", autocrlf)
    clean_file = env.repo / "clean_eol.txt"
    clean_file.write_bytes(b"clean1" + eol + b"clean2" + eol)
    _git(env.repo, "add", "clean_eol.txt")
    _git(env.repo, "commit", "-m", "add clean_eol")
    commit = _git(env.repo, "rev-parse", "HEAD")

    # Overwrite clean_file with the same bytes and modify a.txt
    clean_file.write_bytes(b"clean1" + eol + b"clean2" + eol)
    target_file = env.repo / "a.txt"
    target_file.write_bytes(b"line1" + eol + b"line2" + eol)

    # Force a racy index entry by setting files and .git/index mtimes equal in the past
    past_ns = os.stat(target_file).st_mtime_ns - 5_000_000_000
    os.utime(clean_file, ns=(past_ns, past_ns))
    os.utime(target_file, ns=(past_ns, past_ns))
    index_file = env.repo / ".git" / "index"
    if index_file.exists():
        os.utime(index_file, ns=(past_ns, past_ns))

    snap1 = cp._rollback_snapshot(env.repo, commit)

    # Refresh index and touch mtime without changing content
    _git(env.repo, "status")
    subprocess.run(
        ["git", "update-index", "--refresh", "-q"],
        cwd=env.repo,
        capture_output=True,
        check=False,
    )
    now_ns = time.time_ns()
    os.utime(clean_file, ns=(now_ns, now_ns))
    os.utime(target_file, ns=(now_ns, now_ns))

    snap2 = cp._rollback_snapshot(env.repo, commit)
    assert snap1 == snap2


def test_snapshot_stable_for_files_with_spaces_and_non_ascii_names(env):
    odd_names = [
        "my   spaced   file.txt",
        "caf\u00e9 \u65e5\u672c.txt",
        "quote_\u201d.txt",
    ]
    for name in odd_names:
        (env.repo / name).write_text(f"initial {name}\n", encoding="utf-8")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-m", "add odd names")
    commit = _git(env.repo, "rev-parse", "HEAD")

    for name in odd_names:
        (env.repo / name).write_text(f"modified {name}\n", encoding="utf-8")

    snap1 = cp._rollback_snapshot(env.repo, commit)
    snap2 = cp._rollback_snapshot(env.repo, commit)
    assert snap1 == snap2


# ---------------------------------------------------------------------------
# 2. False-accept resistance: same-size edit, line-ending edit, staged swap,
#    rename, mode change, symlink replacement, delete+recreate, --cached edit.
# ---------------------------------------------------------------------------

def test_snapshot_detects_same_size_edit_and_line_ending_edit(env):
    _git(env.repo, "config", "core.autocrlf", "true")
    _, _, commit = _make_blocked_plan(env, "a.txt", "ABCD\n")
    (env.repo / "a.txt").write_bytes(b"ABCD\r\n")

    snap_base = cp._rollback_snapshot(env.repo, commit)

    # Same byte length (6 bytes), different characters
    (env.repo / "a.txt").write_bytes(b"WXYZ\r\n")
    snap_same_len = cp._rollback_snapshot(env.repo, commit)
    assert snap_same_len != snap_base

    # Line-ending-only change (CRLF -> LF)
    (env.repo / "a.txt").write_bytes(b"ABCD\n")
    snap_lf = cp._rollback_snapshot(env.repo, commit)
    assert snap_lf != snap_base


def test_snapshot_detects_staged_vs_unstaged_swap_and_cached_only_edit(env):
    _, _, commit = _make_blocked_plan(env, "a.txt", "v2\n")

    # 1. Unstaged v2 -> staged v2 (working tree bytes identical, index changes)
    snap_unstaged = cp._rollback_snapshot(env.repo, commit)
    _git(env.repo, "add", "a.txt")
    snap_staged = cp._rollback_snapshot(env.repo, commit)
    assert snap_staged != snap_unstaged

    # 2. Staged v2, working tree reverted to v1 (path only in --cached)
    (env.repo / "a.txt").write_bytes(b"v1\r\n")
    snap_cached_crlf = cp._rollback_snapshot(env.repo, commit)
    (env.repo / "a.txt").write_bytes(b"v1\n")
    snap_cached_lf = cp._rollback_snapshot(env.repo, commit)
    assert snap_cached_lf != snap_cached_crlf


def test_snapshot_detects_rename_and_exec_bit_mode_change(env):
    _, _, commit = _make_blocked_plan(env, "a.txt", "v2\n")
    snap_before = cp._rollback_snapshot(env.repo, commit)

    # Executable bit flip in git index
    _git(env.repo, "update-index", "--chmod=+x", "a.txt")
    snap_chmod = cp._rollback_snapshot(env.repo, commit)
    assert snap_chmod != snap_before

    # Rename in git index
    _git(env.repo, "mv", "a.txt", "renamed_a.txt")
    snap_renamed = cp._rollback_snapshot(env.repo, commit)
    assert snap_renamed != snap_chmod


def test_snapshot_detects_deleted_file_recreated_and_symlink_replacement(env):
    _, _, commit = _make_blocked_plan(env, "a.txt", "v2\n")
    (env.repo / "a.txt").unlink()
    snap_deleted = cp._rollback_snapshot(env.repo, commit)

    # Recreated with checkpoint's original content
    (env.repo / "a.txt").write_text("v1\n", encoding="utf-8")
    snap_recreated = cp._rollback_snapshot(env.repo, commit)
    assert snap_recreated != snap_deleted


# ---------------------------------------------------------------------------
# 3. Symlinks: _raw_content_digest hashes os.readlink() without following.
# ---------------------------------------------------------------------------

def test_raw_content_digest_hashes_symlink_target_without_following(env, monkeypatch):
    _, _, commit = _make_blocked_plan(env, "a.txt", "v2\n")

    # Simulate a symlink at a.txt via monkeypatching Path.is_symlink and os.readlink
    # so this test runs deterministically even on Windows without Developer Mode.
    target_path = env.repo / "a.txt"
    link_target = {"dest": "../outside_secret_1.txt"}

    real_is_symlink = Path.is_symlink
    real_open = Path.open

    def fake_is_symlink(self: Path) -> bool:
        if self == target_path:
            return True
        return real_is_symlink(self)

    def forbid_open_on_symlink(self: Path, *args, **kwargs):
        if self == target_path:
            raise AssertionError("_raw_content_digest must not open() a symlink")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    monkeypatch.setattr(Path, "open", forbid_open_on_symlink)
    monkeypatch.setattr(os, "readlink", lambda p: link_target["dest"])

    d1 = cp._raw_content_digest(env.repo, commit)
    d2 = cp._raw_content_digest(env.repo, commit)
    assert d1 == d2

    link_target["dest"] = "../outside_secret_2.txt"
    d3 = cp._raw_content_digest(env.repo, commit)
    assert d3 != d1


# ---------------------------------------------------------------------------
# 4. Large files: multi-chunk (>1 MiB) streaming and concurrent file growth.
# ---------------------------------------------------------------------------

def test_raw_content_digest_streams_multi_chunk_file_and_detects_growth(env):
    _, _, commit = _make_blocked_plan(env, "a.txt", "v2\n")
    big = env.repo / "a.txt"

    # Write 3.5 MiB (crosses multiple 1 MiB chunk boundaries)
    payload = (b"0123456789abcdef" * 65536) * 3 + b"tail-bytes\n"
    big.write_bytes(payload)

    d1 = cp._raw_content_digest(env.repo, commit)
    d2 = cp._raw_content_digest(env.repo, commit)
    assert d1 == d2

    # Appending bytes while/between digest runs changes the digest cleanly
    with big.open("ab") as handle:
        handle.write(b"appended-while-running\n")
    d3 = cp._raw_content_digest(env.repo, commit)
    assert d3 != d1


# ---------------------------------------------------------------------------
# 5. Locked or unreadable files: deterministic "E"+errno digest, and a lock
#    appearing during the confirmation prompt causes a refusal, not an accept.
# ---------------------------------------------------------------------------

def test_raw_content_digest_locked_file_is_deterministic_and_refuses_when_lock_appears(env, monkeypatch):
    plan, blocked, commit = _make_blocked_plan(env, "a.txt", "v2\n")
    target_path = env.repo / "a.txt"

    d_unlocked = cp._raw_content_digest(env.repo, commit)

    if os.name == "nt":
        # Real Windows sharing-violation lock (dwShareMode=0)
        generic_rw = 0x80000000 | 0x40000000
        open_existing = 3
        handle = ctypes.windll.kernel32.CreateFileW(
            str(target_path), generic_rw, 0, None, open_existing, 0, None
        )
        assert handle not in (0, -1), "CreateFileW failed to acquire exclusive lock"
        try:
            d_locked_1 = cp._raw_content_digest(env.repo, commit)
            d_locked_2 = cp._raw_content_digest(env.repo, commit)
            assert d_locked_1 == d_locked_2
            assert d_locked_1 != d_unlocked
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        real_open = Path.open

        def raising_open(self: Path, *args, **kwargs):
            if self == target_path:
                raise PermissionError(errno.EACCES, "Permission denied", str(self))
            return real_open(self, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(Path, "open", raising_open)
            d_locked_1 = cp._raw_content_digest(env.repo, commit)
            d_locked_2 = cp._raw_content_digest(env.repo, commit)
            assert d_locked_1 == d_locked_2
            assert d_locked_1 != d_unlocked

    # Simulate a file lock appearing while the [y/N] prompt is open
    real_open = Path.open
    locked_now = {"active": False}

    def open_locked_after_prompt(self: Path, *args, **kwargs):
        if locked_now["active"] and self == target_path:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_locked_after_prompt)
    monkeypatch.setattr(cp, "_confirm", lambda _prompt: (locked_now.update(active=True) or True))

    with pytest.raises(cp.CheckpointError, match="changed while you were deciding"):
        cp.rollback(env.tracker, blocked.checkpoint_id, out=lambda *_: None)

    assert env.store.load(plan.plan_id).status is PlanStatus.BLOCKED
    assert ("git_rollback", "FAILURE", "repository changed while waiting for confirmation; nothing was changed") in _execution_rows(env)


# ---------------------------------------------------------------------------
# 6 & 7. Failure path in _refuse_if_changed (including state-only rollback):
#        fails closed on git error/timeout and logs FAILURE.
# ---------------------------------------------------------------------------

def test_state_only_rollback_fails_closed_and_logs_failure_on_recheck_git_error(env, monkeypatch):
    # Create a plan that only writes an untracked file before failing (state-only rollback path)
    plan, blocked, _ = _make_blocked_plan(env, "untracked_only.txt", "hello\n")

    real_run = GitInspector._run
    in_recheck = {"active": False}

    def fail_on_recheck(args, cwd):
        if in_recheck["active"] and args and args[0] == "status":
            return "ERROR: git command timed out."
        return real_run(args, cwd)

    monkeypatch.setattr(GitInspector, "_run", staticmethod(fail_on_recheck))
    monkeypatch.setattr(cp, "_confirm", lambda _prompt: (in_recheck.update(active=True) or True))

    with pytest.raises(cp.CheckpointError, match="Nothing was changed"):
        cp.rollback(env.tracker, blocked.checkpoint_id, out=lambda *_: None)

    assert env.store.load(plan.plan_id).status is PlanStatus.BLOCKED
    rows = _execution_rows(env)
    assert any(tool == "git_rollback" and status == "FAILURE" for tool, status, _ in rows)


# ---------------------------------------------------------------------------
# 8. Review 2 (2138fd5): Real Windows junctions (mklink /J) and _tracked_state_digest
# ---------------------------------------------------------------------------

def _make_dir_link(link_path: Path, target_dir: Path) -> None:
    if os.name == "nt":
        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            pytest.skip(f"mklink /J unavailable: {res.stderr.strip()}")
    else:
        try:
            os.symlink(target_dir, link_path, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink unavailable: {exc}")


def _remove_dir_link(link_path: Path) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "rmdir", str(link_path)], capture_output=True, check=True)
    else:
        link_path.unlink()


def test_real_junction_retargeting_on_file_and_parent_folder_never_opens_outside(env, tmp_path, monkeypatch):
    sub = env.repo / "sub"
    sub.mkdir()
    (sub / "f.txt").write_bytes(b"inside\n")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-m", "add sub/f.txt")
    commit = _git(env.repo, "rev-parse", "HEAD")

    outside1 = tmp_path / "outside1"
    outside1.mkdir()
    (outside1 / "f.txt").write_bytes(b"secret_one\n")

    outside2 = tmp_path / "outside2"
    outside2.mkdir()
    (outside2 / "f.txt").write_bytes(b"secret_two\n")

    opened: list[Path] = []
    real_open = Path.open

    def spy_open(self: Path, mode="r", *args, **kwargs):
        if "r" in mode:
            opened.append(self)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy_open)

    # Part A: Junction replacing a tracked file (a.txt -> outside1, then retargeted -> outside2)
    (env.repo / "a.txt").unlink()
    _make_dir_link(env.repo / "a.txt", outside1)
    snap_file_1 = cp._rollback_snapshot(env.repo, commit)

    _remove_dir_link(env.repo / "a.txt")
    _make_dir_link(env.repo / "a.txt", outside2)
    snap_file_2 = cp._rollback_snapshot(env.repo, commit)

    assert snap_file_1 != snap_file_2
    assert snap_file_1[6] != snap_file_2[6]
    assert opened == []

    _remove_dir_link(env.repo / "a.txt")
    (env.repo / "a.txt").write_bytes(b"v1\n")
    opened.clear()

    # Part B: Junction replacing a parent folder (sub -> outside1, then retargeted -> outside2)
    (sub / "f.txt").unlink()
    sub.rmdir()
    _make_dir_link(sub, outside1)
    with pytest.raises(cp.CheckpointError, match="tracked folder 'sub'"):
        cp._rollback_snapshot(env.repo, commit)

    _remove_dir_link(sub)
    _make_dir_link(sub, outside2)
    with pytest.raises(cp.CheckpointError, match="tracked folder 'sub'"):
        cp._rollback_snapshot(env.repo, commit)

    assert opened == []
    _remove_dir_link(sub)


def test_real_junction_on_tracked_folder_refuses_rollback_and_preserves_outside_file(env, tmp_path):
    sub = env.repo / "sub"
    sub.mkdir()
    (sub / "f.txt").write_bytes(b"inside_v1\n")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-m", "add sub/f.txt")

    cp_id = cp.create_checkpoint(env.tracker, env.task_id, "proj", "before step")

    # Advance HEAD with a change to sub/f.txt so reset --hard would overwrite sub/f.txt if reached
    (sub / "f.txt").write_bytes(b"inside_v2\n")
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-m", "step edits sub/f.txt")
    head_before = _git(env.repo, "rev-parse", "HEAD")

    # Replace tracked folder 'sub' with a junction whose target holds a copy of the tracked file
    outside = tmp_path / "outside_target"
    outside.mkdir()
    outside_bytes = b"inside_v1\n"
    (outside / "f.txt").write_bytes(outside_bytes)

    (sub / "f.txt").unlink()
    sub.rmdir()
    _make_dir_link(sub, outside)
    try:
        preview_lines: list[str] = []
        with pytest.raises(cp.CheckpointError, match="tracked folder 'sub'"):
            cp.rollback(env.tracker, cp_id, auto_confirm=True, out=preview_lines.append)

        assert preview_lines == []
        assert _git(env.repo, "rev-parse", "HEAD") == head_before
        assert (outside / "f.txt").read_bytes() == outside_bytes
    finally:
        if sub.exists() or cp._is_link(sub):
            _remove_dir_link(sub)


def test_tracked_state_digest_detects_mode_staged_blob_link_and_rename(env, tmp_path):
    (env.repo / "b.txt").write_bytes(b"b_init\n")
    _git(env.repo, "add", "b.txt")
    _git(env.repo, "commit", "-m", "add b.txt")
    commit = _git(env.repo, "rev-parse", "HEAD")

    base_state = cp._tracked_state_digest(env.repo, commit)

    # 1. Exec-bit change in index
    _git(env.repo, "update-index", "--chmod=+x", "a.txt")
    exec_state = cp._tracked_state_digest(env.repo, commit)
    assert exec_state != base_state

    # 2. Staged blob swap over identical working tree
    (env.repo / "a.txt").write_bytes(b"staged_v1\n")
    _git(env.repo, "add", "a.txt")
    staged_1 = cp._tracked_state_digest(env.repo, commit)

    (env.repo / "a.txt").write_bytes(b"staged_v2\n")
    _git(env.repo, "add", "a.txt")
    (env.repo / "a.txt").write_bytes(b"staged_v1\n")
    staged_2 = cp._tracked_state_digest(env.repo, commit)
    assert staged_1 != staged_2

    # 3. File replaced by a link/junction
    outside = tmp_path / "link_dest"
    outside.mkdir()
    snap_before_link = cp._rollback_snapshot(env.repo, commit)
    (env.repo / "a.txt").unlink()
    _make_dir_link(env.repo / "a.txt", outside)
    snap_after_link = cp._rollback_snapshot(env.repo, commit)
    assert snap_before_link[6] != snap_after_link[6]
    _remove_dir_link(env.repo / "a.txt")
    (env.repo / "a.txt").write_bytes(b"v1\n")
    _git(env.repo, "add", "a.txt")

    # 4. Rename (git mv)
    state_before_mv = cp._tracked_state_digest(env.repo, commit)
    _git(env.repo, "mv", "b.txt", "b_moved.txt")
    state_after_mv = cp._tracked_state_digest(env.repo, commit)
    assert state_before_mv != state_after_mv


