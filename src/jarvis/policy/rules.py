"""
Jarvis Policy Rules — the non-negotiable boundaries.

Everything Jarvis is allowed to do is defined here. Tools and providers
must ask this module for permission before acting; nothing bypasses it.
"""

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
    def is_path_safe(cls, target_path: str | Path) -> bool:
        """
        True only if target_path resolves to somewhere inside the
        workspace root. Resolves symlinks and '..' segments before
        checking, so path traversal tricks don't slip through.
        """
        try:
            target = Path(target_path).resolve()
            workspace = cls.get_workspace_root()
            return target == workspace or workspace in target.parents
        except (OSError, RuntimeError):
            # Malformed path, permission error resolving it, etc. — treat
            # anything Jarvis can't confidently resolve as unsafe.
            return False
