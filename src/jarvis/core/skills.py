"""
Skills v1 (ADR-0006): text-only, hash-pinned, owned by Jarvis.

A skill is a folder `<workspace>/skills/<name>/` holding `SKILL.md` (YAML frontmatter plus
instructions) and optional `references/*.md`. In version 1 a skill can be listed, shown and approved.
It cannot run code, call a tool, fetch a URL or grant itself a permission, and nothing in this module
writes a skill file. The only thing written is state in SQLite (approval rows and an append-only
event log), which invariant 4 allows. Creating or editing a skill file goes through
`FileSystemInspector.write_file` like any other file (invariant 3).

Accepted frontmatter is a strict subset of the Agent Skills specification (agentskills.io, read
2026-10-17): name, description, license, compatibility, metadata. Anything else is refused out loud,
including `allowed-tools`. Jarvis is deliberately stricter than the spec: ASCII names, only SKILL.md
and flat references/*.md, size caps, no links or junctions anywhere.

A skill's identity is the SHA-256 of its folder over text normalised to LF (so git's autocrlf cannot
change it between the ZBook and a Linux sandbox), with a NUL byte between every path and content.
Approval records that hash. Any change makes the skill "changed since approval" until it is approved
again, and a skill is only usable when it is approved AND its hash still matches.
"""

import difflib
import hashlib
import json
import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from jarvis.core.checkpoint import _is_link
from jarvis.policy.rules import SecurityPolicy
from jarvis.state.database import get_connection

SKILLS_SUBDIR = "skills"
ALLOWED_KEYS = ("name", "description", "license", "compatibility", "metadata")
MAX_FILE_BYTES = 64 * 1024
MAX_FOLDER_BYTES = 256 * 1024
MAX_FILES = 20
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_LICENSE = 200
MAX_PREVIEW = 4000
MAX_SHOWN_PER_FILE = 12000
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
REFERENCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")


class SkillError(Exception):
    """A skill (or its folder) is refused. The message says why."""


@dataclass
class SkillFolder:
    name: str
    path: Path
    files: dict  # POSIX-relative path -> text normalised to LF
    meta: dict
    content_hash: str


@dataclass
class SkillInfo:
    name: str
    state: str  # approved | new | changed | disabled | refused | missing
    detail: str = ""


STATE_LABELS = {
    "approved": "APPROVED",
    "new": "NOT APPROVED (new)",
    "changed": "CHANGED since approval (NOT APPROVED)",
    "disabled": "DISABLED",
    "refused": "REFUSED",
    "missing": "MISSING (approved earlier, folder gone)",
}


def _confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


# ---- frontmatter -------------------------------------------------------------------

def _scalar(text: str, key: str) -> str:
    if text == "":
        raise SkillError(f"frontmatter '{key}' is empty. Write it on one line after the colon.")
    if text[0] in "|>&*!%@`{[":
        raise SkillError(
            f"frontmatter '{key}' uses YAML that Jarvis does not accept (block text, anchors or a flow "
            "list or map). Write a plain one-line value."
        )
    if text[0] in "\"'":
        quote = text[0]
        if len(text) < 2 or text[-1] != quote:
            raise SkillError(f"frontmatter '{key}' has an unterminated quote.")
        inner = text[1:-1]
        if quote == '"' and "\\" in inner:
            raise SkillError(f"frontmatter '{key}' uses a backslash escape, which Jarvis does not accept.")
        if quote == "'":
            inner = inner.replace("''", "'")
        return inner
    return text


