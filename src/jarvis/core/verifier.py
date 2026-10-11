"""
Verifiers for plan steps (M002, ADR-0005).

Targeted postcondition verifiers:
  - FileContentVerifier: verifies file existence and content.
  - PytestTargetVerifier: verifies test suites pass with detailed failure diagnostics.
  - GitCommitVerifier: verifies a git commit was created and matches expectations.

All verifiers are strictly read-only and must never mutate project files.
Types are imported directly from jarvis.core.plan (never redefined).
"""

import ntpath
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Any

from jarvis.core.plan import ExecutionStep, VerificationContext, VerificationResult
from jarvis.policy.rules import SecurityPolicy
from jarvis.tools.git import GitInspector

# Explicitly rejected pytest argument prefixes (ADR-0005 hardening)
REJECTED_PYTEST_ARG_PREFIXES = (
    "--basetemp",
    "--junitxml",
    "--cache-dir",
    "-o",
    "-p",
    "-c",
    "--rootdir",
    "--confcutdir",
    "--import-mode",
)

# Explicitly safe standalone pytest flags
SAFE_PYTEST_FLAGS = {
    "-v", "-vv", "-vvv", "--verbose",
    "-q", "-qq", "--quiet",
    "-s", "--capture=no",
    "-x", "--exitfirst",
    "--lf", "--last-failed",
    "--ff", "--failed-first",
    "-l", "--showlocals",
    "--strict-markers",
    "--disable-warnings",
}

# Explicitly safe pytest options that accept a value
SAFE_PYTEST_OPTIONS = {
    "-k", "--keyword",
    "-m", "--markers",
    "--maxfail",
    "--tb",
    "--durations",
    "-r",
}

# Explicit allow-list of parent environment variables passed to pytest child processes
ALLOWED_PYTEST_ENV_VARS = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_COLLATE",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
)


def _build_pytest_env() -> dict[str, str]:
    """
    Builds a minimal allow-listed environment for the pytest child process so parent
    secrets (API keys, bot tokens, etc.) never reach the test or leak into diagnostics.
    """
    env: dict[str, str] = {}
    for key in ALLOWED_PYTEST_ENV_VARS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Terminates the pytest subprocess and any child processes it spawned."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


def _is_absolute_path_arg(arg: str) -> bool:
    """Checks whether an argument string represents an absolute path on POSIX or Windows."""
    if Path(arg).is_absolute() or ntpath.isabs(arg):
        return True
    if arg.startswith(("/", "\\")) or bool(re.match(r"^[a-zA-Z]:[\\/]", arg)):
        return True
    return False


def _validate_pytest_args(raw_args: list[str]) -> list[str]:
    """
    Validates that pytest arguments match an explicit safe list and rejects
    any dangerous, configuration-overriding, or absolute-path arguments.
    """
    if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
        raise ValueError("pytest_target: 'args' must be a list of strings.")

    i = 0
    validated: list[str] = []
    while i < len(raw_args):
        arg = raw_args[i]

        # 1. Reject absolute paths
        if _is_absolute_path_arg(arg):
            raise PermissionError(
                f"pytest_target: argument '{arg}' is an absolute path, rejected by policy."
            )

        # 2. Reject forbidden prefixes
        for rej in REJECTED_PYTEST_ARG_PREFIXES:
            if arg == rej or arg.startswith(rej + "="):
                raise PermissionError(
                    f"pytest_target: argument '{arg}' is forbidden by policy."
                )

        # 3. Check allowed safe list
        if arg in SAFE_PYTEST_FLAGS:
            validated.append(arg)
            i += 1
        elif arg in SAFE_PYTEST_OPTIONS:
            if i + 1 >= len(raw_args):
                raise ValueError(f"pytest_target: option '{arg}' requires a value.")
            val = raw_args[i + 1]
            if _is_absolute_path_arg(val):
                raise PermissionError(
                    f"pytest_target: argument '{val}' for option '{arg}' is an absolute path, rejected by policy."
                )
            for rej in REJECTED_PYTEST_ARG_PREFIXES:
                if val == rej or val.startswith(rej + "="):
                    raise PermissionError(
                        f"pytest_target: argument '{val}' is forbidden by policy."
                    )
            validated.extend([arg, val])
            i += 2
        elif any(arg.startswith(opt + "=") for opt in SAFE_PYTEST_OPTIONS):
            opt, val = arg.split("=", 1)
            if _is_absolute_path_arg(val):
                raise PermissionError(
                    f"pytest_target: argument '{val}' for option '{opt}' is an absolute path, rejected by policy."
                )
            for rej in REJECTED_PYTEST_ARG_PREFIXES:
                if val == rej or val.startswith(rej + "="):
                    raise PermissionError(
                        f"pytest_target: argument '{val}' is forbidden by policy."
                    )
            validated.append(arg)
            i += 1
        else:
            raise PermissionError(
                f"pytest_target: argument '{arg}' is not in the allowed safe args list."
            )
    return validated


