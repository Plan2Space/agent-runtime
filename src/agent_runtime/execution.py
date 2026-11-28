"""Bounded execution with failure recovery over a ``RoutingDecision`` (§6) + EventStore facts (§8).

Walks the decision's candidates in order. Each attempt is recorded; each failure is classified
(PROTOCOL, SCHEMA, CONTEXT, TOOL, CAPABILITY) and handed back to the next attempt as structured
feedback. CAPABILITY moves to the next candidate at once; the other classes retry the same
executor up to ``attempts_per_executor``. ``max_total_attempts`` caps the whole run: no loops.

Cloud is only ever reached because the router put it in the candidate list (strategy, authorization,
budget and auto-escalation already checked); here the budget is enforced again with *measured*
tokens, and moving from a local executor to cloud emits ``EXECUTION_ESCALATED`` with the reason.
Success is attributed to the executor that produced it (``executed_by``), never to the first local
that was tried. Measured and estimated tokens are recorded in separate fields and never summed.

``run_attempt(candidate, feedback) -> AttemptOutcome`` is the seam to the real executor; the
PoC's local executor (C8 ``AgentAdapter`` / production queue) plugs in here.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .event_store import EventStore
from .routing import Candidate, RoutingDecision

FAILURE_CLASSES = ("PROTOCOL", "SCHEMA", "CONTEXT", "TOOL", "CAPABILITY")
_PATTERNS = (
    ("CONTEXT", re.compile(r"context (length|window)|too many tokens|truncat", re.I)),
    ("SCHEMA", re.compile(r"schema|missing field|unexpected field|validation failed", re.I)),
    ("PROTOCOL", re.compile(r"invalid json|malformed|tool call|unparseable|protocol", re.I)),
)


@dataclass(frozen=True)
class AttemptOutcome:
    status: str  # C8 typed result: SUCCEEDED | FAILED | TIMEOUT | TOOL_ERROR | REFUSED | CANCELLED
    error: str | None = None
    tokens_measured: int | None = None
    tokens_estimated: int | None = None


@dataclass(frozen=True)
class Recovery:
    attempts_per_executor: int = 2
    max_total_attempts: int = 6


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # SUCCEEDED | FAILED | REFUSED
    executed_by: str | None
    executed_by_kind: str | None
    attempts: int
    cloud_tokens_measured: int


def classify_failure(outcome: AttemptOutcome) -> str:
    """Map a failed attempt to one failure class. Order matters: TOOL_ERROR is the adapter's verdict."""
    if outcome.status == "TOOL_ERROR":
        return "TOOL"
    text = outcome.error or ""
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return "CAPABILITY"


class _Recorder:
    def __init__(self, store: EventStore, task_id: str, run_id: str):
        self.store, self.task_id, self.run_id, self.seq = store, task_id, run_id, 0

    def __call__(self, event_type: str, payload: dict) -> None:
        self.seq += 1
        self.store.publish(event_type, f"{self.task_id}:{self.run_id}:{self.seq}:{event_type}",
                           self.task_id, payload, correlation_id=self.run_id,
                           aggregate_type="task", producer="router")


def execute_with_recovery(decision: RoutingDecision, run_attempt: Callable[[Candidate, dict | None], AttemptOutcome],
                          *, store: EventStore, recovery: Recovery | None = None,
                          run_id: str | None = None) -> ExecutionResult:
    recovery = recovery or Recovery()
    rec = _Recorder(store, decision.task.task_id, run_id or uuid.uuid4().hex)
    rec("ROUTING_DECIDED", decision.to_dict())
    if decision.chosen is None:
        rec("ROUTING_REFUSED", {"rejected": dict(sorted(decision.rejected.items()))})
        return ExecutionResult("REFUSED", None, None, 0, 0)

    budget = decision.policy.cloud_token_budget
    cloud_used = 0
    total = 0
    local_failed = 0
    last: tuple[str, str] | None = None  # (executor_id, failure_class) of the last local failure

    for cand in decision.candidates:
        if cand.kind == "cloud":
            if cloud_used >= budget:
                rec("EXECUTION_ATTEMPT_FAILED", {"executor_id": cand.executor_id, "kind": cand.kind,
                                                 "failure_class": None, "error": "CLOUD_BUDGET_EXHAUSTED"})
                continue
            if last is not None:
                rec("EXECUTION_ESCALATED", {"from": last[0], "to": cand.executor_id,
                                            "reason": f"LOCAL_EXHAUSTED:{last[1]}"})
        feedback: dict | None = None
        for attempt in range(1, recovery.attempts_per_executor + 1):
            if total >= recovery.max_total_attempts:
                break
            if cand.kind == "cloud" and cloud_used >= budget:
                break
            total += 1
            rec("EXECUTION_ATTEMPT_STARTED", {"executor_id": cand.executor_id, "kind": cand.kind,
                                              "tier": cand.tier, "attempt": attempt, "feedback": feedback})
            out = run_attempt(cand, feedback)
            if out.tokens_measured is not None or out.tokens_estimated is not None:
                rec("TOKENS_CONSUMED", {"executor_id": cand.executor_id, "kind": cand.kind,
                                        "measured": out.tokens_measured, "estimated": out.tokens_estimated})
            if cand.kind == "cloud":
                # measured when available; otherwise the estimate still counts against the budget
                cloud_used += out.tokens_measured if out.tokens_measured is not None else (out.tokens_estimated or 0)
            if out.status == "SUCCEEDED":
                rec("EXECUTION_COMPLETED", {"executed_by": cand.executor_id, "executed_by_kind": cand.kind,
                                            "tier": cand.tier, "attempts": total,
                                            "local_attempts_failed": local_failed})
                return ExecutionResult("SUCCEEDED", cand.executor_id, cand.kind, total, cloud_used)
            cls = classify_failure(out)
            rec("EXECUTION_ATTEMPT_FAILED", {"executor_id": cand.executor_id, "kind": cand.kind,
                                             "attempt": attempt, "failure_class": cls,
                                             "status": out.status, "error": (out.error or "")[:500]})
            if cand.kind == "local":
                local_failed += 1
                last = (cand.executor_id, cls)
            feedback = {"failure_class": cls, "attempt": attempt, "executor_id": cand.executor_id,
                        "error": out.error}
            if cls == "CAPABILITY":
                break
        if total >= recovery.max_total_attempts:
            break

    rec("EXECUTION_FAILED", {"attempts": total, "local_attempts_failed": local_failed,
                             "last_failure": last[1] if last else None})
    return ExecutionResult("FAILED", None, None, total, cloud_used)
