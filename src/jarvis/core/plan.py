"""
Execution plan data models (M002, ADR-0005).

Pure data: stdlib dataclasses + json, no I/O, no policy, no tool calls.
The engine (core/engine.py) interprets these; the store
(state/plan_store.py) persists them. Keeping this module free of
behaviour is what lets a stored plan be inspected, diffed and
re-validated long after the process that created it is gone.
"""

import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

DEFAULT_MAX_STEPS = 5
DEFAULT_TIMEOUT_SECONDS = 120


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PlanStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


class PlanFormatError(ValueError):
    """A plan (supplied by a user or loaded from storage) is malformed."""


# ---- verifier contract ------------------------------------------------
# core/verifier.py (Antigravity's stream) implements Verifier; the engine
# consumes it. Defined here, beside the data it exchanges, so both
# streams import the same three names from one place.

@dataclass
class VerificationResult:
    """
    What a verifier returns. `detail` carries the evidence (test
    stdout/stderr, a content mismatch, the commit hash found) and is
    preserved verbatim in failure diagnostics, so make it informative.
    """
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class VerificationContext:
    """
    What the engine hands a verifier. `project_root` and `workspace_root`
    are absolute. `time_remaining` is what is left of the plan's
    timeout_seconds budget (never negative): a verifier that spawns a
    subprocess, such as a pytest run, should use it as that
    subprocess's timeout.
    """
    project_root: Path
    workspace_root: Path
    time_remaining: float


class Verifier(Protocol):
    """Checks the intended postcondition of one step. Must not mutate project files."""

    def verify(self, step: "ExecutionStep", ctx: VerificationContext) -> VerificationResult: ...


@dataclass
class ExecutionStep:
    tool: str
    params: dict = field(default_factory=dict)
    # Verification spec, e.g. {"type": "file_content", "path": "a.txt", ...}.
    # `type` selects a verifier; the rest is that verifier's own config.
    verify: dict | None = None
    description: str = ""
    status: StepStatus = StepStatus.PENDING
    result: str = ""   # truncated tool output of the last attempt
    error: str = ""    # short failure summary of the last attempt