class FileContentVerifier:
    """
    Verifies that a file inside project_root exists and matches content expectations.
    Strictly read-only.
    """

    def __init__(self, spec: dict[str, Any]):
        if not isinstance(spec, dict):
            raise ValueError("file_content: spec must be a dictionary.")

        allowed_keys = {"type", "path", "content", "contains", "not_empty"}
        extra = set(spec) - allowed_keys
        if extra:
            raise ValueError(f"file_content: unexpected spec key(s) {sorted(extra)}.")

        raw_path = spec.get("path")
        if raw_path is not None:
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("file_content: 'path' must be a non-empty string.")
            p = Path(raw_path)
            if p.is_absolute() or ntpath.isabs(raw_path) or ".." in p.parts:
                raise PermissionError(
                    f"file_content: path '{raw_path}' attempts path traversal or is absolute."
                )
            self.path: str | None = raw_path
        else:
            self.path = None

        content = spec.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("file_content: 'content' must be a string.")
        self.content: str | None = content

        contains = spec.get("contains")
        if contains is not None:
            if isinstance(contains, str):
                self.contains: list[str] = [contains]
            elif isinstance(contains, list) and all(isinstance(c, str) for c in contains):
                self.contains = contains
            else:
                raise ValueError("file_content: 'contains' must be a string or list of strings.")
        else:
            self.contains = []

        not_empty = spec.get("not_empty")
        if not_empty is not None and not isinstance(not_empty, bool):
            raise ValueError("file_content: 'not_empty' must be a boolean.")
        self.not_empty: bool = bool(not_empty) if not_empty is not None else False

    def verify(self, step: ExecutionStep, ctx: VerificationContext) -> VerificationResult:
        rel = self.path or step.params.get("path")
        if not rel:
            return VerificationResult(ok=False, detail="file_content: missing 'path' in verify spec or step params.")

        target = ctx.project_root / rel
        try:
            resolved = target.resolve()
            # Enforce policy containment: must remain within workspace root and project root
            SecurityPolicy.resolve_safe_path(resolved)
            if resolved != ctx.project_root and ctx.project_root not in resolved.parents:
                raise PermissionError(
                    f"file_content policy breach: target '{rel}' resolves outside project root ({ctx.project_root})."
                )
        except PermissionError as e:
            raise PermissionError(f"file_content policy breach: {e}") from e

        if not resolved.exists():
            return VerificationResult(ok=False, detail=f"File does not exist: {rel}")
        if not resolved.is_file():
            return VerificationResult(ok=False, detail=f"Target is not a file: {rel}")

        try:
            actual = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return VerificationResult(ok=False, detail=f"File '{rel}' is not valid UTF-8 text.")

        expected = self.content
        if expected is None and not self.contains and self.path is None and "content" in step.params:
            expected = step.params["content"]

        if expected is not None and actual != expected:
            return VerificationResult(
                ok=False,
                detail=(
                    f"Content mismatch in '{rel}': expected {len(expected)} chars, "
                    f"got {len(actual)} chars."
                ),
            )

        if self.contains:
            missing = [c for c in self.contains if c not in actual]
            if missing:
                return VerificationResult(
                    ok=False,
                    detail=f"Content in '{rel}' missing expected substring(s): {missing}",
                )

        if self.not_empty and not actual.strip():
            return VerificationResult(ok=False, detail=f"File '{rel}' is empty.")

        return VerificationResult(ok=True, detail=f"File '{rel}' verified ({len(actual)} chars).")


