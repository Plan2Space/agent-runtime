"""Read the PoC's capability proofs and failure records for the router (adendo §6).

Producer: the local executor's benchmark in agent-bar (``capability_proofs.json``,
schema ``p2s.local_capability.v1``; failures JSONL ``p2s.local_failure.v1``). The runtime only
reads files: it never imports agent-bar. Unknown schemas are rejected, not coerced. An item with
``success_rate = null`` (no denominator once infra errors are removed) is *no proof*.
"""

from __future__ import annotations

import json
from pathlib import Path

from .routing import Proof

PROOFS_SCHEMA = "p2s.local_capability.v1"
FAILURE_SCHEMA = "p2s.local_failure.v1"
_ITEM_KEYS = ("executor_id", "task_type", "runs", "passes", "fails", "infra_errors", "success_rate",
              "engine_sha40", "source")


class ProofError(ValueError):
    pass


def load_capability_proofs(path: str | Path) -> dict[str, dict[str, Proof]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != PROOFS_SCHEMA:
        raise ProofError(f"expected schema {PROOFS_SCHEMA}, got {doc.get('schema') if isinstance(doc, dict) else doc!r}")
    out: dict[str, dict[str, Proof]] = {}
    for it in doc.get("items", []):
        missing = [k for k in _ITEM_KEYS if k not in it]
        if missing:
            raise ProofError(f"proof item missing {missing}")
        rate = it["success_rate"]
        if rate is None:
            continue
        if not 0 <= rate <= 1:
            raise ProofError(f"success_rate out of range: {rate!r}")
        denominator = it["passes"] + it["fails"]  # REFUSED and INFRA are not executions of the model
        src = ",".join(f"{s['path']}@{s['sha256'][:12]}" for s in it["source"])
        out.setdefault(it["executor_id"], {})[it["task_type"]] = Proof(
            success_rate=float(rate), runs=int(denominator), source=f"{PROOFS_SCHEMA}:{src}")
    return out


def load_failures(path: str | Path) -> dict[tuple[str, str], int]:
    """Count failure records per (executor_id, task_type). Unparseable lines are skipped, not guessed."""
    counts: dict[tuple[str, str], int] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("schema") != FAILURE_SCHEMA:
            continue
        key = (r["executor_id"], r["task_type"])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))
