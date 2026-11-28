"""Local First task router (P2S-LOCAL-FIRST-EXECUTION-001 §4/§5). Pure: no I/O, no clock."""
import pytest

from agent_runtime.routing import (
    Executor,
    Policy,
    Proof,
    Resources,
    RoutingError,
    Task,
    resolve_policy,
    route,
)

BUILD = "build_room"


def det(**kw):
    return Executor("recipe:build_room", "deterministic", recipes=frozenset({BUILD}), **kw)


def local(eid, rate=0.9, runs=10, tools=("build_room",), max_c=5, vram=8000, available=True, task_type=BUILD):
    proofs = {task_type: Proof(success_rate=rate, runs=runs, source="bench:C4")} if rate is not None else {}
    return Executor(eid, "local", model=eid, tools=frozenset(tools), max_complexity=max_c,
                    vram_mib=vram, available=available, proofs=proofs)


def cloud(eid="claude", **kw):
    return Executor(eid, "cloud", model=eid, tools=frozenset({"build_room", "render_room"}),
                    max_complexity=9, **kw)


def task(**kw):
    base = {"task_id": "t1", "task_type": BUILD, "complexity": 3, "tools": frozenset({"build_room"}), "risk": "LOW"}
    base.update(kw)
    return Task(**base)


RES = Resources(free_vram_mib=16000)
LF = Policy(strategy="LOCAL_FIRST", cloud_authorized=True, cloud_token_budget=10_000, auto_escalation=True)


def test_deterministic_automation_wins_when_task_needs_no_llm():
    d = route(task(), [cloud(), local("llama3"), det()], LF, RES)
    assert d.chosen.executor_id == "recipe:build_room"
    assert d.chosen.tier == "DETERMINISTIC"


def test_proven_local_beats_cloud_under_local_first():
    d = route(task(), [cloud(), local("llama3")], LF, RES)
    assert d.chosen.executor_id == "llama3"
    assert d.chosen.tier == "LOCAL_PROVEN"


def test_installed_model_without_proof_is_never_chosen():
    d = route(task(), [local("qwen", rate=None)], Policy(strategy="LOCAL_ONLY"), RES)
    assert d.chosen is None
    assert d.rejected["qwen"] == "NO_PROVEN_CAPABILITY"


def test_best_proven_local_first_then_other_compatible_local():
    d = route(task(), [local("deepseek", rate=0.7), local("llama3", rate=0.95)], LF, RES)
    ids = [c.executor_id for c in d.candidates]
    assert ids[:2] == ["llama3", "deepseek"]
    assert [c.tier for c in d.candidates[:2]] == ["LOCAL_PROVEN", "LOCAL_COMPATIBLE"]


def test_proof_below_quality_floor_is_rejected():
    d = route(task(), [local("weak", rate=0.2)], Policy(strategy="LOCAL_ONLY", min_success_rate=0.5), RES)
    assert d.chosen is None and d.rejected["weak"] == "PROOF_BELOW_QUALITY_FLOOR"


@pytest.mark.parametrize("ex,reason", [
    (local("x", available=False), "UNAVAILABLE"),
    (local("x", tools=("render_room",)), "MISSING_TOOLS"),
    (local("x", max_c=2), "COMPLEXITY_ABOVE_PROVEN"),
    (local("x", vram=32000), "INSUFFICIENT_RESOURCES"),
])
def test_ineligible_local_is_rejected_with_reason(ex, reason):
    d = route(task(), [ex], Policy(strategy="LOCAL_ONLY"), RES)
    assert d.chosen is None and d.rejected["x"] == reason


def test_local_only_never_lists_cloud():
    d = route(task(), [cloud(), local("x", rate=None)], Policy(strategy="LOCAL_ONLY", cloud_authorized=True,
                                                               cloud_token_budget=10**6), RES)
    assert d.chosen is None
    assert d.rejected["claude"] == "STRATEGY_EXCLUDES_CLOUD"


def test_no_hidden_cloud_fallback_without_authorization():
    p = Policy(strategy="LOCAL_FIRST", cloud_authorized=False, cloud_token_budget=10**6, auto_escalation=True)
    d = route(task(), [cloud()], p, RES)
    assert d.chosen is None and d.rejected["claude"] == "CLOUD_NOT_AUTHORIZED"


