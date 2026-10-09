import json

import pytest

from jarvis.core.plan import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT_SECONDS,
    ExecutionPlan,
    ExecutionStep,
    PlanFormatError,
    PlanStatus,
    StepStatus,
)


def _plan(**kw):
    steps = kw.pop("steps", [ExecutionStep(tool="read_file", params={"path": "a.txt"})])
    return ExecutionPlan(task_id="TSK-AAAA0001", steps=steps, **kw)


def test_defaults_match_agreed_boundaries():
    plan = _plan()
    assert plan.max_steps == DEFAULT_MAX_STEPS == 5
    assert plan.timeout_seconds == DEFAULT_TIMEOUT_SECONDS == 120
    assert plan.status == PlanStatus.PENDING
    assert plan.current_step_index == 0
    assert plan.checkpoint_id is None
    assert plan.plan_id.startswith("PLN-")


def test_json_roundtrip_preserves_everything():
    plan = _plan(
        steps=[
            ExecutionStep(tool="write_file", params={"path": "x.txt", "content": "hi"},
                          verify={"type": "file_content", "path": "x.txt"}, description="write x"),
            ExecutionStep(tool="git_commit", params={"message": "m"}),
        ],
        max_steps=3, timeout_seconds=30,
    )
    plan.status = PlanStatus.BLOCKED
    plan.current_step_index = 1
    plan.checkpoint_id = "CHK-DEADBEEF"
    plan.blocked_reason = "boom"
    plan.steps[0].status = StepStatus.SUCCEEDED
    plan.steps[0].result = "ok"

    restored = ExecutionPlan.from_json(plan.to_json())
    assert restored == plan
    assert restored.steps[0].status is StepStatus.SUCCEEDED
    assert restored.status is PlanStatus.BLOCKED


def test_serialization_is_deterministic():
    plan = _plan()
    assert plan.to_json() == plan.to_json()
    assert list(json.loads(plan.to_json())) == sorted(json.loads(plan.to_json()))


@pytest.mark.parametrize("kwargs, message", [
    ({"max_steps": 0}, "max_steps"),
    ({"max_steps": True}, "max_steps"),
    ({"timeout_seconds": 0}, "timeout_seconds"),
    ({"timeout_seconds": -5}, "timeout_seconds"),
    ({"steps": []}, "at least one step"),
])
def test_validate_shape_rejects_bad_plans(kwargs, message):
    with pytest.raises(PlanFormatError, match=message):
        _plan(**kwargs).validate_shape()


def test_more_steps_than_max_steps_rejected():
    steps = [ExecutionStep(tool="read_file", params={"path": f"{i}.txt"}) for i in range(4)]
    with pytest.raises(PlanFormatError, match="max_steps is 3"):
        _plan(steps=steps, max_steps=3).validate_shape()


def test_bad_step_fields_rejected():
    with pytest.raises(PlanFormatError, match="tool"):
        _plan(steps=[ExecutionStep(tool="  ")]).validate_shape()
    with pytest.raises(PlanFormatError, match="params"):
        _plan(steps=[ExecutionStep(tool="read_file", params=["nope"])]).validate_shape()
    with pytest.raises(PlanFormatError, match="verify"):
        _plan(steps=[ExecutionStep(tool="read_file", verify={"no_type": 1})]).validate_shape()


@pytest.mark.parametrize("raw", ["not json", "[]", "null", "42"])
def test_from_json_rejects_non_plan_documents(raw):
    with pytest.raises(PlanFormatError):
        ExecutionPlan.from_json(raw)


def test_unknown_fields_are_rejected_not_ignored():
    good = json.loads(_plan().to_json())
    with pytest.raises(PlanFormatError, match="Unknown plan field"):
        ExecutionPlan.from_dict({**good, "surprise": 1})
    bad_step = json.loads(_plan().to_json())
    bad_step["steps"][0]["surprise"] = 1
    with pytest.raises(PlanFormatError, match="unknown field"):
        ExecutionPlan.from_dict(bad_step)


def test_invalid_enum_values_rejected():
    data = json.loads(_plan().to_json())
    data["status"] = "FLYING"
    with pytest.raises(PlanFormatError):
        ExecutionPlan.from_dict(data)
    data = json.loads(_plan().to_json())
    data["steps"][0]["status"] = "FLYING"
    with pytest.raises(PlanFormatError):
        ExecutionPlan.from_dict(data)


def test_step_index_out_of_range_rejected():
    data = json.loads(_plan().to_json())
    data["current_step_index"] = 9
    with pytest.raises(PlanFormatError, match="current_step_index"):
        ExecutionPlan.from_dict(data)


def test_user_authored_plan_cannot_set_engine_owned_fields():
    authored = {"task_id": "TSK-AAAA0001", "steps": [{"tool": "read_file", "params": {"path": "a"}}],
                "status": "COMPLETED"}
    with pytest.raises(PlanFormatError, match="engine-owned"):
        ExecutionPlan.from_dict(authored, require_state=False)


def test_user_authored_minimal_plan_loads_with_defaults():
    authored = {"task_id": "TSK-AAAA0001", "steps": [{"tool": "list_directory"}]}
    plan = ExecutionPlan.from_dict(authored, require_state=False)
    assert plan.max_steps == 5 and plan.timeout_seconds == 120
    assert plan.steps[0].params == {} and plan.steps[0].status is StepStatus.PENDING
