from pathlib import Path

from jarvis.policy.rules import SecurityPolicy


class ProjectMemory:
    """
    Human-readable project memory: one Markdown notes file per project,
    stored under <workspace_root>/<project_relative_root>/JARVIS_NOTES.md.
    """

    NOTES_FILENAME = "JARVIS_NOTES.md"

    @classmethod
    def _notes_path(cls, project_relative_root: str) -> Path:
        # Gemini's review (point 3): this previously concatenated paths
        # directly with no boundary check, relying entirely on callers
        # having already validated project_relative_root. Routed through
        # the same canonical helper tools/fs.py and tools/git.py use, so
        # the invariant can't quietly diverge between modules.
        project_root = SecurityPolicy.resolve_safe_path(project_relative_root)
        return project_root / cls.NOTES_FILENAME

    @classmethod
    def append_note(cls, project_relative_root: str, note: str) -> str:
        try:
            path = cls._notes_path(project_relative_root)
        except PermissionError as e:
            return f"ERROR: Access denied by policy. {e}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {note}\n" if note.startswith("#") else f"\n{note}\n")
        return f"Note added to {project_relative_root}."

    @classmethod
    def read_latest(cls, project_relative_root: str, max_chars: int = 2000) -> str:
        try:
            path = cls._notes_path(project_relative_root)
        except PermissionError as e:
            return f"ERROR: Access denied by policy. {e}"
        if not path.exists():
            return "(no notes yet)"
        text = path.read_text(encoding="utf-8")
        return text[-max_chars:] if len(text) > max_chars else text