class PytestTargetVerifier:
    """
    Executes a targeted pytest run within project_root.
    Respects ctx.time_remaining as the subprocess timeout budget.
    Runs pytest with '-p no:cacheprovider' and 'PYTHONDONTWRITEBYTECODE=1'.
    Preserves complete stdout and stderr diagnostics upon test failure.
    Strictly read-only.
    """

    def __init__(self, spec: dict[str, Any]):
        if not isinstance(spec, dict):
            raise ValueError("pytest_target: spec must be a dictionary.")

        allowed_keys = {"type", "target", "args", "timeout"}
        extra = set(spec) - allowed_keys
        if extra:
            raise ValueError(f"pytest_target: unexpected spec key(s) {sorted(extra)}.")

        raw_target = spec.get("target")
        if raw_target is not None:
            if not isinstance(raw_target, str) or not raw_target.strip():
                raise ValueError("pytest_target: 'target' must be a non-empty string.")
            p = Path(raw_target)
            if p.is_absolute() or ntpath.isabs(raw_target) or ".." in p.parts:
                raise PermissionError(
                    f"pytest_target: target '{raw_target}' attempts path traversal or is absolute."
                )
            self.target: str = raw_target
        else:
            self.target = ""

        raw_args = spec.get("args")
        if raw_args is not None:
            self.args: list[str] = _validate_pytest_args(raw_args)
        else:
            self.args = []

        timeout = spec.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError("pytest_target: 'timeout' must be a positive number.")
            self.timeout: float | None = float(timeout)
        else:
            self.timeout = None

    def verify(self, step: ExecutionStep, ctx: VerificationContext) -> VerificationResult:
        if self.target:
            target_path = (ctx.project_root / self.target).resolve()
            if target_path != ctx.project_root and ctx.project_root not in target_path.parents:
                raise PermissionError(
                    f"pytest_target policy breach: target '{self.target}' resolves outside project root."
                )
            SecurityPolicy.resolve_safe_path(target_path)

        if ctx.time_remaining <= 0:
            return VerificationResult(
                ok=False,
                detail=f"Timeout budget exhausted (time_remaining={ctx.time_remaining:.1f}s) before pytest could run.",
            )

        sub_timeout = ctx.time_remaining if self.timeout is None else min(self.timeout, ctx.time_remaining)
        if sub_timeout <= 0:
            return VerificationResult(
                ok=False,
                detail=f"Subprocess timeout budget insufficient ({sub_timeout:.1f}s).",
            )

        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
        ]
        if self.target:
            cmd.append(self.target)
        if self.args:
            cmd.extend(self.args)

        env = _build_pytest_env()

        popen_kwargs: dict[str, Any] = {
            "cwd": ctx.project_root,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": env,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            return VerificationResult(ok=False, detail="Python executable not found to run pytest.")

        try:
            stdout_txt, stderr_txt = proc.communicate(timeout=sub_timeout)
            if proc.returncode == 0:
                summary = stdout_txt.strip()
                if len(summary) > 2000:
                    summary = summary[-2000:]
                return VerificationResult(ok=True, detail=summary or "pytest passed.")

            # Preserves failure diagnostics (both stdout and stderr)
            diagnostics = f"pytest failed with exit code {proc.returncode}:\n{stdout_txt}\n{stderr_txt}".strip()
            return VerificationResult(ok=False, detail=diagnostics)
        except subprocess.TimeoutExpired as e:
            _kill_process_tree(proc)
            try:
                post_out, post_err = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired as e2:
                post_out = e2.stdout
                post_err = e2.stderr
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass

            raw_out = post_out if post_out is not None else e.stdout
            raw_err = post_err if post_err is not None else e.stderr
            stdout_txt = (
                raw_out
                if isinstance(raw_out, str)
                else (raw_out.decode("utf-8", errors="replace") if raw_out else "")
            )
            stderr_txt = (
                raw_err
                if isinstance(raw_err, str)
                else (raw_err.decode("utf-8", errors="replace") if raw_err else "")
            )
            captured = f"{stdout_txt}\n{stderr_txt}".strip()
            detail = f"pytest timed out after {sub_timeout:.1f}s."
            if captured:
                detail += f"\nOutput before timeout:\n{captured}"
            return VerificationResult(ok=False, detail=detail)


class GitCommitVerifier:
    """
    Verifies that a git_commit step created a commit matching expectations.
    Optionally checks that the working tree is clean.
    Strictly read-only.
    """

    def __init__(self, spec: dict[str, Any]):
        if not isinstance(spec, dict):
            raise ValueError("git_commit: spec must be a dictionary.")

        allowed_keys = {"type", "message", "message_contains", "expect_clean"}
        extra = set(spec) - allowed_keys
        if extra:
            raise ValueError(f"git_commit: unexpected spec key(s) {sorted(extra)}.")

        message = spec.get("message")
        if message is not None and not isinstance(message, str):
            raise ValueError("git_commit: 'message' must be a string.")
        self.message: str | None = message

        message_contains = spec.get("message_contains")
        if message_contains is not None and not isinstance(message_contains, str):
            raise ValueError("git_commit: 'message_contains' must be a string.")
        self.message_contains: str | None = message_contains

        expect_clean = spec.get("expect_clean")
        if expect_clean is not None and not isinstance(expect_clean, bool):
            raise ValueError("git_commit: 'expect_clean' must be a boolean.")
        self.expect_clean: bool = bool(expect_clean) if expect_clean is not None else False

    def verify(self, step: ExecutionStep, ctx: VerificationContext) -> VerificationResult:
        if not (ctx.project_root / ".git").exists():
            return VerificationResult(
                ok=False,
                detail=f"Not a git repository: '{ctx.project_root}'.",
            )

        SecurityPolicy.resolve_safe_path(ctx.project_root)

        # Inspect latest commit using GitInspector._run
        out = GitInspector._run(["log", "-n1", "--format=%H%x00%s"], ctx.project_root)
        if out.startswith("ERROR:"):
            return VerificationResult(ok=False, detail=f"Git log failed: {out}")

        parts = out.split("\x00", 1)
        if len(parts) == 2:
            commit_hash, commit_subject = parts[0].strip(), parts[1].strip()
        else:
            commit_hash, commit_subject = out.strip(), ""

        expected_msg = self.message or self.message_contains or step.params.get("message")
        if self.message:
            if commit_subject != self.message:
                return VerificationResult(
                    ok=False,
                    detail=f"Commit message mismatch: expected '{self.message}', got '{commit_subject}' ({commit_hash[:8]}).",
                )
        elif self.message_contains:
            if self.message_contains not in commit_subject:
                return VerificationResult(
                    ok=False,
                    detail=f"Commit message missing substring '{self.message_contains}': got '{commit_subject}' ({commit_hash[:8]}).",
                )
        elif expected_msg and commit_subject != expected_msg and expected_msg not in commit_subject:
            return VerificationResult(
                ok=False,
                detail=f"Commit message mismatch: expected '{expected_msg}', got '{commit_subject}' ({commit_hash[:8]}).",
            )

        if self.expect_clean:
            status = GitInspector._run(["status", "--porcelain"], ctx.project_root)
            if status.startswith("ERROR:"):
                return VerificationResult(
                    ok=False,
                    detail=f"Git status failed: {status}",
                )
            if status:
                return VerificationResult(
                    ok=False,
                    detail=f"Commit {commit_hash[:8]} created, but working directory is dirty:\n{status}",
                )

        return VerificationResult(ok=True, detail=f"Commit verified: {commit_hash[:8]} - '{commit_subject}'")


VERIFIERS: dict[str, type] = {
    "file_content": FileContentVerifier,
    "pytest_target": PytestTargetVerifier,
    "git_commit": GitCommitVerifier,
}
