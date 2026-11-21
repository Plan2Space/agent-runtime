"""Versioned records emitted by the harness: ``agent_event`` v1 and ``benchmark_result`` v1.

Consumers (Agent Bar) read these; they never write them. Unknown majors are rejected.
Schemas: docs/contracts/agent_event.v1.schema.json, docs/contracts/benchmark_result.v1.schema.json.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
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


TERMINAL_STATUSES = STATUSES - {"running"}
ERROR_REQUIRED = frozenset({"blocked", "infra_error", "timeout", "failed"})
TEST_KEYS = ("exit_code", "timed_out", "passed", "failed", "errors", "skipped", "passed_all", "duration_s")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")
_PR = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")


def _utc_ts(v: str) -> None:
    try:
        ts = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise RecordError(f"ts is not ISO 8601: {v!r}") from None
    if ts.utcoffset() != timedelta(0):
        raise RecordError(f"ts must be UTC: {v!r}")


def _relative(path: object, what: str) -> None:
    if not isinstance(path, str) or not path:
        raise RecordError(f"{what} must be a non-empty string")
    p = path.replace("\\", "/")
    if p.startswith("/") or ":" in p or ".." in p.split("/"):
        raise RecordError(f"{what} must be relative to the run directory: {path!r}")


def _non_negative(rec: Mapping[str, Any], *keys: str) -> None:
    for k in keys:
        v = rec.get(k)
        if v is not None and v < 0:
            raise RecordError(f"field {k!r} must be >= 0")


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
    # semantics (S00-05 R1, R2a)
    if not rec["event_id"].strip():
        raise RecordError("event_id must not be empty")
    _utc_ts(rec["ts"])
    if rec["max_attempts"] < 1 or not 0 <= rec["attempt"] <= rec["max_attempts"]:
        raise RecordError(f"attempt {rec['attempt']} outside 0..max_attempts ({rec['max_attempts']})")
    _non_negative(rec, "duration_s")
    finished = rec["type"] == "RUN_FINISHED"
    if rec["status"] in TERMINAL_STATUSES and not finished:
        raise RecordError(f"terminal status {rec['status']!r} only on RUN_FINISHED (got {rec['type']})")
    if finished and rec["status"] == "running":
        raise RecordError("RUN_FINISHED needs a terminal status")
    if rec["status"] in ERROR_REQUIRED and not (rec["error"] or "").strip():
        raise RecordError(f"status {rec['status']!r} requires a non-empty error")
    if rec["head_sha"] is not None and not _SHA40.match(rec["head_sha"]):
        raise RecordError("head_sha must be a full 40-hex SHA")
    if rec["pr"] is not None and not _PR.match(rec["pr"]):
        raise RecordError("pr must be owner/repo#n")
    if rec["tests"] is not None:
        missing = [k for k in TEST_KEYS if k not in rec["tests"]]
        if missing:
            raise RecordError(f"tests missing keys {missing}")
    for i, e in enumerate(rec["evidence"]):
        _relative(e, f"evidence[{i}]")
    if "manifest" in rec and rec["manifest"] is not None:
        m = rec["manifest"]
        if not isinstance(m, dict) or not isinstance(m.get("path"), str) or not isinstance(m.get("sha256"), str):
            raise RecordError("manifest must be {path: str, sha256: str}")
        if not finished:
            raise RecordError("manifest only on RUN_FINISHED")
        _relative(m["path"], "manifest.path")
        if not _SHA64.match(m["sha256"]):
            raise RecordError("manifest.sha256 must be 64 hex")
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
    # semantics (S00-05 R5, R7, R7a)
    if not _SHA40.match(rec["base_commit"]):
        raise RecordError("base_commit must be a full 40-hex SHA")
    _non_negative(rec, "repeat", "attempts", "duration_s", "tool_calls", "tool_errors")
    if rec["tool_errors"] > rec["tool_calls"]:
        raise RecordError("tool_errors cannot exceed tool_calls")
    if (rec["outcome"] == "SUCCESS") != rec["functional_success"]:
        raise RecordError("outcome SUCCESS and functional_success must agree")
    if (rec["outcome"] == "INFRA_FAILURE") != rec["infra_failure"]:
        raise RecordError("outcome INFRA_FAILURE and infra_failure must agree")
    t = rec["tokens"]
    if t.get("measured") is False:
        if t.get("prompt") is not None or t.get("completion") is not None or not str(t.get("reason") or "").strip():
            raise RecordError("unmeasured tokens must be null with a reason (R7)")
    elif t.get("measured") is True:
        for k in ("prompt", "completion"):
            v = t.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise RecordError(f"measured tokens.{k} must be an integer >= 0")
    else:
        raise RecordError("tokens.measured must be true or false")
    return rec


class JsonlSink:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, rec: Mapping[str, Any]) -> None:
        validate_agent_event(rec)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(dict(rec), ensure_ascii=False) + "\n")
