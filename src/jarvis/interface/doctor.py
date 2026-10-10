"""Read-only diagnostics for a Jarvis development or runtime setup."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _requires_python() -> str:
    try:
        return importlib.metadata.metadata("jarvis").get("Requires-Python") or _requires_from_pyproject()
    except importlib.metadata.PackageNotFoundError:
        return _requires_from_pyproject()


def _requires_from_pyproject() -> str:
    project_root = Path(__file__).resolve().parents[3]
    text = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^requires-python\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise ValueError("requires-python is missing from pyproject.toml")
    return match.group(1)


def _version_satisfies(version: tuple[int, ...], requirement: str) -> bool:
    """Evaluate the simple PEP 440 version constraints used by requires-python."""
    parts = re.findall(r"(>=|<=|==|~=|>|<)\s*(\d+(?:\.\d+){0,2})", requirement)
    if not parts:
        raise ValueError(f"Unsupported requires-python value: {requirement!r}")
    padded = version + (0,) * (3 - len(version))
    for operator, value in parts:
        target_parts = tuple(int(part) for part in value.split("."))
        target = target_parts + (0,) * (3 - len(target_parts))
        if operator == ">=" and padded < target:
            return False
        if operator == ">" and padded <= target:
            return False
        if operator == "<=" and padded > target:
            return False
        if operator == "<" and padded >= target:
            return False
        if operator == "==" and padded != target:
            return False
        if operator == "~=" and not (padded >= target and padded[0] == target[0]):
            return False
    return True


def check_python() -> Check:
    try:
        requirement = _requires_python()
        current = tuple(sys.version_info[:3])
        current_text = ".".join(str(part) for part in current)
        if _version_satisfies(current, requirement):
            return Check("Python", "OK", f"Python {current_text} satisfies requires-python {requirement}.")
        return Check("Python", "FAIL", f"Python {current_text} does not satisfy requires-python {requirement}.")
    except (OSError, ValueError, TypeError) as exc:
        return Check("Python", "FAIL", f"Could not verify the Python requirement: {exc}")


def check_pip() -> Check:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("pip", "WARN", f"pip is unavailable ({exc}); install/upgrade commands will not work.")
    if result.returncode == 0:
        return Check("pip", "OK", (result.stdout or result.stderr).strip())
    return Check("pip", "WARN", (result.stderr or result.stdout or "python -m pip failed").strip())


def check_git() -> Check:
    path = shutil.which("git")
    if path:
        return Check("git", "OK", f"Found {path}.")
    return Check("git", "FAIL", "git was not found on PATH.")


def check_jarvis_import() -> Check:
    try:
        importlib.import_module("jarvis.interface.cli")
    except Exception as exc:
        return Check("Jarvis import", "FAIL", f"Could not import jarvis.interface.cli: {exc}")
    return Check("Jarvis import", "OK", "jarvis.interface.cli imports successfully.")


def check_console_command() -> Check:
    command = shutil.which("jarvis")
    if not command:
        return Check("jarvis command", "OK", "No installed jarvis command found; use the Python module command.")

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    fix = '$env:PYTHONPATH = "src"; python -m jarvis.interface.cli'
    try:
        result = subprocess.run(
            [command, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        error_line = _first_error_line(exc.stderr) or _first_error_line(exc.stdout)
        if not error_line:
            error_line = "command timed out after 10 seconds"
        return Check("jarvis command", "WARN", f"{error_line}. Use: {fix}")
    except (OSError, subprocess.SubprocessError) as exc:
        error_line = _first_error_line(str(exc)) or exc.__class__.__name__
        return Check("jarvis command", "WARN", f"Could not run installed command: {error_line}. Use: {fix}")

    if result.returncode == 0:
        return Check("jarvis command", "OK", f"{command} --help completed successfully.")
    error_line = _first_error_line(result.stderr) or _first_error_line(result.stdout)
    if not error_line:
        error_line = f"command exited with status {result.returncode}"
    return Check("jarvis command", "WARN", f"{error_line}. Use: {fix}")


def _first_error_line(output: str | bytes | None) -> str:
    if not output:
        return ""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    if lines[0].startswith("Traceback "):
        return lines[-1]
    # Traceback headers and frame locations are less actionable than the exception.
    for line in lines:
        if not line.startswith(("Traceback ", "File \"")):
            return line
    return lines[-1]


def _workspace_path() -> Path:
    configured = os.getenv("JARVIS_WORKSPACE_ROOT")
    return Path(configured) if configured else Path.home() / "jarvis-workspace"


def check_workspace() -> Check:
    try:
        raw = _workspace_path()
        root = raw.resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as exc:
        return Check("Workspace root", "FAIL", f"Could not resolve the configured workspace root: {exc}")
    if not root.exists():
        return Check("Workspace root", "FAIL", f"Workspace root does not exist: {root}")
    if not root.is_dir():
        return Check("Workspace root", "FAIL", f"Workspace root is not a directory: {root}")
    if not os.access(root, os.W_OK):
        return Check("Workspace root", "FAIL", f"Workspace root is not writable: {root}")
    return Check("Workspace root", "OK", f"Resolves, exists, and is writable: {root}")


def check_database_path() -> Check:
    try:
        root = _workspace_path().resolve(strict=False)
        database = (root.parent / ".jarvis" / "jarvis_state.db").resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as exc:
        return Check("Database path", "FAIL", f"Could not resolve the database path: {exc}")
    return Check("Database path", "OK", f"Resolves to {database}.")


def run_checks() -> list[Check]:
    return [
        check_python(),
        check_pip(),
        check_git(),
        check_jarvis_import(),
        check_console_command(),
        check_workspace(),
        check_database_path(),
    ]
