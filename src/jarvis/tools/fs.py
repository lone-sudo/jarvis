import difflib
from pathlib import Path

from jarvis.policy.rules import ActionTier, SecurityPolicy
from jarvis.policy.validator import PolicyValidator, PolicyViolation


def _confirm(prompt: str) -> bool:
    """Explicit Y/n confirmation. Anything other than 'y'/'yes' is a decline."""
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


class FileSystemInspector:
    @staticmethod
    def read_file(relative_path: str, max_chars: int = 20000) -> str:
        target = SecurityPolicy.get_workspace_root() / relative_path
        try:
            PolicyValidator.authorize_tool("read_file", ActionTier.OBSERVE, target)
        except PolicyViolation as e:
            return f"ERROR: Access denied by policy. {e}"
        if not target.is_file():
            return f"ERROR: '{target}' is not a file."
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"ERROR: could not read file ({e})."
        if len(text) > max_chars:
            return text[:max_chars] + f"\n... [truncated, {len(text) - max_chars} more chars]"
        return text

    @staticmethod
    def list_directory(relative_path: str = "") -> list[str]:
        target = SecurityPolicy.get_workspace_root() / relative_path
        try:
            PolicyValidator.authorize_tool("list_directory", ActionTier.OBSERVE, target)
        except PolicyViolation as e:
            return [f"ERROR: Access denied by policy. {e}"]
        if not target.is_dir():
            return [f"ERROR: '{target}' is not a directory."]
        return sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())

    @staticmethod
    def write_file(relative_path: str, new_content: str, auto_confirm: bool | None = None) -> str:
        """
        SAFE_WRITE tier. Shows a unified diff against the current content
        (or marks it as a new file) and requires explicit [y/N] confirmation
        before writing, unless auto_confirm is set (used by tests only —
        interactive callers must go through the real prompt).
        """
        target = SecurityPolicy.get_workspace_root() / relative_path
        try:
            PolicyValidator.authorize_tool("write_file", ActionTier.SAFE_WRITE, target)
        except PolicyViolation as e:
            return f"ERROR: Access denied by policy. {e}"

        old_content = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        diff = "\n".join(
            difflib.unified_diff(
                old_content.splitlines(), new_content.splitlines(),
                fromfile=f"current: {relative_path}", tofile=f"proposed: {relative_path}",
                lineterm="",
            )
        )

        print(f"\n--- Proposed write to {target} ---")
        print(diff if diff else "(no textual change)")
        print("---")

        confirmed = auto_confirm if auto_confirm is not None else _confirm(f"Write these changes to {relative_path}?")
        if not confirmed:
            return "ERROR: Access denied by policy. (declined by user)"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")
        return f"Wrote {len(new_content)} chars to {relative_path}."
