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
        return SecurityPolicy.get_workspace_root() / project_relative_root / cls.NOTES_FILENAME

    @classmethod
    def append_note(cls, project_relative_root: str, note: str) -> None:
        path = cls._notes_path(project_relative_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {note}\n" if note.startswith("#") else f"\n{note}\n")

    @classmethod
    def read_latest(cls, project_relative_root: str, max_chars: int = 2000) -> str:
        path = cls._notes_path(project_relative_root)
        if not path.exists():
            return "(no notes yet)"
        text = path.read_text(encoding="utf-8")
        return text[-max_chars:] if len(text) > max_chars else text
