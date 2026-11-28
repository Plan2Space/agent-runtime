"""Contract tests: runtime side of the local-executor boundary (agent-bar local executor).

Schemas are vendored verbatim from Plan2Space/agent-bar@123b8fafc18c598a1b5ea3a2c42ebcec87cd5b12
(docs/contracts/). The runtime has no runtime dependencies, so instead of a JSON Schema engine these
tests pin what the runtime relies on (required keys, enums, consts) to the schema files: if the
producer changes the contract, the vendored copy is refreshed and these tests say what broke.
"""
import json
from pathlib import Path

import pytest

from agent_runtime.execution import FAILURE_CLASSES, AttemptOutcome
from agent_runtime.local_job import build_job, outcome_from_document, run_local_job
from agent_runtime.proofs import load_capability_proofs
from agent_runtime.routing import Candidate

HERE = Path(__file__).parent / "p2s_local"
SHA = "a" * 40


def schema(name):
    return json.loads((HERE / f"p2s.local_{name}.v1.schema.json").read_text(encoding="utf-8"))


def outcome_doc(**kw):
    base = {"schema": "p2s.local_outcome.v1", "job_id": "t1-a1", "task_type": "build", "status": "SUCCEEDED",
            "error": None, "failure_class": None, "executor_id": "ollama:llama3.1:8b@46e0c10c",
            "tokens_measured": 4200, "tokens_estimated": None,
            "cloud": {"tokens": 0, "providers_called": []}, "assistance": [], "permissions": [],
            "failure": None, "started_at": "x", "finished_at": "y"}
    base.update(kw)
    return base


LOCAL = Candidate("ollama:llama3.1:8b@46e0c10c", "local", "LOCAL_PROVEN", "proof")
DET = Candidate("deterministic", "deterministic", "DETERMINISTIC", "recipe")


# -- job -----------------------------------------------------------------------------------------
def test_job_has_exactly_the_schema_keys_and_consts():
    s = schema("job")
    job = build_job("t1", "build", LOCAL, {"fixture_ref": "room_001"}, SHA, attempt=1, feedback=None)
    assert set(s["required"]) <= set(job) <= set(s["properties"])
    assert job["schema"] == s["properties"]["schema"]["const"]
    assert job["engine_ref"]["repo"] == s["properties"]["engine_ref"]["properties"]["repo"]["const"]
    ex = s["properties"]["executor"]
    assert set(job["executor"]) <= set(ex["properties"])
    assert job["executor"] == {"kind": "local", "model_id": "llama3.1:8b", "adapter": "ollama",
                               "tier": "LOCAL_PROVEN"}


def test_deterministic_job_carries_no_model():
    job = build_job("t1", "render", DET, {"run_id": "lx-1", "cameras": ["c1"]}, SHA, attempt=2,
                    feedback={"failure_class": "TOOL", "attempt": 1, "executor_id": "deterministic", "error": "x"})
    assert job["executor"] == {"kind": "deterministic", "tier": "DETERMINISTIC"}
    assert job["attempt"] == 2 and job["feedback"]["failure_class"] == "TOOL"


@pytest.mark.parametrize("kw", [
    {"task_type": "bench"}, {"sha40": "abc"}, {"job_id": "t 1"},
    {"candidate": Candidate("claude", "cloud", "CLOUD", "x")},
])
def test_job_outside_the_contract_is_refused_before_the_executor_runs(kw):
    args = dict(task_id="t1", task_type="build", candidate=LOCAL, inputs={"fixture_ref": "r"}, sha40=SHA)
    if "job_id" in kw:
        args["task_id"] = kw.pop("job_id")
    args.update(kw)
    with pytest.raises(ValueError):
        build_job(args["task_id"], args["task_type"], args["candidate"], args["inputs"], args["sha40"],
                  attempt=1, feedback=None)


def test_job_enums_match_runtime():
    s = schema("job")
    assert set(s["properties"]["feedback"]["properties"]["failure_class"]["enum"]) == set(FAILURE_CLASSES)


# -- outcome -------------------------------------------------------------------------------------
def test_outcome_required_keys_are_what_the_runtime_reads():
    s = schema("outcome")
    assert {"status", "error", "tokens_measured", "tokens_estimated", "failure_class"} <= set(s["properties"])
    assert set(s["properties"]["failure_class"]["enum"]) - {None} == set(FAILURE_CLASSES)


def test_outcome_maps_to_attempt_outcome():
    out = outcome_from_document(outcome_doc(status="TOOL_ERROR", error="CAMERA_NOT_ALLOWED",
                                            failure_class="SCHEMA", tokens_measured=900))
    assert out == AttemptOutcome("TOOL_ERROR", error="CAMERA_NOT_ALLOWED", tokens_measured=900,
                                 tokens_estimated=None, failure_class="SCHEMA")


def test_outcome_reporting_cloud_use_is_rejected():
    with pytest.raises(ValueError):
        outcome_from_document(outcome_doc(cloud={"tokens": 10, "providers_called": ["anthropic"]}))


def test_outcome_with_unknown_schema_is_rejected():
    with pytest.raises(ValueError):
        outcome_from_document(outcome_doc(schema="p2s.local_outcome.v2"))


