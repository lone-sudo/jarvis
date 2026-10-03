"""
Direct GitInspector tests. Written after build-log 0015 found that
get_current_branch/get_recent_log had no existence check (unlike
get_status), which crashed with an uncaught NotADirectoryError on
Windows for a registered project whose directory doesn't exist on
disk. GitInspector had no dedicated test file before this -- only
indirect coverage through cmd_resume/session-consolidate tests, none
of which exercised a missing directory.
"""

import subprocess

import pytest

from jarvis.tools.git import GitInspector


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


def _init_real_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@pytest.mark.parametrize("method_name,args", [
    ("get_status", ()),
    ("get_current_branch", ()),
    ("get_recent_log", ()),
])
def test_all_three_read_methods_handle_missing_directory_gracefully(method_name, args):
    """
    The actual regression: before the fix, only get_status had this
    check. All three must now behave consistently.
    """
    method = getattr(GitInspector, method_name)
    result = method("nonexistent-project", *args)
    assert "does not exist" in result
    assert "Traceback" not in result  # never a raw exception leaking through


@pytest.mark.parametrize("method_name,args", [
    ("get_status", ()),
    ("get_current_branch", ()),
    ("get_recent_log", ()),
])
def test_all_three_read_methods_handle_non_git_directory_gracefully(tmp_path, method_name, args):
    from jarvis.policy.rules import SecurityPolicy
    plain_dir = SecurityPolicy.get_workspace_root() / "plain"
    plain_dir.mkdir(parents=True)  # exists, but never `git init`'d

    method = getattr(GitInspector, method_name)
    result = method("plain", *args)
    assert "Not a git repository" in result


def test_get_current_branch_on_real_repo():
    from jarvis.policy.rules import SecurityPolicy
    repo = SecurityPolicy.get_workspace_root() / "real-repo"
    _init_real_repo(repo)
    branch = GitInspector.get_current_branch("real-repo")
    assert branch in ("main", "master")  # git's default varies by global config


def test_get_recent_log_on_real_repo():
    from jarvis.policy.rules import SecurityPolicy
    repo = SecurityPolicy.get_workspace_root() / "real-repo2"
    _init_real_repo(repo)
    log = GitInspector.get_recent_log("real-repo2")
    assert "init" in log


def test_get_status_clean_vs_dirty():
    from jarvis.policy.rules import SecurityPolicy
    repo = SecurityPolicy.get_workspace_root() / "real-repo3"
    _init_real_repo(repo)
    assert GitInspector.get_status("real-repo3") == "Working directory clean."
    (repo / "f.txt").write_text("changed")
    assert "f.txt" in GitInspector.get_status("real-repo3")


def test_path_traversal_rejected_by_all_three_read_methods():
    for method_name in ("get_status", "get_current_branch", "get_recent_log"):
        result = getattr(GitInspector, method_name)("../../etc")
        assert "denied by policy" in result.lower()
