"""
Jarvis Policy Rules — the non-negotiable boundaries.

Everything Jarvis is allowed to do is defined here. Tools and providers
must ask this module for permission before acting; nothing bypasses it.
"""

import ntpath
import os
from enum import Enum
from pathlib import Path


class ActionTier(Enum):
    OBSERVE = 0      # Read-only: git status, read file, list directory
    SAFE_WRITE = 1   # Local mutations: git commit, write file (explicit confirmation)
    DANGEROUS = 2     # Always blocked in Milestone 001: sudo, destructive ops, out-of-scope paths


class SecurityPolicy:
    # Hard $0 automatic-spending policy. This is a constant, not a config
    # value, on purpose — changing it should require editing code, not a
    # settings file, so it can never be silently overridden.
    MAX_AUTO_SPEND_USD = 0.00

    @classmethod
    def get_workspace_root(cls) -> Path:
        """
        The one directory tree Jarvis is allowed to touch.

        Defaults to ~/jarvis-workspace rather than ~/projects, since a bare
        ~/projects is a common, sometimes-existing folder name that could
        collide with unrelated content the user never intended to expose
        to Jarvis. Override with the JARVIS_WORKSPACE_ROOT environment
        variable once real projects are registered.
        """
        root = os.getenv("JARVIS_WORKSPACE_ROOT", str(Path.home() / "jarvis-workspace"))
        path = Path(root).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def resolve_safe_path(cls, relative_or_absolute: str | Path) -> Path:
        """
        The one canonical boundary check. Accepts either a path meant to
        be relative to the workspace root, or an absolute path someone
        has supplied directly (e.g. a --content-file argument) — both are
        resolved and checked identically. Raises PermissionError if the
        result falls outside the workspace root; never returns a path
        outside it.

        Every module that touches the filesystem (tools/fs.py,
        tools/git.py, memory/markdown.py, state/tracker.py's project
        registration, and any CLI argument that names a file) should
        route through this instead of concatenating paths itself, so the
        boundary logic exists in exactly one place.
        """
        root = cls.get_workspace_root()

        # build-log 0004 added a blanket regex here that rejected *every*
        # Windows-style absolute path outright, on any host OS. That was
        # a real bug, caught by Lone actually running the tests on the
        # ZBook: on real Windows, the workspace root itself and every
        # legitimate path inside it are Windows-style absolute paths, so
        # the blanket rule rejected valid, in-workspace paths right along
        # with genuine attacks -- including pytest's own tmp_path fixture.
        #
        # Corrected logic: a Windows-style absolute path (checked via
        # ntpath.isabs, which recognizes "C:\...", "C:/...", and UNC
        # "\\server\share" regardless of host OS) is only rejected
        # up front when the *native* pathlib on this host does NOT also
        # recognize it as absolute -- i.e. we're being asked to resolve
        # a Windows path on a non-Windows machine (this Linux sandbox,
        # for instance), where it could never legitimately resolve inside
        # a POSIX workspace root anyway. On real Windows, native_absolute
        # is True for the same string, so this early check is skipped and
        # the path falls through to the normal resolve-and-contain check
        # below -- which is what correctly allows legitimate Windows paths
        # inside the workspace and correctly rejects ones outside it
        # (e.g. C:\Windows\System32\...), exactly as it did before 0004.
        raw = str(relative_or_absolute)
        windows_style_absolute = ntpath.isabs(raw)
        native_absolute = Path(raw).is_absolute()
        if windows_style_absolute and not native_absolute:
            raise PermissionError(
                f"Path '{raw}' is an absolute Windows-style path, which cannot resolve "
                f"inside this (non-Windows) workspace root ({root})."
            )

        candidate = Path(relative_or_absolute)
        target = candidate if candidate.is_absolute() else root / candidate
        try:
            target = target.resolve()
        except (OSError, RuntimeError) as e:
            raise PermissionError(f"Could not resolve path '{relative_or_absolute}': {e}")
        if not (target == root or root in target.parents):
            raise PermissionError(
                f"Path '{relative_or_absolute}' resolves to '{target}', outside the workspace root ({root})."
            )
        return target

    @classmethod
    def is_path_safe(cls, target_path: str | Path) -> bool:
        """
        Boolean convenience wrapper around resolve_safe_path, for callers
        that want a yes/no rather than a caught exception.
        """
        try:
            cls.resolve_safe_path(target_path)
            return True
        except PermissionError:
            return False
