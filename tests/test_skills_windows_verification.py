"""
Windows-specific verification tests for Skills v1 (ADR-0006).
Branch: feature/antigravity-skills-v1-review
Platform: Windows 11 HP ZBook (real NTFS junctions and line endings).
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.core.skills import (
    SkillError,
    SkillStore,
    _hash_files,
    _read_text,
    load_skill_folder,
)
from jarvis.state.tracker import StateTracker


@pytest.fixture
def skill_env(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(ws))
    skills = ws / "skills"
    skills.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SECRET.txt").write_text("outside data\n")

    db_path = tmp_path / "test.db"
    tracker = StateTracker(db_path=db_path)
    store = SkillStore(db_path)
    return SimpleNamespace(ws=ws, skills=skills, outside=outside, db=db_path, tracker=tracker, store=store)


def make_junction(link_path: Path, target_path: Path):
    if os.name != "nt":
        pytest.skip("Windows NTFS junctions required")
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        pytest.skip(f"Could not create junction: {res.stderr}")


def test_real_windows_junction_at_skills_root_is_refused(skill_env):
    shutil.rmtree(skill_env.skills)
    make_junction(skill_env.skills, skill_env.outside)
    try:
        with pytest.raises(SkillError, match="outside the workspace|link or junction"):
            load_skill_folder("demo")
    finally:
        os.rmdir(skill_env.skills)


def test_real_windows_junction_at_skill_folder_is_refused(skill_env):
    demo = skill_env.skills / "demo"
    make_junction(demo, skill_env.outside)
    try:
        with pytest.raises(SkillError, match="The skill folder 'demo' is a link or junction"):
            load_skill_folder("demo")
        info = skill_env.store.state("demo")
        assert info.state == "refused"
        assert "link or junction" in info.detail
    finally:
        os.rmdir(demo)


def test_real_windows_junction_swapped_post_approval_refuses_show_and_is_not_usable(skill_env):
    demo = skill_env.skills / "demo"
    demo.mkdir()
    (demo / "SKILL.md").write_text("---\nname: demo\ndescription: valid demo\n---\nbody\n")

    # Approve cleanly first:
    res = skill_env.store.approve("demo", auto_confirm=True)
    assert "Approved skill" in res
    assert skill_env.store.is_usable("demo") is True

    # Swap in a junction post-approval:
    shutil.rmtree(demo)
    make_junction(demo, skill_env.outside)
    try:
        # State must now be refused and usable must be False:
        info = skill_env.store.state("demo")
        assert info.state == "refused"
        assert "link or junction" in info.detail
        assert skill_env.store.is_usable("demo") is False

        # show() must refuse without following:
        with pytest.raises(SkillError, match="link or junction"):
            skill_env.store.show("demo")
    finally:
        os.rmdir(demo)


def test_real_windows_junction_at_references_is_refused(skill_env):
    demo = skill_env.skills / "demo"
    demo.mkdir()
    (demo / "SKILL.md").write_text("---\nname: demo\ndescription: valid demo\n---\nbody\n")
    refs = demo / "references"
    make_junction(refs, skill_env.outside)
    try:
        with pytest.raises(SkillError, match="'demo/references' is a link or junction"):
            load_skill_folder("demo")
    finally:
        os.rmdir(refs)


def test_real_windows_dangling_junction_is_refused(skill_env, tmp_path):
    demo = skill_env.skills / "demo"
    temp_target = tmp_path / "to_delete"
    temp_target.mkdir()
    make_junction(demo, temp_target)
    os.rmdir(temp_target)  # Now dangling

    try:
        with pytest.raises(SkillError, match="link or junction"):
            load_skill_folder("demo")
    finally:
        os.rmdir(demo)


def test_crlf_lf_and_lone_cr_produce_identical_hashes(tmp_path):
    f_lf = tmp_path / "f_lf.md"
    f_crlf = tmp_path / "f_crlf.md"
    f_cr = tmp_path / "f_cr.md"

    f_lf.write_bytes(b"---\nname: test\ndescription: test\n---\nline 1\nline 2\n")
    f_crlf.write_bytes(b"---\r\nname: test\r\ndescription: test\r\n---\r\nline 1\r\nline 2\r\n")
    f_cr.write_bytes(b"---\rname: test\rdescription: test\r---\rline 1\rline 2\r")

    t_lf = _read_text(f_lf, "SKILL.md")
    t_crlf = _read_text(f_crlf, "SKILL.md")
    t_cr = _read_text(f_cr, "SKILL.md")

    h_lf = _hash_files({"SKILL.md": t_lf})
    h_crlf = _hash_files({"SKILL.md": t_crlf})
    h_cr = _hash_files({"SKILL.md": t_cr})

    assert h_lf == h_crlf == h_cr


def test_case_sensitivity_references_and_skill_md_refused(skill_env):
    demo = skill_env.skills / "demo"
    demo.mkdir()
    (demo / "SKILL.md").write_text("---\nname: demo\ndescription: valid\n---\nbody\n")

    # Capitalized References folder is refused:
    bad_refs = demo / "References"
    bad_refs.mkdir()
    (bad_refs / "doc.md").write_text("# Doc\n")
    with pytest.raises(SkillError, match="'References' is not allowed"):
        load_skill_folder("demo")
    shutil.rmtree(bad_refs)

    # Lowercase skill.md is refused:
    (demo / "SKILL.md").unlink()
    (demo / "skill.md").write_text("---\nname: demo\ndescription: valid\n---\nbody\n")
    with pytest.raises(SkillError, match="'skill.md' is not allowed"):
        load_skill_folder("demo")
