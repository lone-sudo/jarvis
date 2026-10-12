import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.tools import fs as fs_module
from jarvis.tools.fs import FileSystemInspector
from jarvis.tools.git import GitInspector


def _make_dir_link(link: Path, target: Path) -> bool:
    """Directory link: a symlink if allowed, else a Windows junction (no admin needed)."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        return made.returncode == 0
    return False


def _remove_dir_link(link: Path) -> None:
    """Remove a symlink or junction WITHOUT touching what it points at."""
    try:
        link.unlink()
    except (IsADirectoryError, PermissionError, OSError):
        os.rmdir(link)


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))


def test_git_run_adds_safe_options_to_status_and_preserves_callers_config(monkeypatch, tmp_path):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr("jarvis.tools.git.subprocess.run", fake_run)
    assert GitInspector._run(["-c", "color.ui=always", "status", "--short"], tmp_path) == "ok"
    assert seen["args"] == [
        "git", "-c", "color.ui=always", "-c", "core.fsmonitor=false",
        "-c", f"core.hooksPath={os.devnull}", "status", "--short",
    ]
    assert seen["kwargs"]["cwd"] == tmp_path


def test_git_run_disables_diff_drivers(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        "jarvis.tools.git.subprocess.run",
        lambda args, **kwargs: (seen.update(args=args) or subprocess.CompletedProcess(args, 0, "", "")),
    )
    GitInspector._run(["diff", "--cached"], tmp_path)
    assert seen["args"] == [
        "git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
        "diff", "--no-ext-diff", "--no-textconv", "--cached",
    ]


def test_git_run_does_not_override_hooks_for_commit(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        "jarvis.tools.git.subprocess.run",
        lambda args, **kwargs: (seen.update(args=args) or subprocess.CompletedProcess(args, 0, "", "")),
    )
    GitInspector._run(["commit", "-m", "message"], tmp_path)
    assert seen["args"] == ["git", "-c", "core.fsmonitor=false", "commit", "-m", "message"]


def test_write_file_authorizes_target_twice(monkeypatch):
    from jarvis.policy.validator import PolicyValidator

    original = PolicyValidator.authorize_tool
    calls = []

    def counted(tool_name, tier, target_path=None):
        calls.append((tool_name, tier, target_path))
        return original(tool_name, tier, target_path)

    monkeypatch.setattr(PolicyValidator, "authorize_tool", counted)
    assert "Wrote" in FileSystemInspector.write_file("notes.txt", "content", auto_confirm=True)
    assert len(calls) == 2
    assert all(call[0] == "write_file" for call in calls)


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "file.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def _marker_command(marker: Path) -> str:
    # Git parses its config command string into argv; quote the executable and
    # Python snippet for that parser. Forward slashes keep Git for Windows
    # from treating backslashes in paths as escapes.
    executable = Path(sys.executable).as_posix()
    marker_path = marker.as_posix()
    code = f"from pathlib import Path; Path({marker_path!r}).touch()"
    return f'"{executable}" -c "{code}"'


def test_status_disables_configured_fsmonitor(tmp_path):
    repo = tmp_path / "fsmonitor-repo"
    marker = tmp_path / "fsmonitor-ran"
    _init_repo(repo)
    command = _marker_command(marker)
    subprocess.run(["git", "config", "core.fsmonitor", command], cwd=repo, check=True)

    result = GitInspector._run(["status", "--short"], repo)
    assert not result.startswith("ERROR:")
    assert not marker.exists()


def test_diff_disables_configured_textconv(tmp_path):
    repo = tmp_path / "textconv-repo"
    marker = tmp_path / "textconv-ran"
    _init_repo(repo)
    command = _marker_command(marker)
    subprocess.run(["git", "config", "diff.marker.textconv", command], cwd=repo, check=True)
    (repo / ".gitattributes").write_text("*.txt diff=marker\n", encoding="utf-8")
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")

    result = GitInspector._run(["diff"], repo)
    assert not result.startswith("ERROR:")
    assert not marker.exists()


def test_status_disables_configured_post_index_change_hook(tmp_path):
    repo = tmp_path / "hook-repo"
    hooks = tmp_path / "hooks"
    marker = tmp_path / "hook-ran"
    _init_repo(repo)
    hooks.mkdir()
    hook = hooks / "post-index-change"
    hook.write_text(
        f"#!{Path(sys.executable).as_posix()}\n"
        "from pathlib import Path\n"
        f"Path({marker.as_posix()!r}).touch()\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.hooksPath", hooks.as_posix()], cwd=repo, check=True)
    # Force status to refresh the index stat cache, which invokes this hook.
    os.utime(repo / "file.txt", None)

    result = GitInspector._run(["status", "--short"], repo)
    assert not result.startswith("ERROR:")
    assert not marker.exists()


def test_write_file_rejects_symlink_retarget_during_confirmation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    first = workspace / "first"
    second = workspace / "second"
    first.mkdir(parents=True)
    second.mkdir()
    link = workspace / "link"
    if not _make_dir_link(link, first):
        pytest.skip("cannot create a symlink or junction here")

    def retarget(_prompt):
        _remove_dir_link(link)
        if not _make_dir_link(link, second):
            pytest.skip("cannot create a symlink or junction here")
        return True

    monkeypatch.setattr(fs_module, "_confirm", retarget)
    try:
        result = FileSystemInspector.write_file("link/new.txt", "content")
        assert result == "ERROR: Access denied by policy. Path changed while waiting for confirmation."
        assert not (first / "new.txt").exists()
        assert not (second / "new.txt").exists()
    finally:
        if link.exists() or link.is_symlink():
            _remove_dir_link(link)
