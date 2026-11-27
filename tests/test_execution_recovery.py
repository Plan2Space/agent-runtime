"""Failure recovery (§6) and routing events in the EventStore (§8). No real model is called."""
import json

import pytest

from agent_runtime.event_store import EventStore
from agent_runtime.execution import (
    AttemptOutcome,
    Recovery,
    classify_failure,
    execute_with_recovery,
)
from agent_runtime.routing import Executor, Policy, Proof, Resources, Task, route

BUILD = "build_room"
RES = Resources(free_vram_mib=16000)


def local(eid, rate=0.9):
    return Executor(eid, "local", model=eid, tools=frozenset({BUILD}),
                    proofs={BUILD: Proof(rate, 10, "bench:C4")})


CLOUD = Executor("claude", "cloud", model="claude", tools=frozenset({BUILD}))
TASK = Task("t1", BUILD, 3, frozenset({BUILD}))


def ok(tokens_measured=None, tokens_estimated=None):
    return AttemptOutcome("SUCCEEDED", tokens_measured=tokens_measured, tokens_estimated=tokens_estimated)


def fail(error, status="FAILED"):
    return AttemptOutcome(status, error=error)


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "events.db", clock=lambda: 1000.0)


def events(store):
    return [(e["event_type"], json.loads(e["payload"])) for e in store.events(job_id="t1")]


@pytest.mark.parametrize("outcome,kind", [
    (fail("invalid JSON in tool call"), "PROTOCOL"),
    (fail("schema validation failed: missing field 'room_id'"), "SCHEMA"),
    (fail("context length exceeded"), "CONTEXT"),
    (fail("tool build_room raised: SketchUp not running", status="TOOL_ERROR"), "TOOL"),
    (fail("tests failed: 3 of 5"), "CAPABILITY"),
    (AttemptOutcome("TIMEOUT", error=None), "CAPABILITY"),
])
def test_failure_is_classified(outcome, kind):
    assert classify_failure(outcome) == kind


def test_success_on_first_local_records_decision_attempt_and_completion(store):
    d = route(TASK, [local("llama3")], Policy(strategy="LOCAL_ONLY"), RES)
    r = execute_with_recovery(d, lambda c, fb: ok(), store=store)
    assert r.status == "SUCCEEDED" and r.executed_by == "llama3" and r.executed_by_kind == "local"
    assert [t for t, _ in events(store)] == ["ROUTING_DECIDED", "EXECUTION_ATTEMPT_STARTED",
                                              "EXECUTION_COMPLETED"]


def test_retry_gets_structured_feedback_and_is_bounded(store):
    seen = []

    def run(c, feedback):
        seen.append(feedback)
        return fail("schema validation failed: missing field 'room_id'")

    d = route(TASK, [local("llama3")], Policy(strategy="LOCAL_ONLY"), RES)
    r = execute_with_recovery(d, run, store=store, recovery=Recovery(attempts_per_executor=2, max_total_attempts=5))
    assert r.status == "FAILED" and len(seen) == 2
    assert seen[0] is None
    assert seen[1] == {"failure_class": "SCHEMA", "attempt": 1, "executor_id": "llama3",
                       "error": "schema validation failed: missing field 'room_id'"}


def test_total_attempts_cap_prevents_loops_across_many_executors(store):
    d = route(TASK, [local(f"m{i}") for i in range(10)], Policy(strategy="LOCAL_ONLY"), RES)
    calls = []
    r = execute_with_recovery(d, lambda c, fb: calls.append(c.executor_id) or fail("tests failed"),
                              store=store, recovery=Recovery(attempts_per_executor=3, max_total_attempts=4))
    assert len(calls) == 4 and r.status == "FAILED"


def test_capability_failure_moves_to_next_local_without_retrying_same_model(store):
    d = route(TASK, [local("llama3", 0.95), local("deepseek", 0.8)], Policy(strategy="LOCAL_ONLY"), RES)
    calls = []

    def run(c, fb):
        calls.append(c.executor_id)
        return fail("tests failed") if c.executor_id == "llama3" else ok()

    r = execute_with_recovery(d, run, store=store, recovery=Recovery(attempts_per_executor=3))
    assert calls == ["llama3", "deepseek"] and r.executed_by == "deepseek"


