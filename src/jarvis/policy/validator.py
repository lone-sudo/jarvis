from pathlib import Path

from .rules import ActionTier, SecurityPolicy


class PolicyViolation(Exception):
    """
    Raised whenever a tool or provider call is denied by policy — the one
    exception type callers need to catch, whether the denial came from a
    DANGEROUS tier, a path outside the workspace, or the $0 spend rule.

    This module previously returned a boolean and left it to each caller
    to check it and translate a False into a descriptive string. Per
    ChatGPT and Gemini's combined review, that's fragile by construction:
    a tool that forgets the `if not authorize_tool(...): return` check
    simply executes the denied action, silently. Exceptions make that
    failure mode structurally impossible — an unhandled PolicyViolation
    halts execution rather than falling through.
    """


class PolicyValidator:
    @staticmethod
    def authorize_tool(tool_name: str, tier: ActionTier, target_path: str | Path | None = None) -> None:
        """
        Raises PolicyViolation if the action is denied. Returns None (not
        a bool) on success -- the absence of an exception *is* the
        authorization. Callers that want a printable denial message
        should catch PolicyViolation, not check a return value.
        """
        if tier == ActionTier.DANGEROUS:
            raise PolicyViolation(f"'{tool_name}' requested a DANGEROUS-tier action. Denied.")

        if target_path is not None:
            try:
                SecurityPolicy.resolve_safe_path(target_path)
            except PermissionError as e:
                raise PolicyViolation(f"'{tool_name}' tried to access a path outside the workspace: {e}") from e

    @staticmethod
    def authorize_provider(provider_name: str, expects_cost: bool = False) -> None:
        if expects_cost and SecurityPolicy.MAX_AUTO_SPEND_USD == 0:
            raise PolicyViolation(f"'{provider_name}' would incur cost; $0 auto-spend policy denies this.")