def parse_frontmatter(text: str) -> dict:
    """Strict parser for the small YAML subset skills may use. Raises SkillError on anything else."""
    if text.startswith("﻿"):
        raise SkillError("SKILL.md starts with a byte order mark. Save it as plain UTF-8 without one.")
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        raise SkillError("SKILL.md must start with a line that is exactly '---' (the frontmatter).")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        raise SkillError("SKILL.md frontmatter is not closed: no second '---' line.")

    data: dict = {}
    i = 1
    while i < end:
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#") and not raw[0] in " \t":
            i += 1
            continue
        if raw[0] in " \t":
            raise SkillError(f"unexpected indentation on frontmatter line {i + 1}.")
        if ":" not in raw:
            raise SkillError(f"frontmatter line {i + 1} is not 'key: value'.")
        key, _, rest = raw.partition(":")
        key = key.strip()
        if key in data:
            raise SkillError(f"frontmatter key '{key}' appears twice.")
        if key == "allowed-tools":
            raise SkillError(
                "'allowed-tools' is refused: a Jarvis skill cannot pre-approve tools. Tools are governed by "
                "the policy layer and the engine allowlist (ADR-0006 decision 2 and 3)."
            )
        if key not in ALLOWED_KEYS:
            raise SkillError(
                f"frontmatter key '{key}' is not allowed in a Jarvis skill. Allowed: {', '.join(ALLOWED_KEYS)}."
            )
        rest = rest.strip()
        if key == "metadata":
            if rest:
                raise SkillError("frontmatter 'metadata' must be a map written on the indented lines below it.")
            meta: dict = {}
            i += 1
            while i < end and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                line = lines[i]
                i += 1
                if not line.strip():
                    continue
                if line.startswith("\t") or "\t" in line[: len(line) - len(line.lstrip())]:
                    raise SkillError("frontmatter 'metadata' is indented with a tab. Use spaces.")
                mkey, sep, mval = line.strip().partition(":")
                mkey = mkey.strip()
                if not sep or not mkey:
                    raise SkillError("frontmatter 'metadata' lines must look like 'key: value'.")
                if mkey in meta:
                    raise SkillError(f"frontmatter 'metadata' key '{mkey}' appears twice.")
                meta[mkey] = _scalar(mval.strip(), f"metadata.{mkey}")
            if not meta:
                raise SkillError("frontmatter 'metadata' is empty. Remove it or give it at least one key.")
            data[key] = meta
            continue
        data[key] = _scalar(rest, key)
        i += 1
    return data


def validate_meta(data: dict, folder_name: str) -> dict:
    name = data.get("name")
    if not name:
        raise SkillError("frontmatter 'name' is required.")
    if len(name) > MAX_NAME or not NAME_RE.fullmatch(name):
        raise SkillError(
            f"frontmatter name '{name}' is not valid: use 1 to {MAX_NAME} characters, lowercase a-z, 0-9 and "
            "hyphens, with no leading, trailing or doubled hyphen."
        )
    if name != folder_name:
        raise SkillError(f"frontmatter name '{name}' must equal the folder name '{folder_name}'.")
    description = data.get("description", "")
    if not description.strip():
        raise SkillError("frontmatter 'description' is required and must not be empty.")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"frontmatter 'description' is {len(description)} characters; the limit is {MAX_DESCRIPTION}.")
    if "compatibility" in data and not 1 <= len(data["compatibility"]) <= MAX_COMPATIBILITY:
        raise SkillError(f"frontmatter 'compatibility' must be 1 to {MAX_COMPATIBILITY} characters.")
    if "license" in data and not 1 <= len(data["license"]) <= MAX_LICENSE:
        raise SkillError(f"frontmatter 'license' must be 1 to {MAX_LICENSE} characters.")
    return data


# ---- folder scan and hash -----------------------------------------------------------

