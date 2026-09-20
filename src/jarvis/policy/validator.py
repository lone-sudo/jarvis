from pathlib import Path

from .rules import ActionTier, SecurityPolicy


class PolicyViolation(Exception):
    """Raised when a tool or provider call is denied by policy."""


class PolicyValidator:
    @staticmethod
    def authorize_tool(tool_name: str, tier: ActionTier, target_path: str | Path | None = None) -> bool:
        if tier == ActionTier.DANGEROUS:
            print(f"[POLICY BLOCK] '{tool_name}' requested a DANGEROUS-tier action. Denied.")
            return False

        if target_path is not None and not SecurityPolicy.is_path_safe(target_path):
            print(f"[POLICY BLOCK] '{tool_name}' tried to access '{target_path}', outside the workspace. Denied.")
            return False

        return True

    @staticmethod
    def authorize_provider(provider_name: str, expects_cost: bool = False) -> bool:
        if expects_cost and SecurityPolicy.MAX_AUTO_SPEND_USD == 0:
            print(f"[POLICY BLOCK] '{provider_name}' would incur cost; $0 auto-spend policy denies this.")
            return False
        return True
