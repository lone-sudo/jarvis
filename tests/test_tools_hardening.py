import os
import subprocess
from pathlib import Path

import pytest

from jarvis.tools import fs as fs_module
from jarvis.tools.fs import FileSystemInspector
from jarvis.tools.git import GitInspector


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


def _marker_script(path: Path, marker: Path):
    path.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="marker scripts require POSIX shell execution")
def test_status_disables_configured_fsmonitor(tmp_path):
    repo = tmp_path / "fsmonitor-repo"
    marker = tmp_path / "fsmonitor-ran"
    _init_repo(repo)
    script = tmp_path / "fsmonitor.sh"
    _marker_script(script, marker)
    subprocess.run(["git", "config", "core.fsmonitor", str(script)], cwd=repo, check=True)

    result = GitInspector._run(["status", "--short"], repo)
    assert not result.startswith("ERROR:")
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="marker scripts require POSIX shell execution")
def test_diff_disables_configured_textconv(tmp_path):
    repo = tmp_path / "textconv-repo"
    marker = tmp_path / "textconv-ran"
    _init_repo(repo)
    script = tmp_path / "textconv.sh"
    _marker_script(script, marker)
    subprocess.run(["git", "config", "diff.marker.textconv", str(script)], cwd=repo, check=True)
    (repo / ".gitattributes").write_text("*.txt diff=marker\n", encoding="utf-8")
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")

    result = GitInspector._run(["diff"], repo)
    assert not result.startswith("ERROR:")
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="marker scripts require POSIX shell execution")
def test_status_disables_configured_post_index_change_hook(tmp_path):
    repo = tmp_path / "hook-repo"
    hooks = tmp_path / "hooks"
    marker = tmp_path / "hook-ran"
    _init_repo(repo)
    hooks.mkdir()
    _marker_script(hooks / "post-index-change", marker)
    subprocess.run(["git", "config", "core.hooksPath", str(hooks)], cwd=repo, check=True)

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
    try:
        link.symlink_to(first, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    def retarget(_prompt):
        link.unlink()
        try:
            link.symlink_to(second, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        return True

    monkeypatch.setattr(fs_module, "_confirm", retarget)
    result = FileSystemInspector.write_file("link/new.txt", "content")
    assert result == "ERROR: Access denied by policy. Path changed while waiting for confirmation."
    assert not (first / "new.txt").exists()
    assert not (second / "new.txt").exists()
