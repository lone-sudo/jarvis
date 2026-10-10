"""
ExecutionEngine: the deterministic execution substrate (M002, ADR-0005).

    Execution Plan -> per-step policy check -> tool invocation
                   -> targeted verification -> durable state

Properties this module is responsible for, each covered by tests:

  * Approving a plan is not authorizing its steps. Every step is
    re-authorized at execution time (tool registered? tier allowed?
    path inside the workspace?), and write/commit tools still ask their
    own interactive [y/N] with a diff, per step.
  * Nothing is guessed. A plan that is malformed, names an unknown tool,
    has a mutating step with no verification, or needs a verifier that
    isn't installed is rejected in pre-flight, BEFORE any step runs.
  * Failure never destroys evidence. On any failure the engine writes
    diagnostics to the `executions` table, marks the step FAILED, the
    plan and task BLOCKED, prints what happened, and stops. It does not
    delete, revert, stash or branch. The working tree stays as the
    failed step left it; `jarvis rollback` is a separate, human-run,
    confirmed action.
  * A BLOCKED (or interrupted) plan can be re-run: it resumes at the
    failed step, re-authorized and re-confirmed like any other.
"""

import importlib
import json
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from jarvis.core.checkpoint import CheckpointError, create_checkpoint
from jarvis.core.plan_commit import commit_plan_paths
from jarvis.core.plan import (
    ExecutionPlan,
    ExecutionStep,
    PlanFormatError,
    PlanStatus,
    StepStatus,
    VerificationContext,
    Verifier,
)
from jarvis.policy.rules import ActionTier, SecurityPolicy
from jarvis.policy.validator import PolicyValidator, PolicyViolation
from jarvis.state.plan_store import PlanStore, get_task_row
from jarvis.tools.fs import FileSystemInspector
from jarvis.tools.git import GitInspector

VerifierFactory = Callable[[dict], Verifier]

MAX_DIFF_CHARS = 50_000
MAX_TEXT_FIELD_CHARS = 20_000
MAX_PARAM_PREVIEW_CHARS = 500


class EngineError(Exception):
    """The engine refused to start (nothing ran, nothing changed, plan state untouched)."""


class PreflightError(Exception):
    """A plan failed pre-flight (nothing ran)."""


# ---- tool registry ---------------------------------------------------

def _ws_rel(project_root: str, rel: str) -> str:
    """Project-relative path -> workspace-relative path (the form the tools take)."""
    return (Path(project_root) / rel).as_posix()


@dataclass(frozen=True)
class ToolSpec:
    tier: ActionTier
    action_type: str                      # value for the executions.action_type column
    path_param: str | None                # which param names a project-relative path, if any
    required: tuple[str, ...]
    optional: tuple[str, ...]
    invoke: Callable[[str, dict, "bool | None"], str]   # (project_root, params, auto_confirm) -> result


def _list_directory(project_root: str, params: dict, _auto: "bool | None") -> str:
    return "\n".join(FileSystemInspector.list_directory(_ws_rel(project_root, params.get("path", ""))))


# Deny by default: a plan step can only name a tool that appears here.
# Network tools are deliberately absent (no network steps in M002).
TOOL_REGISTRY: dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        ActionTier.OBSERVE, "READ_ONLY", "path", ("path",), (),
        lambda root, p, _a: FileSystemInspector.read_file(_ws_rel(root, p["path"])),
    ),
    "list_directory": ToolSpec(
        ActionTier.OBSERVE, "READ_ONLY", "path", (), ("path",), _list_directory,
    ),
    "write_file": ToolSpec(
        ActionTier.SAFE_WRITE, "SAFE_WRITE", "path", ("path", "content"), (),
        lambda root, p, auto: FileSystemInspector.write_file(_ws_rel(root, p["path"]), p["content"], auto_confirm=auto),
    ),
    "git_commit": ToolSpec(
        ActionTier.SAFE_WRITE, "SAFE_WRITE", None, ("message",), (),
        # NOT GitInspector.commit_changes: that runs `git add -A` and would sweep unrelated
        # untracked files into the commit, which a rollback would then delete (ADR-0005 h).
        # `_paths` is injected by the engine (never accepted from a plan; see validate_plan).
        lambda root, p, auto: commit_plan_paths(root, p["message"], p.get("_paths", []), auto),
    ),
}


