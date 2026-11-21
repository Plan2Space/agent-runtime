"""The published JSON Schemas and the runtime validators must agree on required fields and enums."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "docs" / "contracts"

agents_events = pytest.importorskip("agent_runtime.agents.events")


def _schema(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_agent_event_schema_matches_validator_enums():
    s = _schema("agent_event.v1.schema.json")
    assert set(s["properties"]["type"]["enum"]) == agents_events.EVENT_TYPES
    assert set(s["properties"]["status"]["enum"]) == agents_events.STATUSES


def test_benchmark_result_schema_matches_validator_enums():
    s = _schema("benchmark_result.v1.schema.json")
    assert set(s["properties"]["outcome"]["enum"]) == agents_events.OUTCOMES


@pytest.mark.parametrize("name,validator", [("agent_event.v1.schema.json", "validate_agent_event"),
                                            ("benchmark_result.v1.schema.json", "validate_benchmark_result")])
def test_every_schema_required_field_is_enforced_by_the_validator(name, validator):
    s = _schema(name)
    fn = getattr(agents_events, validator)
    # each field individually
    full = _complete(s)
    for field in s["required"]:
        if field == "schema":
            continue
        broken = {k: v for k, v in full.items() if k != field}
        with pytest.raises(agents_events.RecordError):
            fn(broken)
    fn(full)


def _complete(s):
    name = s["properties"]["schema"]["pattern"].split("/")[0].lstrip("^")
    if name == "agent_event":
        return {"schema": "agent_event/1.0", "event_id": "e", "ts": "t", "run_id": "r", "task_id": "S00-11",
                "agent": "qwen", "model": "m", "repo": "r", "worktree": "w", "type": "RUN_STARTED",
                "status": "running", "attempt": 0, "max_attempts": 3, "duration_s": None, "head_sha": None,
                "pr": None, "error": None, "tests": None, "evidence": []}
    return {"schema": "benchmark_result/1.0", "run_id": "r", "scenario": "s", "repeat": 1, "backend": "b",
            "provider": "p", "model": "m", "base_commit": "c", "outcome": "BLOCKED", "functional_success": False,
            "infra_failure": False, "human_intervention": False, "attempts": 3, "duration_s": 1.0, "tool_calls": 0,
            "tool_errors": 0, "tests_before": {}, "tests_after": None, "hidden_tests": None, "tokens": {},
            "contract_violations": [], "evidence": {}}
