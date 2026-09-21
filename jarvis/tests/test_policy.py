import pytest

from jarvis.policy.rules import ActionTier, SecurityPolicy
from jarvis.policy.validator import PolicyValidator, PolicyViolation


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


def test_workspace_root_is_created():
    root = SecurityPolicy.get_workspace_root()
    assert root.exists()


def test_path_inside_workspace_is_safe():
    root = SecurityPolicy.get_workspace_root()
    target = root / "some_project" / "file.py"
    assert SecurityPolicy.is_path_safe(target)


def test_path_traversal_outside_workspace_is_unsafe():
    root = SecurityPolicy.get_workspace_root()
    traversal = root / ".." / ".." / "etc" / "passwd"
    assert not SecurityPolicy.is_path_safe(traversal)


# --- authorize_tool / authorize_provider: exception-based per ChatGPT+Gemini
# combined review. These now raise PolicyViolation on denial instead of
# returning False; a caller that forgets to check a return value can no
# longer accidentally proceed with a denied action, because there's no
# return value to forget to check -- an unhandled exception halts it.

def test_dangerous_tier_always_denied():
    with pytest.raises(PolicyViolation):
        PolicyValidator.authorize_tool("rm_rf", ActionTier.DANGEROUS)


def test_observe_tier_inside_workspace_allowed():
    root = SecurityPolicy.get_workspace_root()
    target = root / "project"
    PolicyValidator.authorize_tool("read_file", ActionTier.OBSERVE, target)  # must not raise


def test_observe_tier_outside_workspace_denied():
    with pytest.raises(PolicyViolation):
        PolicyValidator.authorize_tool("read_file", ActionTier.OBSERVE, "/etc/passwd")


def test_zero_spend_blocks_costly_provider():
    with pytest.raises(PolicyViolation):
        PolicyValidator.authorize_provider("some_paid_api", expects_cost=True)


def test_zero_cost_provider_allowed():
    PolicyValidator.authorize_provider("manual_clipboard", expects_cost=False)  # must not raise


# --- Windows drive-letter / UNC path rejection (item 4 of the combined
# review). Checked as an explicit string pattern in resolve_safe_path,
# specifically so this is meaningfully testable on any host OS, not just
# Windows -- see the comment in policy/rules.py for why.

@pytest.mark.parametrize("windows_path", [
    r"C:\Users\lone\secrets.txt",
    r"C:/Users/lone/secrets.txt",
    r"D:\projects\evil.py",
    r"\\fileserver\share\secrets.txt",
])
def test_windows_style_absolute_paths_rejected(windows_path):
    with pytest.raises(PermissionError):
        SecurityPolicy.resolve_safe_path(windows_path)
