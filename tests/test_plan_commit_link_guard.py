"""
Junction guard on a plan's git_commit (M002.1, ADR-0005).

Git for Windows walks into a directory junction as if it were a normal folder, and Path.is_symlink()
is False for one. A plan commit must therefore refuse a path that is, or sits under, a link, both
before it shows the preview and again after the [y/N] (the prompt can stay open indefinitely).
Mirrors the rollback's `_refuse_if_tracked_folder_is_a_link`.
"""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.core import plan_commit
from jarvis.core.plan_commit import commit_plan_paths


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


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
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("SECRET from outside the project\n")
    return SimpleNamespace(repo=repo, outside=outside, tmp=tmp_path)


def head(env):
    return git(env.repo, "rev-parse", "HEAD")


def staged(env):
    return git(env.repo, "diff", "--cached", "--name-only")


def write_plan_file(env):
    (env.repo / "sub").mkdir(exist_ok=True)
    (env.repo / "sub" / "f.txt").write_text("written by the plan\n")


def pretend_is_junction(monkeypatch, name):
    """Windows reports a junction through is_junction() and NOT is_symlink(): simulate it with a real folder."""
    real = getattr(Path, "is_junction", lambda self: False)
    monkeypatch.setattr(Path, "is_junction", lambda self: self.name == name or real(self), raising=False)


# ---- the normal case still works -----------------------------------------------

def test_a_plain_commit_still_works(env):
    write_plan_file(env)
    before = head(env)
    result = commit_plan_paths("proj", "add sub/f.txt", ["sub/f.txt"], auto_confirm=True)
    assert not result.startswith("ERROR"), result
    assert head(env) != before
    assert "sub/f.txt" in git(env.repo, "ls-files")


# ---- refused before anything is shown ---------------------------------------------

def test_a_junction_in_place_of_the_parent_folder_is_refused(env, monkeypatch, capsys):
    write_plan_file(env)
    pretend_is_junction(monkeypatch, "sub")
    before = head(env)
    result = commit_plan_paths("proj", "m", ["sub/f.txt"], auto_confirm=True)
    assert result.startswith("ERROR: Refusing to commit: the folder 'sub' is a link or junction")
    assert "Nothing was staged or committed" in result
    assert head(env) == before and staged(env) == ""
    assert "Proposed commit" not in capsys.readouterr().out   # refused before any preview


def test_a_link_at_the_file_itself_is_left_to_the_preview_not_refused_here(env, monkeypatch):
    # git records a symlinked file as a link and does not read through it, and the preview already
    # refuses to show its content (test_engine_core.py::test_preview_does_not_follow_a_symlink_...).
    write_plan_file(env)
    pretend_is_junction(monkeypatch, "f.txt")
    result = commit_plan_paths("proj", "m", ["sub/f.txt"], auto_confirm=True)
    assert "is a link or junction" not in result


def test_a_real_symlinked_folder_pointing_outside_is_refused_and_nothing_leaks(env):
    try:
        os.symlink(env.outside, env.repo / "sub", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symlink here")
    before = head(env)
    result = commit_plan_paths("proj", "m", ["sub/f.txt"], auto_confirm=True)
    assert result.startswith("ERROR: Refusing to commit: the folder 'sub' is a link or junction")
    assert head(env) == before and staged(env) == ""
    assert "sub" not in git(env.repo, "ls-files")


def test_a_real_windows_junction_is_refused_and_nothing_leaks(env):
    if os.name != "nt":
        pytest.skip("directory junctions exist only on Windows")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(env.repo / "sub"), str(env.outside)], capture_output=True
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    before = head(env)
    result = commit_plan_paths("proj", "m", ["sub/f.txt"], auto_confirm=True)
    assert result.startswith("ERROR: Refusing to commit: the folder 'sub' is a link or junction")
    assert head(env) == before and staged(env) == ""
    assert "sub" not in git(env.repo, "ls-files")
    os.rmdir(env.repo / "sub")   # remove the junction itself, never what it points at


# ---- the link appears while the prompt is open -------------------------------------

def test_a_link_swapped_in_during_the_prompt_is_caught_before_staging(env, monkeypatch):
    write_plan_file(env)

    def answer_yes_after_a_link_appears(_prompt):
        pretend_is_junction(monkeypatch, "sub")   # the person walks away, something swaps the folder
        return True

    monkeypatch.setattr(plan_commit, "_confirm", answer_yes_after_a_link_appears)
    before = head(env)
    result = commit_plan_paths("proj", "m", ["sub/f.txt"])   # real prompt path (auto_confirm=None)
    assert "changed while the prompt was open" in result
    assert head(env) == before and staged(env) == ""


def test_declining_still_reports_a_decline_not_a_link_error(env, monkeypatch):
    write_plan_file(env)
    monkeypatch.setattr(plan_commit, "_confirm", lambda _p: False)
    assert "(declined by user)" in commit_plan_paths("proj", "m", ["sub/f.txt"])