def test_cloud_needs_budget():
    p = Policy(strategy="LOCAL_FIRST", cloud_authorized=True, cloud_token_budget=0, auto_escalation=True)
    d = route(task(), [cloud()], p, RES)
    assert d.rejected["claude"] == "CLOUD_BUDGET_EXHAUSTED"


def test_local_first_with_auto_escalation_off_keeps_cloud_out_of_the_plan():
    p = Policy(strategy="LOCAL_FIRST", cloud_authorized=True, cloud_token_budget=10**6, auto_escalation=False)
    d = route(task(), [cloud(), local("llama3")], p, RES)
    assert [c.executor_id for c in d.candidates] == ["llama3"]
    assert d.rejected["claude"] == "AUTO_ESCALATION_DISABLED"


def test_local_first_puts_cloud_last_as_escalation():
    d = route(task(), [cloud(), local("llama3")], LF, RES)
    assert [(c.executor_id, c.tier) for c in d.candidates] == [("llama3", "LOCAL_PROVEN"), ("claude", "CLOUD")]


def test_cloud_first_puts_cloud_ahead_of_local():
    p = Policy(strategy="CLOUD_FIRST", cloud_authorized=True, cloud_token_budget=10**6)
    d = route(task(), [local("llama3"), cloud()], p, RES)
    assert d.chosen.executor_id == "claude"


def test_hybrid_sends_high_risk_to_cloud_and_low_risk_local():
    p = Policy(strategy="HYBRID", cloud_authorized=True, cloud_token_budget=10**6)
    assert route(task(risk="HIGH"), [local("llama3"), cloud()], p, RES).chosen.executor_id == "claude"
    assert route(task(risk="LOW"), [local("llama3"), cloud()], p, RES).chosen.executor_id == "llama3"


def test_manual_uses_only_the_chosen_executor():
    p = Policy(strategy="MANUAL", manual_executor="deepseek")
    d = route(task(), [local("llama3"), local("deepseek", rate=None)], p, RES)
    assert [c.executor_id for c in d.candidates] == ["deepseek"]
    assert d.chosen.tier == "MANUAL"


def test_manual_refuses_unknown_or_unavailable_executor():
    p = Policy(strategy="MANUAL", manual_executor="ghost")
    d = route(task(), [local("llama3")], p, RES)
    assert d.chosen is None and d.rejected["ghost"] == "MANUAL_EXECUTOR_NOT_FOUND"


def test_manual_cloud_still_needs_authorization():
    d = route(task(), [cloud()], Policy(strategy="MANUAL", manual_executor="claude"), RES)
    assert d.chosen is None and d.rejected["claude"] == "CLOUD_NOT_AUTHORIZED"


def test_user_preference_breaks_ties_between_equally_proven_locals():
    p = Policy(strategy="LOCAL_ONLY", preferred=("deepseek",))
    d = route(task(), [local("llama3"), local("deepseek")], p, RES)
    assert d.chosen.executor_id == "deepseek"


def test_decision_is_deterministic_regardless_of_input_order():
    exs = [local("a", rate=0.8), local("b", rate=0.8), cloud()]
    assert route(task(), exs, LF, RES).to_dict() == route(task(), list(reversed(exs)), LF, RES).to_dict()


def test_decision_serialises_for_the_event_store():
    d = route(task(), [local("llama3"), cloud()], LF, RES).to_dict()
    assert d["schema"] == "routing_decision/1.0"
    assert d["chosen"]["executor_id"] == "llama3"
    assert d["policy"]["strategy"] == "LOCAL_FIRST"


def test_project_policy_overrides_global_field_by_field():
    g = Policy(strategy="LOCAL_FIRST", cloud_authorized=True, cloud_token_budget=5000)
    p = resolve_policy(g, {"strategy": "LOCAL_ONLY"})
    assert p.strategy == "LOCAL_ONLY" and p.cloud_token_budget == 5000


@pytest.mark.parametrize("bad", [{"strategy": "YOLO"}, {"cloud_token_budget": -1}, {"unknown": 1}])
def test_invalid_policy_is_rejected_not_coerced(bad):
    with pytest.raises(RoutingError):
        resolve_policy(Policy(), bad)
