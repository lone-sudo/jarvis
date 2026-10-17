"""
Skills v1 (ADR-0006): text-only, hash-pinned, approval by [y/N], links refused everywhere.

Junctions are simulated by patching Path.is_junction on a real folder (the technique the rollback and
plan-commit tests use); a real symlink test runs where the machine allows one.
"""

import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.core import skills as skills_module
from jarvis.core.skills import (
    SkillError, SkillStore, _hash_files, load_skill_folder, parse_frontmatter, skills_root,
)
from jarvis.interface.cli import main
from jarvis.state.tracker import StateTracker

GOOD = """---
name: {name}
description: Turns a saved link into a short note. Use when Lone saves a link.
license: MIT
metadata:
  author: lone
  version: "1.0"
---

# {name}

1. Read the link text.
2. Write three lines.
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(workspace))
    tracker = StateTracker(db_path=tmp_path / "s.db")
    store = SkillStore(tracker.db_path)
    root = workspace / "skills"

    def make(name="link-to-note", body=None, extra=None, refs=None):
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(body if body is not None else GOOD.format(name=name), encoding="utf-8", newline="")
        if refs:
            (folder / "references").mkdir(exist_ok=True)
            for rel, text in refs.items():
                (folder / "references" / rel).write_text(text, encoding="utf-8", newline="")
        for rel, text in (extra or {}).items():
            target = folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return folder

    return SimpleNamespace(workspace=workspace, root=root, tracker=tracker, store=store, make=make, tmp=tmp_path)


def pretend_is_junction(monkeypatch, name):
    real = getattr(Path, "is_junction", lambda self: False)
    monkeypatch.setattr(Path, "is_junction", lambda self: self.name == name or real(self), raising=False)


def state_of(env, name):
    return env.store.state(name).state


def quiet(_line):
    return None


# ---- the happy path ---------------------------------------------------------------

def test_a_valid_skill_loads_and_is_new_until_approved(env):
    env.make()
    folder = load_skill_folder("link-to-note")
    assert folder.meta["name"] == "link-to-note" and folder.meta["metadata"] == {"author": "lone", "version": "1.0"}
    assert state_of(env, "link-to-note") == "new"
    assert env.store.is_usable("link-to-note") is False


def test_approve_records_the_hash_and_makes_the_skill_usable(env):
    env.make()
    result = env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    assert result.startswith("Approved skill 'link-to-note'")
    assert state_of(env, "link-to-note") == "approved"
    assert env.store.is_usable("link-to-note") is True


def test_declining_the_prompt_changes_nothing(env):
    env.make()
    result = env.store.approve("link-to-note", auto_confirm=False, out=quiet)
    assert "declined" in result.lower()
    assert state_of(env, "link-to-note") == "new"
    assert [e["event"] for e in env.store.events("link-to-note")] == ["approve_declined"]


def test_the_prompt_defaults_to_no(env, monkeypatch):
    env.make()
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert "declined" in env.store.approve("link-to-note", out=quiet).lower()
    assert state_of(env, "link-to-note") == "new"


# ---- hash pinning -----------------------------------------------------------------

def test_a_one_byte_edit_unapproves_the_skill(env):
    folder = env.make()
    env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    (folder / "SKILL.md").write_text(GOOD.format(name="link-to-note") + "x", encoding="utf-8", newline="")
    assert state_of(env, "link-to-note") == "changed"
    assert env.store.is_usable("link-to-note") is False


def test_editing_a_reference_file_also_unapproves_the_skill(env):
    folder = env.make(refs={"notes.md": "one\n"})
    env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    (folder / "references" / "notes.md").write_text("two\n", encoding="utf-8", newline="")
    assert state_of(env, "link-to-note") == "changed"


def test_crlf_and_lf_give_the_same_hash(env):
    env.make("lf-skill", body=GOOD.format(name="lf-skill"))
    env.make("crlf-skill", body=GOOD.format(name="crlf-skill").replace("crlf-skill", "lf-skill").replace("\n", "\r\n"))
    # same content except the folder name, so compare the hash of the same text under the same name
    lf = load_skill_folder("lf-skill").content_hash
    (env.root / "lf-skill" / "SKILL.md").write_text(GOOD.format(name="lf-skill").replace("\n", "\r\n"), encoding="utf-8", newline="")
    assert load_skill_folder("lf-skill").content_hash == lf


def test_a_trailing_newline_difference_changes_the_hash(env):
    folder = env.make()
    before = load_skill_folder("link-to-note").content_hash
    (folder / "SKILL.md").write_text(GOOD.format(name="link-to-note") + "\n", encoding="utf-8", newline="")
    assert load_skill_folder("link-to-note").content_hash != before


def test_the_hash_has_an_unambiguous_separator():
    # Without a NUL between path and content both of these would be the same byte stream "abcde".
    assert _hash_files({"a": "bc", "d": "e"}) != _hash_files({"a": "b", "cd": "e"})


def test_changed_files_show_a_diff_against_the_approved_version(env):
    folder = env.make()
    env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    (folder / "SKILL.md").write_text(GOOD.format(name="link-to-note").replace("three lines", "ten lines"), encoding="utf-8", newline="")
    lines = []
    env.store.approve("link-to-note", auto_confirm=False, out=lines.append)
    text = "\n".join(lines)
    assert "-2. Write three lines." in text and "+2. Write ten lines." in text


def test_a_skill_that_changes_during_the_prompt_is_not_approved(env, monkeypatch):
    folder = env.make()

    def edit_then_yes(_prompt):
        (folder / "SKILL.md").write_text(GOOD.format(name="link-to-note") + "\nsneaky\n", encoding="utf-8", newline="")
        return "y"

    monkeypatch.setattr("builtins.input", edit_then_yes)
    result = env.store.approve("link-to-note", out=quiet)
    assert result.startswith("ERROR: Refusing to approve") and "while the prompt was open" in result
    assert state_of(env, "link-to-note") == "new"


# ---- what a skill may contain ---------------------------------------------------------

@pytest.mark.parametrize("line", [
    "allowed-tools: Bash(git:*) Read",
    "tools: Read",
    "model: sonnet",
])
def test_unknown_or_permission_keys_are_refused(env, line):
    env.make(body=GOOD.format(name="link-to-note").replace("license: MIT", line))
    with pytest.raises(SkillError, match="allowed-tools|not allowed"):
        load_skill_folder("link-to-note")
    assert env.store.approve("link-to-note", auto_confirm=True, out=quiet).startswith("ERROR: Refusing to approve")


@pytest.mark.parametrize("extra", [
    {"scripts/run.py": "print(1)"},
    {"assets/logo.txt": "x"},
    {"LICENSE.txt": "MIT"},
    {"notes.md": "stray"},
    {"references/sub/deep.md": "nested"},
    {"references/data.json": "{}"},
])
def test_scripts_assets_and_stray_files_are_refused(env, extra):
    env.make(extra=extra)
    with pytest.raises(SkillError):
        load_skill_folder("link-to-note")


@pytest.mark.parametrize("bad_name", ["Link-To-Note", "-link", "link-", "link--note", "link_note", "a" * 65])
def test_bad_names_are_refused(env, bad_name):
    with pytest.raises(SkillError, match="not a valid skill name"):
        load_skill_folder(bad_name)


def test_frontmatter_name_must_match_the_folder(env):
    env.make("link-to-note", body=GOOD.format(name="something-else"))
    with pytest.raises(SkillError, match="must equal the folder name"):
        load_skill_folder("link-to-note")


@pytest.mark.parametrize("frontmatter, message", [
    ("name: x\ndescription:", "empty"),
    ("name: x\ndescription: " + "d" * 1025, "limit is 1024"),
    ("name: x\ndescription: ok\ncompatibility: " + "c" * 501, "compatibility"),
    ("name: x\ndescription: >\n  folded", "does not accept|empty"),
    ("name: x\ndescription: ok\ndescription: again", "twice"),
    ("name: x\ndescription: ok\nmetadata:", "empty"),
    ("name: x\ndescription: ok\nmetadata: {a: b}", "indented lines"),
])
def test_bad_frontmatter_is_refused(env, frontmatter, message):
    env.make("x", body=f"---\n{frontmatter}\n---\nbody\n")
    with pytest.raises(SkillError, match=message):
        load_skill_folder("x")


@pytest.mark.parametrize("text, message", [
    ("no frontmatter here", "must start with"),
    ("---\nname: x\ndescription: ok\n", "not closed"),
    ("\ufeff---\nname: x\ndescription: ok\n---\n", "byte order mark"),
])
def test_missing_or_broken_frontmatter_fences_are_refused(text, message):
    with pytest.raises(SkillError, match=message):
        parse_frontmatter(text)


def test_nul_bytes_bad_utf8_and_oversize_files_are_refused(env):
    folder = env.make()
    (folder / "SKILL.md").write_bytes(b"---\nname: link-to-note\n\x00")
    with pytest.raises(SkillError, match="NUL"):
        load_skill_folder("link-to-note")
    (folder / "SKILL.md").write_bytes(b"---\nname: link-to-note\n\xff\xfe\n")
    with pytest.raises(SkillError, match="UTF-8"):
        load_skill_folder("link-to-note")
    (folder / "SKILL.md").write_text(GOOD.format(name="link-to-note") + "x" * 70000, encoding="utf-8")
    with pytest.raises(SkillError, match="limit per file"):
        load_skill_folder("link-to-note")


# ---- containment -------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "C:/x", "/etc"])
def test_a_name_that_could_escape_the_skills_folder_is_refused(env, bad):
    with pytest.raises(SkillError, match="not a valid skill name"):
        load_skill_folder(bad)


def test_a_junction_at_the_skills_folder_is_refused(env, monkeypatch):
    env.make()
    pretend_is_junction(monkeypatch, "skills")
    with pytest.raises(SkillError, match="link or junction"):
        load_skill_folder("link-to-note")
    assert env.store.list_skills()[0].state == "refused"


def test_a_junction_at_the_skill_folder_is_refused(env, monkeypatch):
    env.make()
    pretend_is_junction(monkeypatch, "link-to-note")
    with pytest.raises(SkillError, match="link or junction"):
        load_skill_folder("link-to-note")


def test_a_junction_inside_a_skill_is_refused(env, monkeypatch):
    env.make(refs={"notes.md": "x\n"})
    pretend_is_junction(monkeypatch, "references")
    with pytest.raises(SkillError, match="link or junction"):
        load_skill_folder("link-to-note")


def test_a_real_symlink_inside_a_skill_is_refused_and_not_followed(env):
    folder = env.make()
    secret = env.tmp / "secret.md"
    secret.write_text("OUTSIDE\n", encoding="utf-8")
    try:
        os.symlink(secret, folder / "SKILL.md.bak")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symlink here")
    with pytest.raises(SkillError, match="link or junction"):
        load_skill_folder("link-to-note")


def test_a_skills_folder_outside_the_workspace_is_refused(env, monkeypatch):
    monkeypatch.setattr(skills_module.SecurityPolicy, "resolve_safe_path",
                        classmethod(lambda cls, p: (_ for _ in ()).throw(PermissionError("outside"))))
    with pytest.raises(SkillError, match="outside the workspace"):
        skills_root()


# ---- list, show, disable, events ---------------------------------------------------------

def test_list_reports_every_state(env):
    env.make("fresh-one")
    env.make("approved-one")
    env.store.approve("approved-one", auto_confirm=True, out=quiet)
    env.make("bad-one", extra={"scripts/x.sh": "echo"})
    env.make("gone-one")
    env.store.approve("gone-one", auto_confirm=True, out=quiet)
    import shutil
    shutil.rmtree(env.root / "gone-one")
    states = {i.name: i.state for i in env.store.list_skills()}
    assert states == {"fresh-one": "new", "approved-one": "approved", "bad-one": "refused", "gone-one": "missing"}


def test_list_with_no_skills_folder_is_empty_not_an_error(env):
    assert env.store.list_skills() == []


def test_show_prints_the_skill_and_warns_when_it_is_not_approved(env):
    env.make()
    lines = []
    env.store.show("link-to-note", out=lines.append)
    text = "\n".join(lines)
    assert "NOT APPROVED (new)" in text and "WARNING" in text and "Write three lines" in text


def test_show_of_an_approved_skill_has_no_warning(env):
    env.make()
    env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    lines = []
    env.store.show("link-to-note", out=lines.append)
    assert "APPROVED" in "\n".join(lines) and "WARNING" not in "\n".join(lines)


def test_disable_blocks_use_and_reapproval_enables_it_again(env):
    env.make()
    env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    assert env.store.disable("link-to-note").startswith("Disabled")
    assert state_of(env, "link-to-note") == "disabled" and env.store.is_usable("link-to-note") is False
    env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    assert env.store.is_usable("link-to-note") is True


def test_disable_of_an_unknown_skill_is_an_error(env):
    env.make()
    assert env.store.disable("link-to-note").startswith("ERROR")


def test_every_list_show_and_approval_is_logged(env):
    env.make()
    env.store.list_skills()
    env.store.show("link-to-note", out=quiet)
    env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    events = [e["event"] for e in env.store.events()]
    assert events == ["list", "show", "approve"]
    assert env.store.events("link-to-note")[-1]["content_hash"] == load_skill_folder("link-to-note").content_hash


def test_a_refused_approval_is_logged_too(env):
    env.make(extra={"scripts/x.sh": "echo"})
    env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    assert env.store.events("link-to-note")[-1]["event"] == "approve_refused"


# ---- nothing is written to the skills folder, and the CLI works end to end ---------------------

def snapshot(root):
    return sorted((str(p.relative_to(root)), p.read_bytes()) for p in root.rglob("*") if p.is_file())


def test_skill_commands_never_write_into_the_skills_folder(env):
    env.make(refs={"notes.md": "x\n"})
    before = snapshot(env.root)
    env.store.list_skills()
    env.store.show("link-to-note", out=quiet)
    env.store.approve("link-to-note", auto_confirm=True, out=quiet)
    env.store.disable("link-to-note")
    assert snapshot(env.root) == before


def run_cli(*argv):
    sys.argv = ["jarvis", *argv]
    main()


def test_cli_list_show_approve_disable(env, monkeypatch, capsys):
    env.make()
    run_cli("skill", "list")
    assert "link-to-note" in capsys.readouterr().out
    run_cli("skill", "show", "link-to-note")
    assert "WARNING" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    run_cli("skill", "approve", "link-to-note")
    assert "Approved skill" in capsys.readouterr().out
    run_cli("skill", "list")
    assert "APPROVED" in capsys.readouterr().out
    run_cli("skill", "disable", "link-to-note")
    assert "Disabled" in capsys.readouterr().out


def test_cli_prints_an_error_for_a_bad_skill_instead_of_crashing(env, capsys):
    run_cli("skill", "show", "does-not-exist")
    assert capsys.readouterr().out.startswith("ERROR:")


def test_the_skills_tables_are_created_by_the_migration(env):
    conn = sqlite3.connect(env.tracker.db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"skills", "skill_events"} <= tables
