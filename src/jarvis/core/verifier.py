"""
Verifiers for plan steps (M002, ADR-0005).

Targeted postcondition verifiers:
  - FileContentVerifier: verifies file existence and content.
  - PytestTargetVerifier: verifies test suites pass with detailed failure diagnostics.
  - GitCommitVerifier: verifies a git commit was created and matches expectations.

All verifiers are strictly read-only and must never mutate project files.
Types are imported directly from jarvis.core.plan (never redefined).
"""

from pathlib import Path
import subprocess
import sys
from typing import Any

from jarvis.core.plan import ExecutionStep, VerificationContext, VerificationResult
from jarvis.policy.rules import SecurityPolicy
from jarvis.tools.git import GitInspector


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
            if p.is_absolute() or ".." in p.parts:
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
            if p.is_absolute() or ".." in p.parts:
                raise PermissionError(
                    f"pytest_target: target '{raw_target}' attempts path traversal or is absolute."
                )
            self.target: str = raw_target
        else:
            self.target = ""

        args = spec.get("args")
        if args is not None:
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ValueError("pytest_target: 'args' must be a list of strings.")
            self.args: list[str] = args
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

        cmd = [sys.executable, "-m", "pytest"]
        if self.target:
            cmd.append(self.target)
        if self.args:
            cmd.extend(self.args)

        try:
            proc = subprocess.run(
                cmd,
                cwd=ctx.project_root,
                capture_output=True,
                text=True,
                timeout=sub_timeout,
            )
            if proc.returncode == 0:
                summary = proc.stdout.strip()
                if len(summary) > 2000:
                    summary = summary[-2000:]
                return VerificationResult(ok=True, detail=summary or "pytest passed.")

            # Preserves failure diagnostics (both stdout and stderr)
            diagnostics = f"pytest failed with exit code {proc.returncode}:\n{proc.stdout}\n{proc.stderr}".strip()
            return VerificationResult(ok=False, detail=diagnostics)
        except subprocess.TimeoutExpired as e:
            stdout_txt = e.stdout if isinstance(e.stdout, str) else (e.stdout.decode("utf-8", errors="replace") if e.stdout else "")
            stderr_txt = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode("utf-8", errors="replace") if e.stderr else "")
            captured = f"{stdout_txt}\n{stderr_txt}".strip()
            detail = f"pytest timed out after {sub_timeout:.1f}s."
            if captured:
                detail += f"\nOutput before timeout:\n{captured}"
            return VerificationResult(ok=False, detail=detail)
        except FileNotFoundError:
            return VerificationResult(ok=False, detail="Python executable not found to run pytest.")


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
            if status and not status.startswith("ERROR:"):
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