def validate_plan(plan: ExecutionPlan) -> None:
    """
    Static, side-effect-free checks used both when a plan is created and
    again in pre-flight. Raises PlanFormatError.
    """
    plan.validate_shape()
    for i, step in enumerate(plan.steps, start=1):
        spec = TOOL_REGISTRY.get(step.tool)
        if spec is None:
            raise PlanFormatError(
                f"step {i}: unknown tool '{step.tool}'. Plan tools: {sorted(TOOL_REGISTRY)}."
            )
        missing = [k for k in spec.required if k not in step.params]
        if missing:
            raise PlanFormatError(f"step {i} ({step.tool}): missing param(s) {missing}.")
        extra = set(step.params) - set(spec.required) - set(spec.optional)
        if extra:
            raise PlanFormatError(f"step {i} ({step.tool}): unexpected param(s) {sorted(extra)}.")
        for key, value in step.params.items():
            if not isinstance(value, str):
                raise PlanFormatError(f"step {i} ({step.tool}): param '{key}' must be a string.")
        if step.tool == "git_commit" and not step.params["message"].strip():
            raise PlanFormatError(f"step {i} (git_commit): 'message' must not be empty.")
        if step.tool == "git_commit" and not any(t.tool == "write_file" for t in plan.steps[: i - 1]):
            raise PlanFormatError(
                f"step {i} (git_commit): a plan's commit only includes files written by earlier "
                f"write_file steps of the same plan, and none come before this step."
            )
        if spec.path_param in step.params and not step.params[spec.path_param].strip() \
                and step.tool != "list_directory":
            raise PlanFormatError(f"step {i} ({step.tool}): '{spec.path_param}' must not be empty.")
        if spec.tier == ActionTier.SAFE_WRITE and step.verify is None:
            raise PlanFormatError(
                f"step {i} ({step.tool}) changes state, so it needs a 'verify' spec "
                f"(its intended postcondition). Unverified mutations are not accepted."
            )


def load_default_verifiers() -> dict[str, VerifierFactory]:
    """
    Picks up `VERIFIERS` (type name -> factory(spec) -> Verifier) from
    jarvis.core.verifier when that module exists. Absent module means no
    verifiers, which is a normal state (read-only plans still run).
    Any OTHER import problem inside the module is a real bug and propagates.
    """
    try:
        module = importlib.import_module("jarvis.core.verifier")
    except ModuleNotFoundError as e:
        if e.name == "jarvis.core.verifier":
            return {}
        raise
    return dict(getattr(module, "VERIFIERS", {}))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def _param_preview(params: dict) -> dict:
    return {
        k: (v if not isinstance(v, str) or len(v) <= MAX_PARAM_PREVIEW_CHARS
            else v[:MAX_PARAM_PREVIEW_CHARS] + f"...[{len(v)} chars]")
        for k, v in params.items()
    }


def _classify(result: str) -> str:
    """OK | DECLINED | POLICY | TOOL_ERROR, from the tools' 'ERROR: ...' string convention."""
    if not result.startswith("ERROR:"):
        return "OK"
    if "(declined by user)" in result:
        return "DECLINED"
    if "denied by policy" in result.lower():
        return "POLICY"
    return "TOOL_ERROR"


