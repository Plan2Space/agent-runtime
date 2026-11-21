"""Versioned records emitted by the harness: ``agent_event`` v1 and ``benchmark_result`` v1.

Consumers (Agent Bar) read these; they never write them. Unknown majors are rejected.
Schemas: docs/contracts/agent_event.v1.schema.json, docs/contracts/benchmark_result.v1.schema.json.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

AGENT_EVENT_SCHEMA = "agent_event/1.1"  # 1.1: optional "manifest" {path, sha256} on RUN_FINISHED
BENCHMARK_RESULT_SCHEMA = "benchmark_result/1.0"

EVENT_TYPES = frozenset({"RUN_STARTED", "ATTEMPT_STARTED", "TOOL_CALLED", "TESTS_RAN",
                         "ATTEMPT_FINISHED", "RUN_FINISHED"})
STATUSES = frozenset({"running", "succeeded", "failed", "blocked", "infra_error", "timeout", "cancelled"})
OUTCOMES = frozenset({"SUCCESS", "FUNCTIONAL_FAILURE", "BLOCKED", "INFRA_FAILURE", "TIMEOUT",
                      "CONTRACT_VIOLATION", "CANCELLED"})


class RecordError(ValueError):
    pass


def _check_schema(rec: Mapping[str, Any], expected: str) -> None:
    got = rec.get("schema")
    if not isinstance(got, str) or "/" not in got:
        raise RecordError(f"schema missing or malformed: {got!r}")
    name, _, ver = got.partition("/")
    if name != expected.split("/")[0] or ver.split(".")[0] != expected.split("/")[1].split(".")[0]:
        raise RecordError(f"unsupported schema {got!r}; expected {expected} (same major)")


def _req(rec: Mapping[str, Any], key: str, types: tuple, nullable: bool = False) -> None:
    if key not in rec:
        raise RecordError(f"missing field {key!r}")
    v = rec[key]
    if v is None and nullable:
        return
    if isinstance(v, bool) and bool not in types:
        raise RecordError(f"field {key!r} has wrong type bool")
    if not isinstance(v, types):
        raise RecordError(f"field {key!r} has wrong type {type(v).__name__}")


def validate_agent_event(rec: Mapping[str, Any]) -> Mapping[str, Any]:
    _check_schema(rec, AGENT_EVENT_SCHEMA)
    for k in ("event_id", "ts", "run_id", "task_id", "agent", "model", "repo", "worktree", "type", "status"):
        _req(rec, k, (str,))
    if rec["type"] not in EVENT_TYPES:
        raise RecordError(f"unknown event type {rec['type']!r}")
    if rec["status"] not in STATUSES:
        raise RecordError(f"unknown status {rec['status']!r}")
    _req(rec, "attempt", (int,))
    _req(rec, "max_attempts", (int,))
    _req(rec, "duration_s", (int, float), nullable=True)
    _req(rec, "head_sha", (str,), nullable=True)
    _req(rec, "pr", (str,), nullable=True)
    _req(rec, "error", (str,), nullable=True)
    _req(rec, "tests", (dict,), nullable=True)
    _req(rec, "evidence", (list,))
    if "manifest" in rec and rec["manifest"] is not None:
        m = rec["manifest"]
        if not isinstance(m, dict) or not isinstance(m.get("path"), str) or not isinstance(m.get("sha256"), str):
            raise RecordError("manifest must be {path: str, sha256: str}")
    return rec


def validate_benchmark_result(rec: Mapping[str, Any]) -> Mapping[str, Any]:
    _check_schema(rec, BENCHMARK_RESULT_SCHEMA)
    for k in ("run_id", "scenario", "backend", "provider", "model", "base_commit", "outcome"):
        _req(rec, k, (str,))
    if rec["outcome"] not in OUTCOMES:
        raise RecordError(f"unknown outcome {rec['outcome']!r}")
    _req(rec, "repeat", (int,))
    _req(rec, "functional_success", (bool,))
    _req(rec, "infra_failure", (bool,))
    _req(rec, "human_intervention", (bool,))
    _req(rec, "attempts", (int,))
    _req(rec, "duration_s", (int, float))
    _req(rec, "tool_calls", (int,))
    _req(rec, "tool_errors", (int,))
    _req(rec, "tests_before", (dict,))
    _req(rec, "tests_after", (dict,), nullable=True)
    _req(rec, "hidden_tests", (dict,), nullable=True)
    _req(rec, "tokens", (dict,))
    _req(rec, "contract_violations", (list,))
    _req(rec, "evidence", (dict,))
    if rec["functional_success"] and rec["outcome"] != "SUCCESS":
        raise RecordError("functional_success requires outcome SUCCESS")
    if rec["infra_failure"] and rec["functional_success"]:
        raise RecordError("an infrastructure failure cannot be a success")
    return rec


class JsonlSink:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, rec: Mapping[str, Any]) -> None:
        validate_agent_event(rec)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(dict(rec), ensure_ascii=False) + "\n")
