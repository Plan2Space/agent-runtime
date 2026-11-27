"""Local First task router (P2S-LOCAL-FIRST-EXECUTION-001 §4/§5).

Evolves the existing distribution path instead of adding a second orchestrator:
``EventStore.claim`` still decides *which job* runs; this module decides *which executor*
(deterministic recipe, local model or cloud model) runs it, and the chosen executor is driven
through the existing C8 ``AgentAdapter``. Pure: no I/O, no clock, no randomness.

Priority (Felipe, 2026-11-27):
  1. deterministic automation when the task needs no LLM;
  2. local model with proven capability for this task type;
  3. another compatible local model (also proven, ranked lower);
  4. cloud, only when the strategy allows it AND it is authorized AND budget remains.

A model is never chosen because it is installed: a local executor without a ``Proof`` for the
task type (from the Frente A matrix / benchmark C0–C9 / history) is rejected. There is no hidden
cloud fallback: every excluded executor is listed in ``rejected`` with a reason code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any

DECISION_SCHEMA = "routing_decision/1.0"
STRATEGIES = frozenset({"LOCAL_FIRST", "LOCAL_ONLY", "HYBRID", "CLOUD_FIRST", "MANUAL"})
KINDS = frozenset({"deterministic", "local", "cloud"})
RISKS = ("LOW", "MEDIUM", "HIGH")


class RoutingError(ValueError):
    """Invalid policy or input; rejected, never coerced."""


@dataclass(frozen=True)
class Proof:
    """Proven capability for one task type. ``source`` names the evidence (bench run, matrix row)."""

    success_rate: float
    runs: int
    source: str


@dataclass(frozen=True)
class Executor:
    executor_id: str
    kind: str
    model: str | None = None
    tools: frozenset = frozenset()
    recipes: frozenset = frozenset()  # deterministic only: task types it automates
    max_complexity: int = 9
    vram_mib: int = 0
    available: bool = True
    proofs: dict = field(default_factory=dict, hash=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise RoutingError(f"unknown executor kind {self.kind!r}")


@dataclass(frozen=True)
class Task:
    task_id: str
    task_type: str
    complexity: int
    tools: frozenset
    risk: str = "LOW"

    def __post_init__(self) -> None:
        if self.risk not in RISKS:
            raise RoutingError(f"unknown risk {self.risk!r}")


@dataclass(frozen=True)
class Resources:
    free_vram_mib: int


@dataclass(frozen=True)
class Policy:
    strategy: str = "LOCAL_FIRST"
    cloud_authorized: bool = False
    cloud_token_budget: int = 0
    auto_escalation: bool = False
    manual_executor: str | None = None
    min_success_rate: float = 0.5
    preferred: tuple = ()
    max_recent_failures: int | None = None  # None = recurring failures do not demote

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise RoutingError(f"unknown strategy {self.strategy!r}")
        if not isinstance(self.cloud_token_budget, int) or self.cloud_token_budget < 0:
            raise RoutingError("cloud_token_budget must be a non-negative int")
        if not 0 <= self.min_success_rate <= 1:
            raise RoutingError("min_success_rate must be in [0, 1]")
        if self.strategy == "MANUAL" and not self.manual_executor:
            raise RoutingError("MANUAL strategy needs manual_executor")


def resolve_policy(global_policy: Policy, project_overrides: dict[str, Any] | None) -> Policy:
    """Project settings override the global policy field by field; unknown keys are rejected."""
    overrides = dict(project_overrides or {})
    known = {f.name for f in fields(Policy)}
    unknown = set(overrides) - known
    if unknown:
        raise RoutingError(f"unknown policy keys {sorted(unknown)}")
    if "preferred" in overrides:
        overrides["preferred"] = tuple(overrides["preferred"])
    return replace(global_policy, **overrides)


@dataclass(frozen=True)
class Candidate:
    executor_id: str
    kind: str
    tier: str
    reason: str


@dataclass(frozen=True)
class RoutingDecision:
    task: Task
    policy: Policy
    candidates: tuple
    rejected: dict

    @property
    def chosen(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def to_dict(self) -> dict:
        t = asdict(self.task)
        t["tools"] = sorted(t["tools"])
        p = asdict(self.policy)
        p["preferred"] = list(p["preferred"])
        return {"schema": DECISION_SCHEMA, "task": t, "policy": p,
                "chosen": asdict(self.chosen) if self.chosen else None,
                "candidates": [asdict(c) for c in self.candidates],
                "rejected": dict(sorted(self.rejected.items()))}


def _local_reject(ex: Executor, task: Task, policy: Policy, res: Resources,
                  recent_failures: dict) -> str | None:
    if not ex.available:
        return "UNAVAILABLE"
    if not task.tools <= ex.tools:
        return "MISSING_TOOLS"
    proof = ex.proofs.get(task.task_type)
    if proof is None or proof.runs <= 0:
        return "NO_PROVEN_CAPABILITY"
    if proof.success_rate < policy.min_success_rate:
        return "PROOF_BELOW_QUALITY_FLOOR"
    if task.complexity > ex.max_complexity:
        return "COMPLEXITY_ABOVE_PROVEN"
    if ex.vram_mib > res.free_vram_mib:
        return "INSUFFICIENT_RESOURCES"
    if (policy.max_recent_failures is not None
            and recent_failures.get((ex.executor_id, task.task_type), 0) >= policy.max_recent_failures):
        return "RECURRING_FAILURE"
    return None


def _cloud_reject(ex: Executor, task: Task, policy: Policy) -> str | None:
    if policy.strategy == "LOCAL_ONLY":
        return "STRATEGY_EXCLUDES_CLOUD"
    if not policy.cloud_authorized:
        return "CLOUD_NOT_AUTHORIZED"
    if policy.cloud_token_budget <= 0:
        return "CLOUD_BUDGET_EXHAUSTED"
    if not ex.available:
        return "UNAVAILABLE"
    if not task.tools <= ex.tools:
        return "MISSING_TOOLS"
    return None


def _manual(task: Task, executors: list[Executor], policy: Policy, res: Resources) -> RoutingDecision:
    eid = policy.manual_executor
    ex = next((e for e in executors if e.executor_id == eid), None)
    if ex is None:
        return RoutingDecision(task, policy, (), {eid: "MANUAL_EXECUTOR_NOT_FOUND"})
    if ex.kind == "cloud":
        why = _cloud_reject(ex, task, policy)
    elif ex.kind == "deterministic":
        why = None if task.task_type in ex.recipes and ex.available else "UNAVAILABLE"
    else:  # the user chose it: proof is not required, but availability and tools are
        why = ("UNAVAILABLE" if not ex.available else
               "MISSING_TOOLS" if not task.tools <= ex.tools else None)
    if why:
        return RoutingDecision(task, policy, (), {eid: why})
    return RoutingDecision(task, policy, (Candidate(eid, ex.kind, "MANUAL", "user-selected executor"),), {})


def route(task: Task, executors: list[Executor], policy: Policy, resources: Resources, *,
          recent_failures: dict | None = None) -> RoutingDecision:
    """``recent_failures``: {(executor_id, task_type): count} from the PoC failure records."""
    if policy.strategy == "MANUAL":
        return _manual(task, executors, policy, resources)

    rejected: dict[str, str] = {}
    det: list[Candidate] = []
    locals_: list[tuple[Executor, Proof]] = []
    clouds: list[Candidate] = []
    pref = {eid: i for i, eid in enumerate(policy.preferred)}

    for ex in sorted(executors, key=lambda e: e.executor_id):
        if ex.kind == "deterministic":
            if task.task_type in ex.recipes and ex.available:
                det.append(Candidate(ex.executor_id, ex.kind, "DETERMINISTIC", "recipe automates task type"))
            continue
        if ex.kind == "local":
            why = _local_reject(ex, task, policy, resources, recent_failures or {})
            if why:
                rejected[ex.executor_id] = why
            else:
                locals_.append((ex, ex.proofs[task.task_type]))
            continue
        why = _cloud_reject(ex, task, policy)
        if why is None and policy.strategy == "LOCAL_FIRST" and not policy.auto_escalation:
            why = "AUTO_ESCALATION_DISABLED"
        if why:
            rejected[ex.executor_id] = why
        else:
            clouds.append(Candidate(ex.executor_id, ex.kind, "CLOUD", f"strategy {policy.strategy}"))

    locals_.sort(key=lambda lp: (-lp[1].success_rate, pref.get(lp[0].executor_id, len(pref)),
                                 -lp[1].runs, lp[0].executor_id))
    local_cands = [Candidate(ex.executor_id, ex.kind, "LOCAL_PROVEN" if i == 0 else "LOCAL_COMPATIBLE",
                             f"proof {p.source}: {p.success_rate:.2f} over {p.runs} runs")
                   for i, (ex, p) in enumerate(locals_)]

    cloud_ahead = policy.strategy == "CLOUD_FIRST" or (
        policy.strategy == "HYBRID" and (task.risk == "HIGH" or not local_cands))
    ordered = det + (clouds + local_cands if cloud_ahead else local_cands + clouds)
    return RoutingDecision(task, policy, tuple(ordered), rejected)
