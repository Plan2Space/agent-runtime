"""``run_attempt`` for local and deterministic executors via the PoC's file/CLI boundary.

Contract (agent-bar@123b8faf, docs/contracts/): the runtime writes a ``p2s.local_job.v1`` file,
runs ``python -m agent_bar.platform.local_job run <job.json> --out <dir>`` and reads
``<dir>/<job_id>/outcome.json`` (``p2s.local_outcome.v1``). No import of agent-bar.

The engine version is pinned by ``sha40`` in every job; the executor refuses (``ENGINE_NOT_PINNED``)
an unpinned, dirty or non-production checkout. Cloud candidates never go through here, and an
outcome that reports any cloud use is rejected: production local is zero cloud by contract.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .execution import FAILURE_CLASSES, AttemptOutcome
from .routing import Candidate

JOB_SCHEMA = "p2s.local_job.v1"
OUTCOME_SCHEMA = "p2s.local_outcome.v1"
ENGINE_REPO = "Plan2Space/sketchup-mcp-next"
TASK_TYPES = frozenset({"build", "render"})
_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_OLLAMA_ID = re.compile(r"^ollama:(?P<model>.+)@[0-9a-f]{8}$")


def build_job(task_id: str, task_type: str, candidate: Candidate, inputs: dict, sha40: str, *,
              attempt: int, feedback: dict | None) -> dict:
    job_id = f"{task_id}-a{attempt}"
    if not _JOB_ID.match(job_id):
        raise ValueError(f"job_id outside contract: {job_id!r}")
    if task_type not in TASK_TYPES:
        raise ValueError(f"task_type {task_type!r} not in {sorted(TASK_TYPES)}")
    if not _SHA40.match(sha40 or ""):
        raise ValueError("engine sha40 must be 40 lowercase hex chars")
    if candidate.kind == "local":
        m = _OLLAMA_ID.match(candidate.executor_id)
        if not m:
            raise ValueError(f"local executor_id must be ollama:<model>@<digest8>: {candidate.executor_id!r}")
        executor = {"kind": "local", "model_id": m["model"], "adapter": "ollama", "tier": candidate.tier}
    elif candidate.kind == "deterministic":
        executor = {"kind": "deterministic", "tier": candidate.tier}
    else:
        raise ValueError(f"{candidate.kind} executors do not run through the local job boundary")
    return {"schema": JOB_SCHEMA, "job_id": job_id, "task_type": task_type, "executor": executor,
            "inputs": dict(inputs), "engine_ref": {"repo": ENGINE_REPO, "sha40": sha40},
            "attempt": attempt, "feedback": feedback}


def outcome_from_document(doc: dict) -> AttemptOutcome:
    if doc.get("schema") != OUTCOME_SCHEMA:
        raise ValueError(f"expected {OUTCOME_SCHEMA}, got {doc.get('schema')!r}")
    cloud = doc.get("cloud") or {}
    if cloud.get("tokens") != 0 or cloud.get("providers_called"):
        raise ValueError(f"local outcome reports cloud use: {cloud!r}")
    fc = doc.get("failure_class")
    if fc is not None and fc not in FAILURE_CLASSES:
        raise ValueError(f"unknown failure_class {fc!r}")
    return AttemptOutcome(doc["status"], error=doc.get("error"), tokens_measured=doc.get("tokens_measured"),
                          tokens_estimated=doc.get("tokens_estimated"), failure_class=fc)


def _subprocess_runner(argv: list[str], job_path: Path, out_dir: Path) -> int:
    return subprocess.run(argv, check=False).returncode


def run_local_job(candidate: Candidate, feedback: dict | None, *, task_id: str, task_type: str, inputs: dict,
                  sha40: str, attempt: int, work_dir: str | Path,
                  runner: Callable[[list[str], Path, Path], int] = _subprocess_runner,
                  python: str = sys.executable) -> AttemptOutcome:
    work = Path(work_dir).resolve()
    job = build_job(task_id, task_type, candidate, inputs, sha40, attempt=attempt, feedback=feedback)
    jobs, outs = work / "jobs", work / "out"
    jobs.mkdir(parents=True, exist_ok=True)
    outs.mkdir(parents=True, exist_ok=True)
    job_path = jobs / f"{job['job_id']}.json"
    job_path.write_text(json.dumps(job, sort_keys=True), encoding="utf-8")
    argv = [python, "-m", "agent_bar.platform.local_job", "run", str(job_path), "--out", str(outs)]
    code = runner(argv, job_path, outs)
    out_path = outs / job["job_id"] / "outcome.json"
    if not out_path.is_file():
        return AttemptOutcome("FAILED", error=f"INFRA:NO_OUTCOME exit={code}")
    return outcome_from_document(json.loads(out_path.read_text(encoding="utf-8")))
