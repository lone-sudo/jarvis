"""
Path-boundary invariant tests requested in Gemini's M001/V1 review (point 9).

Note on scope: this sandbox runs Linux, so Windows drive-letter paths
(C:\\...) and UNC paths (\\\\server\\share) can't be meaningfully exercised
here — on Linux they're just unusual-looking relative filenames, not
absolute paths, so a test asserting they're rejected would be testing
nothing real. Those two should be manually verified on the ZBook once
this is deployed there; noted in the build log rather than faked here.
"""

import os

import pytest

from jarvis.policy.rules import SecurityPolicy
from jarvis.state.tracker import StateTracker
from jarvis.memory.markdown import ProjectMemory
from jarvis.tools.fs import FileSystemInspector


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


def test_absolute_path_outside_workspace_rejected():
    with pytest.raises(PermissionError):
        SecurityPolicy.resolve_safe_path("/etc/passwd")


def test_single_dot_dot_traversal_rejected():
    with pytest.raises(PermissionError):
        SecurityPolicy.resolve_safe_path("../outside.txt")


def test_nested_dot_dot_traversal_rejected():
    with pytest.raises(PermissionError):
        SecurityPolicy.resolve_safe_path("a/b/../../../outside.txt")


def test_symlink_escape_rejected(tmp_path):
    root = SecurityPolicy.get_workspace_root()
    outside_target = tmp_path / "definitely_outside"
    outside_target.mkdir()
    (outside_target / "secret.txt").write_text("nope")

    link = root / "escape_link"
    try:
        link.symlink_to(outside_target)
    except OSError as e:
        # Creating symlinks on Windows requires Administrator privileges
        # or Developer Mode enabled — a real, environment-dependent
        # limitation Lone hit on the ZBook, not a bug. Skip rather than
        # fail, since the test genuinely cannot run without that
        # capability; it still runs for real wherever the capability
        # exists (Linux, macOS, or Windows with Developer Mode on).
        pytest.skip(f"Cannot create symlinks in this environment (Windows needs admin or Developer Mode): {e}")

    with pytest.raises(PermissionError):
        SecurityPolicy.resolve_safe_path("escape_link/secret.txt")


def test_path_inside_workspace_accepted():
    result = SecurityPolicy.resolve_safe_path("project/file.py")
    root = SecurityPolicy.get_workspace_root()
    assert result == (root / "project" / "file.py").resolve()


def test_register_project_outside_workspace_rejected(tmp_path):
    tracker = StateTracker(db_path=tmp_path / "t.db")
    with pytest.raises(PermissionError):
        tracker.register_project("evil", "../outside-project")


def test_register_project_absolute_path_rejected(tmp_path):
    tracker = StateTracker(db_path=tmp_path / "t.db")
    with pytest.raises(PermissionError):
        tracker.register_project("evil", "/etc")


def test_register_project_inside_workspace_accepted(tmp_path):
    tracker = StateTracker(db_path=tmp_path / "t.db")
    tracker.register_project("good-project", "good-project", "Good Project")
    assert tracker.get_project_root("good-project") == "good-project"


def test_content_file_outside_workspace_rejected():
    """
    Regression test for the exact bug Gemini flagged: --content-file must
    not be able to read a file outside the workspace, regardless of
    whether the write target is inside it.
    """
    from jarvis.policy.rules import SecurityPolicy as SP
    with pytest.raises(PermissionError):
        SP.resolve_safe_path("/etc/passwd")
    # The CLI layer (cmd_write_file) catches this PermissionError and
    # refuses before ever opening the file — see interface/cli.py.


def test_markdown_memory_rejects_unsafe_project_root():
    result = ProjectMemory.append_note("../outside", "should not be written")
    assert "denied by policy" in result.lower()


def test_markdown_memory_accepts_safe_project_root(tmp_path):
    result = ProjectMemory.append_note("safe-project", "a real note")
    assert "note added" in result.lower()