# -- CLI seam ------------------------------------------------------------------------------------
def test_run_local_job_writes_job_and_reads_outcome(tmp_path):
    seen = {}

    def fake_runner(argv, job_path, out_dir):
        seen["argv"], seen["job"] = argv, json.loads(Path(job_path).read_text(encoding="utf-8"))
        d = Path(out_dir) / seen["job"]["job_id"]
        d.mkdir(parents=True)
        (d / "outcome.json").write_text(json.dumps(outcome_doc(job_id=seen["job"]["job_id"])), encoding="utf-8")
        return 0

    out = run_local_job(LOCAL, None, task_id="t1", task_type="build", inputs={"fixture_ref": "room_001"},
                        sha40=SHA, attempt=1, work_dir=tmp_path, runner=fake_runner)
    assert out.status == "SUCCEEDED" and out.tokens_measured == 4200
    assert seen["argv"][1:4] == ["-m", "agent_bar.platform.local_job", "run"]
    assert seen["job"]["job_id"] == "t1-a1"


def test_missing_outcome_is_an_infra_failure_not_a_model_failure(tmp_path):
    out = run_local_job(LOCAL, None, task_id="t1", task_type="build", inputs={"fixture_ref": "r"},
                        sha40=SHA, attempt=1, work_dir=tmp_path, runner=lambda *a: 3)
    assert out.status == "FAILED" and out.error.startswith("INFRA:NO_OUTCOME")


# -- capability ----------------------------------------------------------------------------------
def test_capability_loader_reads_a_schema_shaped_document(tmp_path):
    s = schema("capability")
    item_keys = s["properties"]["items"]["items"]["required"]
    it = {"executor_id": "ollama:llama3.1:8b@46e0c10c", "task_type": "build", "runs": 12, "passes": 8,
          "fails": 2, "infra_errors": 2, "success_rate": 0.8, "engine_sha40": [SHA],
          "source": [{"path": "b/run.json", "sha256": "c" * 64}]}
    assert set(item_keys) == set(it)
    doc = {"schema": "p2s.local_capability.v1", "generated_at": "2026-11-27T00:00:00Z", "items": [it]}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    proof = load_capability_proofs(p)["ollama:llama3.1:8b@46e0c10c"]["build"]
    assert proof.success_rate == 0.8 and proof.runs == 10  # passes+fails: INFRA and REFUSED stay out


# -- invalid or refused outcome: always a classified failure and a terminal fact ------------------
def _writer(content):
    def runner(argv, job_path, out_dir):
        job = json.loads(Path(job_path).read_text(encoding="utf-8"))
        d = Path(out_dir) / job["job_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "outcome.json").write_text(content(job["job_id"]), encoding="utf-8")
        return 0
    return runner


BAD_OUTCOMES = {
    "not_json": lambda jid: "{not json",
    "wrong_schema": lambda jid: json.dumps(outcome_doc(job_id=jid, schema="p2s.local_outcome.v2")),
    "cloud_used": lambda jid: json.dumps(outcome_doc(job_id=jid, cloud={"tokens": 5, "providers_called": ["x"]})),
    "unknown_failure_class": lambda jid: json.dumps(outcome_doc(job_id=jid, status="FAILED", failure_class="WEIRD")),
}


@pytest.mark.parametrize("case", sorted(BAD_OUTCOMES))
def test_invalid_outcome_is_a_protocol_failure_not_an_exception(tmp_path, case):
    out = run_local_job(LOCAL, None, task_id="t1", task_type="build", inputs={"fixture_ref": "r"}, sha40=SHA,
                        attempt=1, work_dir=tmp_path, runner=_writer(BAD_OUTCOMES[case]))
    assert out.status == "FAILED" and out.failure_class == "PROTOCOL"
    assert out.error.startswith("PROTOCOL:INVALID_OUTCOME")


@pytest.mark.parametrize("case", sorted(BAD_OUTCOMES))
def test_invalid_outcome_still_records_a_terminal_fact(tmp_path, case):
    from agent_runtime.event_store import EventStore
    from agent_runtime.execution import execute_with_recovery
    from agent_runtime.routing import Executor, Policy, Proof, Resources, Task, route

    store = EventStore(tmp_path / "e.db", clock=lambda: 1.0)
    ex = Executor(LOCAL.executor_id, "local", tools=frozenset({"build"}), proofs={"build": Proof(0.9, 10, "x")})
    d = route(Task("t1", "build", 1, frozenset({"build"})), [ex], Policy(strategy="LOCAL_ONLY"), Resources(16000))
    n = {"a": 0}

    def run(c, fb):
        n["a"] += 1
        return run_local_job(c, fb, task_id="t1", task_type="build", inputs={"fixture_ref": "r"}, sha40=SHA,
                             attempt=n["a"], work_dir=tmp_path / "w", runner=_writer(BAD_OUTCOMES[case]))

    r = execute_with_recovery(d, run, store=store)
    types = [e["event_type"] for e in store.events(job_id="t1")]
    assert r.status == "FAILED" and types[-1] == "EXECUTION_FAILED"