class ExecutionEngine:
    def __init__(
        self,
        tracker,
        store: PlanStore,
        *,
        verifier_factories: Mapping[str, VerifierFactory] | None = None,
        auto_confirm: bool | None = None,   # tests only: None means real interactive prompts
        time_fn: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = print,
    ):
        self.tracker = tracker
        self.store = store
        self.verifier_factories = dict(
            load_default_verifiers() if verifier_factories is None else verifier_factories
        )
        self.auto_confirm = auto_confirm
        self.time_fn = time_fn
        self.out = out

    # ---- public API ---------------------------------------------------

    def run(self, plan_id: str) -> ExecutionPlan:
        """
        Runs (or resumes) a plan and returns it. Inspect `.status`:
        COMPLETED, or BLOCKED with `.blocked_reason`. Raises EngineError
        only when it refuses to start at all.
        """
        plan = self.store.load(plan_id)
        if plan.status == PlanStatus.COMPLETED:
            raise EngineError(f"Plan {plan_id} is already COMPLETED.")
        task = get_task_row(self.tracker.db_path, plan.task_id)
        if task is None:
            raise EngineError(f"Plan {plan_id} refers to task {plan.task_id}, which does not exist.")
        if task["status"] in ("DONE", "ABORTED"):
            raise EngineError(f"Task {plan.task_id} is {task['status']}. Refusing to run a plan against it.")
        project_root = self.tracker.get_project_root(task["project_key"])
        if not project_root:
            raise EngineError(f"Project '{task['project_key']}' is not registered.")

        deadline = self.time_fn() + plan.timeout_seconds
        if plan.status == PlanStatus.RUNNING:
            self.out(f"Note: {plan_id} was left RUNNING by an interrupted earlier run. "
                     f"Resuming at step {plan.current_step_index + 1}, which will be re-authorized and re-confirmed.")

        # ---- pre-flight: nothing below this block runs a step -------------
        try:
            verifiers = self._preflight(plan)
        except (PlanFormatError, PreflightError) as e:
            return self._preserve_failure(plan, None, project_root, "PREFLIGHT", str(e))

        if plan.checkpoint_id is None:
            needs_rollback_point = any(
                TOOL_REGISTRY[s.tool].tier == ActionTier.SAFE_WRITE
                for s in plan.steps[plan.current_step_index:]
            )
            try:
                plan.checkpoint_id = create_checkpoint(
                    self.tracker, plan.task_id, project_root, notes=f"pre-flight for {plan.plan_id}"
                )
            except CheckpointError as e:
                if needs_rollback_point:
                    return self._preserve_failure(
                        plan, None, project_root, "PREFLIGHT",
                        f"A plan that changes state needs a rollback point first. {e}",
                    )
                self.out(f"Note: no checkpoint taken (read-only plan): {e}")

        plan.status = PlanStatus.RUNNING
        plan.blocked_reason = ""
        self.store.save(plan)
        self._set_task_status(plan.task_id, "IN_PROGRESS")

        # ---- step loop -----------------------------------------------------
        total = len(plan.steps)
        workspace_root = SecurityPolicy.get_workspace_root()
        for i in range(plan.current_step_index, total):
            step = plan.steps[i]
            label = f"[{i + 1}/{total}] {step.tool}" + (f" - {step.description}" if step.description else "")

            if self.time_fn() >= deadline:
                return self._preserve_failure(
                    plan, i, project_root, "TIMEOUT",
                    f"timeout_seconds ({plan.timeout_seconds}) used up before step {i + 1} could start. "
                    f"Time spent at [y/N] prompts counts. Re-run to continue from this step.",
                )

            # 1. per-step policy re-authorization (never inherited from plan approval)
            try:
                spec = self._authorize_step(step, project_root)
            except PolicyViolation as e:
                self._log_step(plan, i, step, "REJECTED_BY_POLICY", str(e))
                return self._preserve_failure(plan, i, project_root, "POLICY", str(e))

            step.status = StepStatus.RUNNING
            plan.current_step_index = i
            self.store.save(plan)
            self.out(label)

            # 2. tool invocation
            try:
                result = spec.invoke(project_root, self._invoke_params(plan, i, step), self.auto_confirm)
                result = result if isinstance(result, str) else str(result)
            except Exception as e:  # noqa: BLE001 - any tool crash must be preserved, not propagated
                self._log_step(plan, i, step, "FAILURE", str(e))
                return self._preserve_failure(
                    plan, i, project_root, "EXCEPTION", f"{type(e).__name__}: {e}",
                    trace=traceback.format_exc(),
                )

            outcome = _classify(result)
            if outcome != "OK":
                status = "FAILURE" if outcome == "TOOL_ERROR" else "REJECTED_BY_POLICY"
                self._log_step(plan, i, step, status, result)
                return self._preserve_failure(
                    plan, i, project_root, outcome, result.removeprefix("ERROR:").strip(), tool_output=result,
                )
            self._log_step(plan, i, step, "SUCCESS", result)

            # 3. targeted verification of this step's own postcondition
            verifier = verifiers[i]
            if verifier is not None:
                vtype = step.verify["type"]
                ctx = VerificationContext(
                    project_root=workspace_root / project_root,
                    workspace_root=workspace_root,
                    time_remaining=max(0.0, deadline - self.time_fn()),
                )
                try:
                    vres = verifier.verify(step, ctx)
                except Exception as e:  # noqa: BLE001
                    self.tracker.log_execution(
                        plan.task_id, f"verify:{vtype}", "READ_ONLY", "FAILURE",
                        input_payload=self._payload(plan, i, step), output_payload=str(e),
                    )
                    return self._preserve_failure(
                        plan, i, project_root, "VERIFIER_ERROR",
                        f"verifier '{vtype}' crashed: {type(e).__name__}: {e}",
                        tool_output=result, trace=traceback.format_exc(),
                    )
                self.tracker.log_execution(
                    plan.task_id, f"verify:{vtype}", "READ_ONLY", "SUCCESS" if vres.ok else "FAILURE",
                    input_payload=self._payload(plan, i, step), output_payload=_clip(vres.detail, 2000),
                )
                if not vres.ok:
                    return self._preserve_failure(
                        plan, i, project_root, "VERIFICATION",
                        f"step {i + 1} ran, but its postcondition ('{vtype}') was not met.",
                        tool_output=result, verifier_detail=vres.detail,
                    )
                self.out(f"    verified ({vtype})")

            step.status = StepStatus.SUCCEEDED
            step.result = _clip(result, 2000)
            step.error = ""
            plan.current_step_index = i + 1
            self.store.save(plan)

        plan.status = PlanStatus.COMPLETED
        plan.blocked_reason = ""
        self.store.save(plan)
        self.out(f"Plan {plan.plan_id} COMPLETED ({total}/{total} steps).")
        return plan

    # ---- pre-flight ---------------------------------------------------

    def _preflight(self, plan: ExecutionPlan) -> list[Verifier | None]:
        validate_plan(plan)
        verifiers: list[Verifier | None] = [None] * len(plan.steps)
        for i in range(plan.current_step_index, len(plan.steps)):
            spec = plan.steps[i].verify
            if spec is None:
                continue
            factory = self.verifier_factories.get(spec["type"])
            if factory is None:
                raise PreflightError(
                    f"step {i + 1}: no verifier is installed for type '{spec['type']}' "
                    f"(available: {sorted(self.verifier_factories) or 'none'}). No step was run."
                )
            try:
                verifiers[i] = factory(spec)
            except Exception as e:  # noqa: BLE001
                raise PreflightError(
                    f"step {i + 1}: verifier '{spec['type']}' rejected its spec: {type(e).__name__}: {e}"
                ) from e
        return verifiers

    @staticmethod
    def _invoke_params(plan: ExecutionPlan, i: int, step: ExecutionStep) -> dict:
        """
        What the tool actually receives. For git_commit the engine adds the exact files this
        plan has written so far (successful earlier write_file steps), so the commit can never
        include anything else. Always built here, never taken from the plan, and derived from
        persisted step statuses so a resumed plan commits the same set.
        """
        params = dict(step.params)
        if step.tool == "git_commit":
            written: list[str] = []
            for prior in plan.steps[:i]:
                if prior.tool == "write_file" and prior.status is StepStatus.SUCCEEDED:
                    path = prior.params.get("path")
                    if isinstance(path, str) and path not in written:
                        written.append(path)
            params["_paths"] = written
        return params

    # ---- per-step policy ------------------------------------------------

    def _authorize_step(self, step: ExecutionStep, project_root: str) -> ToolSpec:
        spec = TOOL_REGISTRY.get(step.tool)
        if spec is None:
            raise PolicyViolation(f"Tool '{step.tool}' is not a registered plan tool (deny by default).")
        workspace_root = SecurityPolicy.get_workspace_root()
        raw = step.params.get(spec.path_param) if spec.path_param else None
        if raw is not None and not isinstance(raw, str):
            raise PolicyViolation(f"'{step.tool}': '{spec.path_param}' must be a string.")
        target = workspace_root / (_ws_rel(project_root, raw) if raw is not None else project_root)
        PolicyValidator.authorize_tool(step.tool, spec.tier, target)

        if spec.tier == ActionTier.SAFE_WRITE:
            # Stricter than workspace containment, on purpose: a checkpoint only
            # protects the task's own project repository, so a mutation anywhere
            # else in the workspace would be something `jarvis rollback` cannot undo.
            project_abs = (workspace_root / project_root).resolve()
            resolved = target.resolve()
            if resolved != project_abs and project_abs not in resolved.parents:
                raise PolicyViolation(
                    f"'{step.tool}' targets '{resolved}', outside the task's project ({project_abs}). "
                    f"Mutating steps are confined to the project because rollback only covers it."
                )
        return spec

    # ---- logging & failure preservation -----------------------------------

    @staticmethod
    def _payload(plan: ExecutionPlan, i: int, step: ExecutionStep) -> str:
        return json.dumps(
            {"plan_id": plan.plan_id, "step": i + 1, "tool": step.tool, "params": _param_preview(step.params)},
            sort_keys=True,
        )

    def _log_step(self, plan, i, step, status, output):
        spec = TOOL_REGISTRY.get(step.tool)  # None for an unregistered tool, which was denied
        self.tracker.log_execution(
            plan.task_id, step.tool, spec.action_type if spec else "READ_ONLY", status,
            input_payload=self._payload(plan, i, step), output_payload=_clip(output, 2000),
        )

    def _set_task_status(self, task_id: str, status: str) -> None:
        try:
            self.tracker.update_task_status(task_id, status)
        except ValueError as e:  # an illegal transition must not mask or replace the real outcome
            self.out(f"Warning: could not set task {task_id} to {status}: {e}")

    def _git_evidence(self, project_root: str) -> tuple[str, str]:
        target, error = GitInspector._authorized_target(
            "git_diagnostics", ActionTier.OBSERVE, project_root, require_git_repo=True
        )
        if error:
            return f"(unavailable: {error})", ""
        return (
            GitInspector._run(["status", "--short"], target) or "(clean)",
            _clip(GitInspector._run(["diff"], target), MAX_DIFF_CHARS),
        )

    def _preserve_failure(
        self,
        plan: ExecutionPlan,
        step_index: int | None,
        project_root: str,
        kind: str,
        message: str,
        *,
        tool_output: str = "",
        verifier_detail: str = "",
        trace: str = "",
    ) -> ExecutionPlan:
        """
        The only place a failure is handled. Read-only with respect to the
        project: it gathers evidence, records it, blocks the plan and task,
        tells the human, and returns. It never deletes, reverts, stashes
        or branches anything.
        """
        step = plan.steps[step_index] if step_index is not None else None
        git_status, git_diff = self._git_evidence(project_root)

        diagnostics = {
            "plan_id": plan.plan_id,
            "task_id": plan.task_id,
            "step_index": step_index,
            "step_number": None if step_index is None else step_index + 1,
            "tool": step.tool if step else None,
            "params": _param_preview(step.params) if step else None,
            "kind": kind,
            "message": message,
            "tool_output": _clip(tool_output, MAX_TEXT_FIELD_CHARS),
            "verifier_detail": _clip(verifier_detail, MAX_TEXT_FIELD_CHARS),
            "traceback": _clip(trace, MAX_TEXT_FIELD_CHARS),
            "checkpoint_id": plan.checkpoint_id,
            "git_status": git_status,
            "git_diff": git_diff,
        }
        self.tracker.log_execution(
            plan.task_id, "engine:failure", "READ_ONLY",
            "REJECTED_BY_POLICY" if kind in ("POLICY", "DECLINED") else "FAILURE",
            input_payload=json.dumps({"plan_id": plan.plan_id, "step_index": step_index}, sort_keys=True),
            output_payload=json.dumps(diagnostics, indent=2),
        )

        if step is not None:
            plan.current_step_index = step_index
            if kind != "TIMEOUT":  # on a timeout the step never started, so it is not 'failed'
                step.status = StepStatus.FAILED
                step.error = _clip(message, 2000)
        plan.status = PlanStatus.BLOCKED
        plan.blocked_reason = f"{kind}: {message}"
        self.store.save(plan)
        self._set_task_status(plan.task_id, "BLOCKED")

        self._print_diagnostics(plan, diagnostics)
        return plan

    def _print_diagnostics(self, plan: ExecutionPlan, d: dict) -> None:
        where = "before any step ran" if d["step_number"] is None else \
            f"at step {d['step_number']}/{len(plan.steps)} ({d['tool']})"
        self.out(f"\n=== Plan {plan.plan_id} BLOCKED {where} ===")
        self.out(f"Reason ({d['kind']}): {d['message']}")
        if d["verifier_detail"]:
            self.out("Verifier output:")
            self.out(_clip(d["verifier_detail"], 2000))
        if d["traceback"]:
            self.out("Traceback:")
            self.out(_clip(d["traceback"], 2000))
        self.out("Working tree status (left exactly as the failed step left it):")
        self.out(d["git_status"])
        self.out("Nothing was rolled back or deleted. Full evidence is saved to the executions log.")
        self.out(f"  Inspect:   jarvis plan-show {plan.plan_id}")
        self.out(f"  Retry:     jarvis plan-run {plan.plan_id}   (resumes at the failed step)")
        if plan.checkpoint_id:
            self.out(f"  Roll back: jarvis rollback --checkpoint {plan.checkpoint_id}   (previews, then asks)")