@dataclass
class ExecutionPlan:
    task_id: str
    steps: list[ExecutionStep]
    plan_id: str = field(default_factory=lambda: f"PLN-{uuid.uuid4().hex[:8].upper()}")
    max_steps: int = DEFAULT_MAX_STEPS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    status: PlanStatus = PlanStatus.PENDING
    current_step_index: int = 0
    checkpoint_id: str | None = None
    blocked_reason: str = ""

    # ---- validation -------------------------------------------------

    def validate_shape(self) -> None:
        """
        Structural checks only. Whether a tool exists, or a verifier is
        available, is the engine's call (it owns the registries); this
        only guarantees the plan is well-formed and within its own
        declared boundaries.
        """
        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool) or self.max_steps < 1:
            raise PlanFormatError(f"max_steps must be an integer >= 1 (got {self.max_steps!r}).")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise PlanFormatError(f"timeout_seconds must be a number > 0 (got {self.timeout_seconds!r}).")
        if not self.steps:
            raise PlanFormatError("A plan needs at least one step.")
        if len(self.steps) > self.max_steps:
            raise PlanFormatError(
                f"Plan has {len(self.steps)} steps but max_steps is {self.max_steps}. "
                f"Split it into smaller plans or raise max_steps explicitly."
            )
        for i, step in enumerate(self.steps):
            where = f"step {i + 1}"
            if not isinstance(step.tool, str) or not step.tool.strip():
                raise PlanFormatError(f"{where}: 'tool' must be a non-empty string.")
            if not isinstance(step.params, dict):
                raise PlanFormatError(f"{where}: 'params' must be an object.")
            if step.verify is not None:
                if not isinstance(step.verify, dict) or not isinstance(step.verify.get("type"), str) \
                        or not step.verify["type"].strip():
                    raise PlanFormatError(f"{where}: 'verify' must be an object with a non-empty 'type'.")

    # ---- (de)serialization ------------------------------------------

    def to_json(self) -> str:
        # sort_keys keeps the stored form deterministic (stable diffs, stable tests).
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "ExecutionPlan":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlanFormatError(f"Plan JSON is not valid JSON: {e}") from e
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict, *, require_state: bool = True) -> "ExecutionPlan":
        """
        Strict reconstruction. Unknown keys and wrong types raise
        PlanFormatError rather than being ignored: a plan that is only
        half-understood must not be run.

        require_state=False accepts a user-authored plan file, which
        carries only task/steps/limits, not engine-owned fields like
        status or checkpoint_id.
        """
        if not isinstance(data, dict):
            raise PlanFormatError("Plan must be a JSON object.")

        plan_keys = {
            "plan_id", "task_id", "steps", "max_steps", "timeout_seconds",
            "status", "current_step_index", "checkpoint_id", "blocked_reason",
        }
        step_keys = {"tool", "params", "verify", "description", "status", "result", "error"}

        unknown = set(data) - plan_keys
        if unknown:
            raise PlanFormatError(f"Unknown plan field(s): {sorted(unknown)}.")
        if "task_id" not in data or not isinstance(data["task_id"], str) or not data["task_id"]:
            raise PlanFormatError("Plan needs a 'task_id' string.")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            raise PlanFormatError("Plan needs a 'steps' list.")

        steps = []
        for i, s in enumerate(raw_steps):
            if not isinstance(s, dict):
                raise PlanFormatError(f"step {i + 1} must be an object.")
            bad = set(s) - step_keys
            if bad:
                raise PlanFormatError(f"step {i + 1}: unknown field(s) {sorted(bad)}.")
            if "tool" not in s:
                raise PlanFormatError(f"step {i + 1}: missing 'tool'.")
            try:
                status = StepStatus(s.get("status", StepStatus.PENDING.value))
            except ValueError as e:
                raise PlanFormatError(f"step {i + 1}: {e}") from e
            steps.append(ExecutionStep(
                tool=s["tool"],
                params=s.get("params", {}),
                verify=s.get("verify"),
                description=s.get("description", ""),
                status=status,
                result=s.get("result", ""),
                error=s.get("error", ""),
            ))

        kwargs = {}
        for key in ("plan_id", "max_steps", "timeout_seconds", "current_step_index",
                    "checkpoint_id", "blocked_reason"):
            if key in data:
                kwargs[key] = data[key]
        if "status" in data:
            try:
                kwargs["status"] = PlanStatus(data["status"])
            except ValueError as e:
                raise PlanFormatError(str(e)) from e
        if not require_state:
            stray = {"status", "current_step_index", "checkpoint_id", "blocked_reason", "plan_id"} & set(data)
            if stray:
                raise PlanFormatError(
                    f"A new plan must not set engine-owned field(s): {sorted(stray)}."
                )

        plan = cls(task_id=data["task_id"], steps=steps, **kwargs)
        if not (0 <= plan.current_step_index <= len(plan.steps)):
            raise PlanFormatError(
                f"current_step_index {plan.current_step_index} is outside 0..{len(plan.steps)}."
            )
        plan.validate_shape()
        return plan

    # ---- small helpers ----------------------------------------------

    def is_terminal(self) -> bool:
        return self.status == PlanStatus.COMPLETED

    def reset_for_rerun(self) -> None:
        """
        Back to a never-run state. Used after a rollback: the work this
        plan had done no longer exists on disk, so resuming it from its
        old step index would skip steps whose effects are gone.
        """
        self.status = PlanStatus.PENDING
        self.current_step_index = 0
        self.checkpoint_id = None
        self.blocked_reason = ""
        for step in self.steps:
            step.status = StepStatus.PENDING
            step.result = ""
            step.error = ""
