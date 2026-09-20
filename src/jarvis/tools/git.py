import subprocess
from pathlib import Path

from jarvis.policy.rules import ActionTier, SecurityPolicy
from jarvis.policy.validator import PolicyValidator


def _confirm(prompt: str) -> bool:
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


class GitInspector:
    @staticmethod
    def _run(args: list[str], cwd: Path) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=15
            )
            return result.stdout.strip()
        except FileNotFoundError:
            # git itself isn't installed / not on PATH — distinct from a
            # git command failing, and Gemini's original code didn't
            # catch this case at all, so it would crash the CLI outright.
            return "ERROR: git executable not found."
        except subprocess.CalledProcessError as e:
            return f"ERROR: {e.stderr.strip() or e}"
        except subprocess.TimeoutExpired:
            return "ERROR: git command timed out."

    @staticmethod
    def get_status(project_relative_root: str) -> str:
        target_dir = SecurityPolicy.get_workspace_root() / project_relative_root

        if not PolicyValidator.authorize_tool("git_status", ActionTier.OBSERVE, target_dir):
            return "ERROR: Access denied by policy."
        if not target_dir.exists():
            return f"ERROR: '{target_dir}' does not exist."
        if not (target_dir / ".git").exists():
            return "ERROR: Not a git repository."

        return GitInspector._run(["status", "--short"], target_dir) or "Working directory clean."

    @staticmethod
    def get_current_branch(project_relative_root: str) -> str:
        target_dir = SecurityPolicy.get_workspace_root() / project_relative_root
        if not PolicyValidator.authorize_tool("git_branch", ActionTier.OBSERVE, target_dir):
            return "ERROR: Access denied by policy."
        return GitInspector._run(["branch", "--show-current"], target_dir)

    @staticmethod
    def get_recent_log(project_relative_root: str, count: int = 3) -> str:
        target_dir = SecurityPolicy.get_workspace_root() / project_relative_root
        if not PolicyValidator.authorize_tool("git_log", ActionTier.OBSERVE, target_dir):
            return "ERROR: Access denied by policy."
        return GitInspector._run(["log", f"-n{count}", "--oneline"], target_dir)

    @staticmethod
    def commit_changes(project_relative_root: str, message: str, auto_confirm: bool | None = None) -> str:
        """
        SAFE_WRITE tier. Shows `git status --short` and the pending diff,
        then requires explicit [y/N] confirmation before `git add -A` +
        `git commit`. Declining is logged as REJECTED_BY_POLICY by the
        caller (see tools/base.py::run_logged), not silently dropped.
        """
        target_dir = SecurityPolicy.get_workspace_root() / project_relative_root
        if not PolicyValidator.authorize_tool("git_commit", ActionTier.SAFE_WRITE, target_dir):
            return "ERROR: Access denied by policy."
        if not (target_dir / ".git").exists():
            return "ERROR: Not a git repository."

        status = GitInspector._run(["status", "--short"], target_dir)
        if not status:
            return "ERROR: Nothing to commit (working directory clean)."
        diff = GitInspector._run(["diff"], target_dir)

        print(f"\n--- Proposed commit in {target_dir} ---")
        print(f"Message: {message}")
        print("\nChanged files:")
        print(status)
        if diff:
            print("\nDiff:")
            print(diff[:4000] + ("\n... [diff truncated]" if len(diff) > 4000 else ""))
        print("---")

        confirmed = auto_confirm if auto_confirm is not None else _confirm("Commit these changes?")
        if not confirmed:
            return "ERROR: Access denied by policy. (declined by user)"

        add_result = GitInspector._run(["add", "-A"], target_dir)
        if add_result.startswith("ERROR"):
            return add_result
        commit_result = GitInspector._run(["commit", "-m", message], target_dir)
        return commit_result