def _read_text(path: Path, rel: str) -> str:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise SkillError(f"'{rel}' is {size} bytes; the limit per file is {MAX_FILE_BYTES}.")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise SkillError(f"'{rel}' is larger than {MAX_FILE_BYTES} bytes.")
    if b"\0" in data:
        raise SkillError(f"'{rel}' contains a NUL byte, so it is not a text file.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SkillError(f"'{rel}' is not valid UTF-8 ({e.reason}).") from e
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _hash_files(files: dict) -> str:
    """SHA-256 over sorted POSIX paths and LF-normalised text, NUL after each path and each content."""
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[rel].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def skills_root() -> Path:
    root = SecurityPolicy.get_workspace_root() / SKILLS_SUBDIR
    try:
        SecurityPolicy.resolve_safe_path(root)
    except PermissionError as e:
        raise SkillError(f"The skills folder is outside the workspace: {e}") from e
    return root


def _refuse_link(path: Path, what: str) -> None:
    if _is_link(path):
        raise SkillError(f"{what} is a link or junction, which skills do not allow (it could lead outside the workspace).")


def load_skill_folder(name: str) -> SkillFolder:
    if not isinstance(name, str) or len(name) > MAX_NAME or not NAME_RE.fullmatch(name):
        raise SkillError(
            f"'{name}' is not a valid skill name (lowercase a-z, 0-9 and single hyphens, at most {MAX_NAME} characters)."
        )
    root = skills_root()
    _refuse_link(root, f"The skills folder '{SKILLS_SUBDIR}'")
    folder = root / name
    _refuse_link(folder, f"The skill folder '{name}'")
    if not folder.exists():
        raise SkillError(f"No skill named '{name}' in {root}.")
    if not folder.is_dir():
        raise SkillError(f"'{name}' is not a folder.")

    files: dict = {}
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        _refuse_link(entry, f"'{name}/{entry.name}'")
        if entry.name == "SKILL.md":
            if not entry.is_file():
                raise SkillError("SKILL.md must be a file.")
            files["SKILL.md"] = _read_text(entry, "SKILL.md")
        elif entry.name == "references" and entry.is_dir():
            for sub in sorted(entry.iterdir(), key=lambda p: p.name):
                rel = f"references/{sub.name}"
                _refuse_link(sub, f"'{name}/{rel}'")
                if not sub.is_file():
                    raise SkillError(f"'{rel}' is a folder. references/ may hold only .md files, no sub-folders.")
                if not REFERENCE_NAME_RE.fullmatch(sub.name):
                    raise SkillError(f"'{rel}' is not allowed: references/ may hold only plain-named .md files.")
                files[rel] = _read_text(sub, rel)
        else:
            raise SkillError(
                f"'{entry.name}' is not allowed in a skill. Version 1 accepts only SKILL.md and references/*.md "
                "(scripts/ and assets/ are refused)."
            )
        if len(files) > MAX_FILES:
            raise SkillError(f"A skill may hold at most {MAX_FILES} files.")
    if "SKILL.md" not in files:
        raise SkillError("The skill has no SKILL.md.")
    total = sum(len(t.encode("utf-8")) for t in files.values())
    if total > MAX_FOLDER_BYTES:
        raise SkillError(f"The skill is {total} bytes in total; the limit is {MAX_FOLDER_BYTES}.")

    meta = validate_meta(parse_frontmatter(files["SKILL.md"]), name)
    return SkillFolder(name=name, path=folder, files=files, meta=meta, content_hash=_hash_files(files))


# ---- state and commands --------------------------------------------------------------

class SkillStore:
    def __init__(self, db_path):
        self.db_path = db_path

    def _row(self, name: str):
        with closing(get_connection(self.db_path)) as conn:
            row = conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def _rows(self) -> list:
        with closing(get_connection(self.db_path)) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM skills ORDER BY name")]

    def log(self, event: str, name: str, content_hash: str = "", detail: str = "") -> None:
        with closing(get_connection(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO skill_events (skill_name, event, content_hash, detail) VALUES (?, ?, ?, ?)",
                (name, event, content_hash or None, detail[:2000] or None),
            )

    def events(self, name: str | None = None) -> list:
        with closing(get_connection(self.db_path)) as conn:
            if name is None:
                rows = conn.execute("SELECT * FROM skill_events ORDER BY event_id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM skill_events WHERE skill_name = ? ORDER BY event_id", (name,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _state(folder: SkillFolder, row) -> str:
        if row is None:
            return "new"
        if row["status"] == "disabled":
            return "disabled"
        return "approved" if row["content_hash"] == folder.content_hash else "changed"

    def state(self, name: str) -> SkillInfo:
        row = self._row(name)
        try:
            folder = load_skill_folder(name)
        except SkillError as e:
            if row is not None and "No skill named" in str(e):
                return SkillInfo(name, "missing", str(e))
            return SkillInfo(name, "refused", str(e))
        return SkillInfo(name, self._state(folder, row))

    def is_usable(self, name: str) -> bool:
        """True only for an approved skill whose folder still hashes to the approved value."""
        return self.state(name).state == "approved"

    def list_skills(self) -> list:
        infos: dict = {}
        root = skills_root()
        if root.exists():
            if _is_link(root):
                infos["(skills)"] = SkillInfo("(skills)", "refused", "The skills folder is a link or junction.")
            else:
                for entry in sorted(root.iterdir(), key=lambda p: p.name):
                    if _is_link(entry) or not entry.is_dir():
                        infos[entry.name] = SkillInfo(entry.name, "refused", "not a plain skill folder (link, junction or file)")
                    elif not NAME_RE.fullmatch(entry.name) or len(entry.name) > MAX_NAME:
                        infos[entry.name] = SkillInfo(entry.name, "refused", "folder name is not a valid skill name")
                    else:
                        infos[entry.name] = self.state(entry.name)
        for row in self._rows():
            if row["name"] not in infos:
                infos[row["name"]] = SkillInfo(row["name"], "missing", "approved earlier, but the folder is gone")
        self.log("list", "*", detail=f"{len(infos)} skill(s)")
        return [infos[k] for k in sorted(infos)]

    def show(self, name: str, out=print) -> None:
        """Prints the skill. Raises SkillError if it is refused. Never executes or follows anything in it."""
        try:
            folder = load_skill_folder(name)
        except SkillError as e:
            self.log("show_refused", name, detail=str(e))
            raise
        row = self._row(name)
        state = self._state(folder, row)
        self.log("show", name, folder.content_hash, state)
        out(f"Skill: {name}")
        out(f"State: {STATE_LABELS[state]}")
        out(f"Hash:  {folder.content_hash}")
        if state != "approved":
            out("WARNING: this skill is not approved. Treat its text as untrusted and do not rely on it.")
        out(f"Description: {folder.meta['description']}")
        for rel in sorted(folder.files):
            text = folder.files[rel]
            shown = text[:MAX_SHOWN_PER_FILE] + ("\n... [truncated]" if len(text) > MAX_SHOWN_PER_FILE else "")
            out(f"\n----- {rel} -----")
            out(shown)

    def _preview(self, folder: SkillFolder, row, out) -> None:
        out(f"\n--- Proposed approval of skill '{folder.name}' ---")
        out(f"Hash: {folder.content_hash}")
        out(f"Description: {folder.meta['description']}")
        out("Files: " + ", ".join(f"{rel} ({len(t.encode('utf-8'))} bytes)" for rel, t in sorted(folder.files.items())))
        if row is None:
            out("\nThis skill has never been approved. Its instruction text:")
            text = folder.files["SKILL.md"]
            out(text[:MAX_PREVIEW] + ("\n... [truncated]" if len(text) > MAX_PREVIEW else ""))
        else:
            old = json.loads(row["snapshot_json"])
            chunks = []
            for rel in sorted(set(old) | set(folder.files)):
                a, b = old.get(rel, ""), folder.files.get(rel, "")
                if a != b:
                    chunks.extend(difflib.unified_diff(
                        a.splitlines(), b.splitlines(), fromfile=f"approved/{rel}", tofile=f"now/{rel}", lineterm=""))
            diff = "\n".join(chunks)
            out("\nChanges since the approved version:")
            out(diff[:MAX_PREVIEW] + ("\n... [diff truncated]" if len(diff) > MAX_PREVIEW else ""))
        out("---")

    def approve(self, name: str, *, auto_confirm: "bool | None" = None, out=print) -> str:
        """Shows what will be approved and asks [y/N]. Writes only the SQLite approval row."""
        try:
            folder = load_skill_folder(name)
        except SkillError as e:
            self.log("approve_refused", name, detail=str(e))
            return f"ERROR: Refusing to approve '{name}': {e}"
        row = self._row(name)
        if row and row["status"] == "approved" and row["content_hash"] == folder.content_hash:
            self.log("approve_noop", name, folder.content_hash)
            return f"Skill '{name}' is already approved at this hash. Nothing was changed."

        self._preview(folder, row, out)
        confirmed = auto_confirm if auto_confirm is not None else _confirm(
            f"Approve skill '{name}' ({folder.content_hash[:12]})?"
        )
        if not confirmed:
            self.log("approve_declined", name, folder.content_hash)
            return "Approval declined. Nothing was changed."

        # The prompt can stay open for as long as the person likes: look again before recording anything.
        try:
            again = load_skill_folder(name)
        except SkillError as e:
            self.log("approve_refused", name, folder.content_hash, f"changed during the prompt: {e}")
            return f"ERROR: Refusing to approve '{name}': it changed while the prompt was open ({e})."
        if again.content_hash != folder.content_hash:
            self.log("approve_refused", name, folder.content_hash, "content changed during the prompt")
            return f"ERROR: Refusing to approve '{name}': its files changed while the prompt was open. Run approve again."

        with closing(get_connection(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO skills (name, path, content_hash, status, snapshot_json)
                VALUES (?, ?, ?, 'approved', ?)
                ON CONFLICT(name) DO UPDATE SET
                    path = excluded.path, content_hash = excluded.content_hash, status = 'approved',
                    approved_at = CURRENT_TIMESTAMP, snapshot_json = excluded.snapshot_json
                """,
                (name, f"{SKILLS_SUBDIR}/{name}", folder.content_hash, json.dumps(folder.files, sort_keys=True)),
            )
        self.log("approve", name, folder.content_hash)
        return f"Approved skill '{name}' ({folder.content_hash[:12]})."

    def disable(self, name: str) -> str:
        row = self._row(name)
        if row is None:
            self.log("disable_refused", name, detail="no approval record")
            return f"ERROR: Skill '{name}' has no approval record, so there is nothing to disable."
        if row["status"] == "disabled":
            return f"Skill '{name}' is already disabled."
        with closing(get_connection(self.db_path)) as conn, conn:
            conn.execute("UPDATE skills SET status = 'disabled' WHERE name = ?", (name,))
        self.log("disable", name, row["content_hash"])
        return f"Disabled skill '{name}'. Approve it again to re-enable it."