def test_escalation_to_cloud_is_recorded_with_reason_and_success_is_attributed_to_cloud(store):
    p = Policy(strategy="LOCAL_FIRST", cloud_authorized=True, cloud_token_budget=5000, auto_escalation=True)
    d = route(TASK, [local("llama3"), CLOUD], p, RES)

    def run(c, fb):
        return fail("tests failed") if c.kind == "local" else ok(tokens_measured=1200)

    r = execute_with_recovery(d, run, store=store, recovery=Recovery(attempts_per_executor=1))
    assert r.executed_by == "claude" and r.executed_by_kind == "cloud"
    evs = events(store)
    esc = [p for t, p in evs if t == "EXECUTION_ESCALATED"]
    assert esc == [{"from": "llama3", "to": "claude", "reason": "LOCAL_EXHAUSTED:CAPABILITY"}]
    done = [p for t, p in evs if t == "EXECUTION_COMPLETED"][0]
    assert done["executed_by"] == "claude" and done["executed_by_kind"] == "cloud"
    assert done["local_attempts_failed"] == 1
    tok = [p for t, p in evs if t == "TOKENS_CONSUMED"]
    assert tok == [{"executor_id": "claude", "kind": "cloud", "measured": 1200, "estimated": None}]


def test_measured_and_estimated_tokens_are_never_merged(store):
    d = route(TASK, [local("llama3")], Policy(strategy="LOCAL_ONLY"), RES)
    execute_with_recovery(d, lambda c, fb: ok(tokens_estimated=800), store=store)
    tok = [p for t, p in events(store) if t == "TOKENS_CONSUMED"]
    assert tok == [{"executor_id": "llama3", "kind": "local", "measured": None, "estimated": 800}]


def test_cloud_budget_is_enforced_during_execution(store):
    p = Policy(strategy="CLOUD_FIRST", cloud_authorized=True, cloud_token_budget=1000)
    d = route(TASK, [CLOUD, local("llama3")], p, RES)
    calls = []

    def run(c, fb):
        calls.append(c.executor_id)
        return AttemptOutcome("FAILED", error="tests failed", tokens_measured=1500) if c.kind == "cloud" else ok()

    r = execute_with_recovery(d, run, store=store, recovery=Recovery(attempts_per_executor=3))
    assert calls == ["claude", "llama3"] and r.executed_by == "llama3"
    assert r.cloud_tokens_measured == 1500


def test_refused_route_is_recorded_and_nothing_runs(store):
    d = route(TASK, [CLOUD], Policy(strategy="LOCAL_ONLY"), RES)
    r = execute_with_recovery(d, lambda c, fb: pytest.fail("must not run"), store=store)
    assert r.status == "REFUSED"
    assert [t for t, _ in events(store)] == ["ROUTING_DECIDED", "ROUTING_REFUSED"]
    assert events(store)[1][1]["rejected"] == {"claude": "STRATEGY_EXCLUDES_CLOUD"}


def test_reexecution_is_idempotent_per_run_id(store):
    d = route(TASK, [local("llama3")], Policy(strategy="LOCAL_ONLY"), RES)
    execute_with_recovery(d, lambda c, fb: ok(), store=store, run_id="r1")
    n = len(events(store))
    execute_with_recovery(d, lambda c, fb: ok(), store=store, run_id="r1")
    assert len(events(store)) == n


def test_infra_failure_is_retried_and_never_counted_against_the_model(store):
    d = route(TASK, [local("llama3")], Policy(strategy="LOCAL_ONLY"), RES)
    outs = iter([AttemptOutcome("FAILED", error="INFRA:SKETCHUP_BUSY"), ok()])
    r = execute_with_recovery(d, lambda c, fb: next(outs), store=store)
    assert r.status == "SUCCEEDED"
    done = [p for t, p in events(store) if t == "EXECUTION_COMPLETED"][0]
    assert done["local_attempts_failed"] == 0
    failed = [p for t, p in events(store) if t == "EXECUTION_ATTEMPT_FAILED"][0]
    assert failed["failure_class"] == "TOOL" and failed["infra"] is True
