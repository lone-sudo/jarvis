import os

import pytest

from jarvis.policy.rules import ActionTier, SecurityPolicy
from jarvis.policy.validator import PolicyValidator


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


def test_dangerous_tier_always_denied():
    assert not PolicyValidator.authorize_tool("rm_rf", ActionTier.DANGEROUS)


def test_observe_tier_inside_workspace_allowed():
    root = SecurityPolicy.get_workspace_root()
    target = root / "project"
    assert PolicyValidator.authorize_tool("read_file", ActionTier.OBSERVE, target)


def test_zero_spend_blocks_costly_provider():
    assert not PolicyValidator.authorize_provider("some_paid_api", expects_cost=True)


def test_zero_cost_provider_allowed():
    assert PolicyValidator.authorize_provider("manual_clipboard", expects_cost=False)
