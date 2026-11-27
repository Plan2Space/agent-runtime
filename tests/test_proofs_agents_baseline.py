"""PoC capability proofs + failure records in routing, Codex/Claude identity under one ownership,
and the measured cloud-token baseline (adendo §1/§6/§7)."""
import json

import pytest

from agent_runtime.agent_identity import (
    AgentIdentity,
    OwnershipConflict,
    claim_task,
    register_session,
)
from agent_runtime.event_store import EventStore
from agent_runtime.proofs import ProofError, load_capability_proofs, load_failures
from agent_runtime.routing import Executor, Policy, Resources, Task, route
from agent_runtime.token_baseline import (
    claude_transcript_usage,
    production_cloud_tokens,
)

BUILD = "build"


def write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def proofs_doc(items):
    return {"schema": "p2s.local_capability.v1", "items": items}


def item(eid, runs=10, passes=9, fails=1, infra=0, rate=0.9):
    return {"executor_id": eid, "task_type": BUILD, "runs": runs, "passes": passes, "fails": fails,
            "infra_errors": infra, "success_rate": rate, "engine_sha40": ["a" * 40],
            "source": [{"path": "bench/llama/benchmark_run.json", "sha256": "b" * 64}]}


def test_capability_proofs_become_router_proofs(tmp_path):
    p = load_capability_proofs(write(tmp_path / "c.json", proofs_doc([item("ollama:llama3.1:8b@46e0c10c")])))
    proof = p["ollama:llama3.1:8b@46e0c10c"][BUILD]
    assert proof.success_rate == 0.9 and proof.runs == 10
    assert proof.source.startswith("p2s.local_capability.v1:bench/llama/benchmark_run.json")


def test_null_success_rate_is_no_proof(tmp_path):
    p = load_capability_proofs(write(tmp_path / "c.json", proofs_doc([item("m", runs=2, passes=0, fails=0,
                                                                          infra=2, rate=None)])))
    assert p == {}


@pytest.mark.parametrize("doc", [{"schema": "p2s.local_capability.v2", "items": []},
                                 {"items": []}, proofs_doc([{"executor_id": "m"}])])
def test_bad_proof_documents_are_rejected(tmp_path, doc):
    with pytest.raises(ProofError):
        load_capability_proofs(write(tmp_path / "c.json", doc))


def failure(eid, cls="SCHEMA", tool="build_room"):
    return {"schema": "p2s.local_failure.v1", "executor_id": eid, "task_type": BUILD, "tool": tool,
            "failure_class": cls, "invalid_argument": {"name": "cameras", "sent": "\"[1]\"", "expected": "array"},
            "contract_misread": None, "sequence": [], "helpful_example": None,
            "adapter_fix": "strict coercion", "skill_hint": None, "source": "run/1"}


def test_failure_records_are_counted_per_executor_and_task_type(tmp_path):
    f = tmp_path / "f.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in [failure("a"), failure("a"), failure("b", "PROTOCOL")]) + "\n",
                 encoding="utf-8")
    assert load_failures(f) == {("a", BUILD): 2, ("b", BUILD): 1}


def test_recurring_failures_demote_a_proven_local(tmp_path):
    from agent_runtime.routing import Proof
    exs = [Executor(e, "local", tools=frozenset({BUILD}), proofs={BUILD: Proof(0.9, 10, "x")}) for e in ("a", "b")]
    t = Task("t", BUILD, 1, frozenset({BUILD}))
    d = route(t, exs, Policy(strategy="LOCAL_ONLY", max_recent_failures=3), Resources(16000),
              recent_failures={("a", BUILD): 3})
    assert d.chosen.executor_id == "b" and d.rejected["a"] == "RECURRING_FAILURE"


# -- one governance for Claude Code and Codex ----------------------------------------------------
@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "e.db", clock=lambda: 1.0)


def ident(agent, session="s1", task="P2S-E-1"):
    return AgentIdentity(agent=agent, session_id=session, frente="E", worktree="E:/Claude/worktrees/x",
                         task_id=task)


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_both_agents_register_with_the_same_identity_shape(store, agent):
    register_session(store, ident(agent))
    ev = [e for e in store.events() if e["event_type"] == "AGENT_SESSION_REGISTERED"]
    assert json.loads(ev[0]["payload"])["agent"] == agent


def test_unknown_agent_is_rejected():
    with pytest.raises(ValueError):
        ident("gemini")


def test_task_owned_by_claude_cannot_be_claimed_by_codex(store):
    claim_task(store, ident("claude-code", "s1"))
    with pytest.raises(OwnershipConflict):
        claim_task(store, ident("codex", "s2"))


def test_reclaim_by_the_same_session_is_idempotent(store):
    claim_task(store, ident("codex", "s2"))
    claim_task(store, ident("codex", "s2"))
    assert len([e for e in store.events() if e["event_type"] == "TASK_OWNERSHIP_CLAIMED"]) == 1


# -- measured baseline ---------------------------------------------------------------------------
def test_claude_transcript_usage_is_measured_and_deduplicated(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    u = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
         "output_tokens": 5}
    lines = [
        {"type": "assistant", "timestamp": "2026-11-20T10:00:00Z", "requestId": "r1",
         "message": {"id": "m1", "model": "claude-opus-5-5", "usage": u}},
        {"type": "assistant", "timestamp": "2026-11-20T10:00:00Z", "requestId": "r1",
         "message": {"id": "m1", "model": "claude-opus-5-5", "usage": u}},  # same message, 2nd block
        {"type": "assistant", "timestamp": "2026-12-01T10:00:00Z", "requestId": "r2",
         "message": {"id": "m2", "model": "claude-opus-5-5", "usage": u}},  # outside window
        {"type": "user", "timestamp": "2026-11-20T10:00:00Z"},
    ]
    (proj / "a.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
    r = claude_transcript_usage(tmp_path, since="2026-11-01T00:00:00Z", until="2026-11-30T00:00:00Z")
    assert r["kind"] == "measured" and r["scope"] == "engineering"
    assert r["models"]["claude-opus-5-5"] == {"messages": 1, "input": 10, "cache_creation": 100,
                                              "cache_read": 1000, "output": 5}
    assert r["skipped_lines"] == 1


def test_production_cloud_tokens_come_only_from_measured_router_facts(store):
    store.publish("TOKENS_CONSUMED", "k1", "t", {"executor_id": "claude", "kind": "cloud",
                                                 "measured": 700, "estimated": None},
                  aggregate_type="task", producer="router")
    store.publish("TOKENS_CONSUMED", "k2", "t", {"executor_id": "claude", "kind": "cloud",
                                                 "measured": None, "estimated": 300},
                  aggregate_type="task", producer="router")
    store.publish("TOKENS_CONSUMED", "k3", "t", {"executor_id": "llama", "kind": "local",
                                                 "measured": 9000, "estimated": None},
                  aggregate_type="task", producer="router")
    assert production_cloud_tokens(store) == {"scope": "production", "cloud_measured": 700,
                                              "cloud_estimated": 300, "cloud_unmeasured_events": 1}
