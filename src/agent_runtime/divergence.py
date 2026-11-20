"""Read-only divergence detector between a JSON queue mirror and the EventStore.

Issue #21 (FP-062). Replaces the sketchup-mcp `shadow_divergence` (PR #634) with
the generic core only. Executor policy (daily cap, instance lock, pause flags)
stays with the executor and is gated in the cutover issue (#19).

Contract:
- Reads the queue file and the SQLite database; never writes to either one.
- The database is opened with `mode=ro`. Opening it through `EventStore` is not
  allowed here because `EventStore._conn` runs `PRAGMA journal_mode=WAL`.
- A missing or invalid source is an error, never an empty set. An empty queue
  would look like agreement.
- Unknown queue states are reported, never assumed equal.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIRROR_EVENT_TYPE = "CORRECTION_REQUESTED"
MIRROR_PRODUCER = "event_bridge"
DEFAULT_MAX_ATTEMPTS = 3
LATE_TOLERANCE_S = 60
SQLITE_MAGIC = b"SQLite format 3\x00"

# Queue states the executor may still pick (below the attempt budget).
PICKABLE = frozenset({"QUEUED", "FAILED", "BLOCKED_CLI", "BLOCKED_CREDENTIALS", "BLOCKED_WORKTREE"})
RETRYABLE = frozenset({"FAILED", "BLOCKED_CLI", "BLOCKED_CREDENTIALS", "BLOCKED_WORKTREE", "BLOCKED_REVIEW_FETCH"})
FINISHED = frozenset({"DONE_AWAITING_REREVIEW", "SUPERSEDED", "REFUSED", "PARKED_UNSUPPORTED"})
EXECUTABLE_EVENT = frozenset({"PENDING", "RUNNING"})

# Expected event status for each queue state (queue state -> allowed statuses).
EXPECTED: Mapping[str, frozenset[str]] = {
    "QUEUED": frozenset({"PENDING"}),
    "RUNNING": frozenset({"PENDING"}),
    "DONE_AWAITING_REREVIEW": frozenset({"DONE"}),
    "SUPERSEDED": frozenset({"SUPERSEDED"}),
    "REFUSED": frozenset({"SUPERSEDED"}),
    "PARKED_UNSUPPORTED": frozenset({"SUPERSEDED"}),
    "FAILED": frozenset({"PENDING", "DLQ"}),
    "BLOCKED_CLI": frozenset({"PENDING", "DLQ"}),
    "BLOCKED_CREDENTIALS": frozenset({"PENDING", "DLQ"}),
    "BLOCKED_WORKTREE": frozenset({"PENDING", "DLQ"}),
    "BLOCKED_REVIEW_FETCH": frozenset({"PENDING", "DLQ"}),
}

CANONICAL_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class DivergenceInputError(ValueError):
    """A source cannot be compared safely. Reported as an error, not a divergence."""


@dataclass(frozen=True)
class Divergence:
    code: str
    key: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "key": self.key, "detail": self.detail}


def mirror_key(item: Mapping[str, Any]) -> str:
    """The idempotency key the bridge publishes for a review item."""
    return f"review:{str(item.get('event_id', '')).split(':')[-1]}:{item.get('pr')}:{item.get('head_sha')}"


def payload_mismatch(item: Mapping[str, Any], event: Mapping[str, Any]) -> str:
    """'' when the event describes the same work as the queue item, else the reason.

    The idempotency key alone is not enough: the store keeps the first payload
    published for a key, so a colliding event with different PR/head would be
    accepted silently."""
    raw = event.get("payload")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return "payload is not JSON"
    if not isinstance(payload, dict):
        return "event has no readable payload"
    review_id = str(item.get("event_id", "")).split(":")[-1]
    if event.get("job_id") != f"pr:{item.get('pr')}":
        return f"job_id {event.get('job_id')!r} != 'pr:{item.get('pr')}'"
    for field, want in (("pr", item.get("pr")), ("head_sha", item.get("head_sha")),
                        ("review_id", review_id), ("issue", item.get("issue"))):
        if payload.get(field) != want:
            return f"payload field {field!r} ({payload.get(field)!r}) differs from queue item ({want!r})"
    return ""


def _epoch(iso: object) -> float | None:
    """Epoch seconds for a canonical `AAAA-MM-DDTHH:MM:SSZ` string, else None."""
    import datetime as dt

    if not isinstance(iso, str) or not CANONICAL_ISO.fullmatch(iso):
        return None
    try:
        return dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC).timestamp()
    except ValueError:
        return None


def invalid_item(item: Mapping[str, Any]) -> str:
    """'' when the item has the fields and types the comparison uses, else the problem.

    Only field names are reported, never their content."""
    kind = item.get("kind")
    if not isinstance(kind, str) or not kind:
        return "field `kind` missing or not text"
    if kind != "review":
        return ""
    for field in ("key", "state", "event_id", "head_sha"):
        if not isinstance(item.get(field), str) or not item[field]:
            return f"review without `{field}` (non-empty text)"
    if not isinstance(item.get("pr"), int) or isinstance(item.get("pr"), bool):
        return "review with `pr` that is not an integer"
    retry_after = item.get("retry_after")
    if retry_after not in (None, "") and _epoch(retry_after) is None:
        return "review with `retry_after` that is not a canonical UTC instant"
    attempts = item.get("attempts")
    if attempts is not None and (not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0):
        return "review with `attempts` that is not an integer >= 0"
    return ""


def compare(
    queue: list[Mapping[str, Any]],
    events: list[Mapping[str, Any]],
    *,
    now: float | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    expected: Mapping[str, frozenset[str]] = EXPECTED,
    pickable: frozenset[str] = PICKABLE,
) -> list[Divergence]:
    """Compare a queue snapshot with store events. Pure: no I/O, no clock unless `now` is None."""
    mirror = {
        e["idempotency_key"]: e
        for e in events
        if e.get("producer") == MIRROR_PRODUCER and e.get("event_type") == MIRROR_EVENT_TYPE
    }
    found: list[Divergence] = []
    matched: set[str] = set()
    t_now = time.time() if now is None else now

    for n, item in enumerate(queue):
        problem = invalid_item(item)
        if problem:
            raise DivergenceInputError(f"invalid queue item #{n}: {problem}")
        if item.get("kind") != "review":
            if item.get("state") == "RUNNING":
                found.append(Divergence("JSON_RUNNING", str(item.get("key")),
                                        "non-review item RUNNING in the JSON queue: drain it before cutover"))
            continue

        key, state = item["key"], item.get("state")
        event = mirror.get(mirror_key(item))
        if event is None:
            found.append(Divergence("MISSING_IN_STORE", key, f"item {state} without event {mirror_key(item)}"))
            continue
        matched.add(event["idempotency_key"])

        wrong = payload_mismatch(item, event)
        if wrong:
            found.append(Divergence("PAYLOAD_MISMATCH", key, f"same key but different work: {wrong}"))
            continue

        if state == "RUNNING":
            found.append(Divergence("JSON_RUNNING", key,
                                    "item RUNNING in the JSON queue: the PENDING event would be claimable in parallel"))

        allowed = expected.get(state)
        if allowed is None:
            found.append(Divergence("UNKNOWN_STATE", key, f"queue state {state!r} is not in the table; event {event['status']}"))
            continue
        if event["status"] not in allowed:
            code = "STALE_PENDING" if event["status"] == "PENDING" and state in FINISHED else "STATE_MISMATCH"
            found.append(Divergence(code, key, f"item {state} but event {event['status']} (expected {'/'.join(sorted(allowed))})"))
            continue

        if state in RETRYABLE or state == "QUEUED":
            _check_budget_and_window(found, key, state, item, event, max_attempts, pickable, t_now)

    for idem, event in mirror.items():
        if idem in matched:
            continue
        if event["status"] in EXECUTABLE_EVENT:
            found.append(Divergence("ORPHAN_EXECUTABLE", idem,
                                    f"event {event['status']} without a queue item: could run from the store alone"))
        else:
            found.append(Divergence("ORPHAN_IN_STORE", idem, f"terminal event {event['status']} without a queue item"))
    return found


def _check_budget_and_window(found: list[Divergence], key: str, state: str, item: Mapping[str, Any],
                             event: Mapping[str, Any], max_attempts: int, pickable: frozenset[str],
                             t_now: float) -> None:
    json_attempts = int(item.get("attempts") or 0)
    can_pick = state in pickable and json_attempts < max_attempts
    if event["status"] == "DLQ" and can_pick:
        found.append(Divergence("STATE_MISMATCH", key,
                                f"item {state} with {json_attempts}/{max_attempts} attempts still pickable, but event is DLQ"))
        return
    if event["status"] == "PENDING" and not can_pick:
        found.append(Divergence("STATE_MISMATCH", key,
                                f"item {state} with {json_attempts}/{max_attempts} attempts not pickable by the queue, "
                                "but PENDING event is still claimable"))
        return
    if event["status"] != "PENDING" or not can_pick:
        return

    json_left = max_attempts - json_attempts
    event_max = max_attempts if event.get("max_attempts") is None else int(event["max_attempts"])
    event_left = event_max - int(event.get("attempt") or 0)
    if event_left <= 0:
        found.append(Divergence("STATE_MISMATCH", key,
                                f"PENDING event not claimable (max_attempts={event_max}, "
                                f"attempt={int(event.get('attempt') or 0)}) but queue item has {json_left} attempt(s) left"))
        return
    if event_left != json_left:
        found.append(Divergence("STATE_MISMATCH", key,
                                f"attempt budget differs: queue has {json_left}, event has {event_left}"))

    retry_at = _epoch(item.get("retry_after"))
    json_ready = max(retry_at or 0.0, t_now)
    event_ready = max(float(event.get("available_at") or 0), t_now)
    if event_ready < json_ready - 1:
        found.append(Divergence("STATE_MISMATCH", key,
                                "queue waits for `retry_after` but the event is available earlier"))
    elif event_ready > json_ready + LATE_TOLERANCE_S:
        found.append(Divergence("STATE_MISMATCH", key,
                                f"event available {int(event_ready - json_ready)}s after the queue could run it "
                                f"(tolerance {LATE_TOLERANCE_S}s)"))


def read_queue(path: str | Path) -> list[dict[str, Any]]:
    """The queue file must exist and carry an explicit `queue` list. Anything else is an error."""
    p = Path(path)
    if not p.is_file():
        raise DivergenceInputError(f"queue file missing: {p.name}")
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise DivergenceInputError(f"queue file unreadable ({type(exc).__name__}): {p.name}") from exc
    queue = data.get("queue") if isinstance(data, dict) else None
    if not isinstance(queue, list) or any(not isinstance(i, dict) for i in queue):
        raise DivergenceInputError(f"queue file without a `queue` list of objects: {p.name}")
    for n, item in enumerate(queue):
        problem = invalid_item(item)
        if problem:
            raise DivergenceInputError(f"invalid queue item #{n}: {problem}")
    return queue


def _is_wal(path: Path) -> bool:
    """True when the database file is in WAL mode (header byte 18 == 2).

    SQLite creates `-shm`/`-wal` sidecars when a WAL database is opened read-only,
    so a WAL store is opened `immutable=1`, which takes no locks and creates nothing.
    That is only sound when the WAL holds no frames: a non-empty `-wal` means a writer
    is open or a crash left uncheckpointed data, and the reader refuses to guess."""
    with path.open("rb") as fh:
        header = fh.read(20)
    if len(header) < 20 or header[:16] != SQLITE_MAGIC or header[18] != 2:
        return False
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise DivergenceInputError("event store has a non-empty WAL: a writer is open or data is not checkpointed; "
                                   "quiesce writers and retry")
    return True


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Events from the store, opened read-only. Never creates the file, schema or rows."""
    p = Path(path)
    if not p.is_file():
        raise DivergenceInputError(f"event store missing: {p.name}")
    uri = p.resolve().as_uri() + ("?mode=ro&immutable=1" if _is_wal(p) else "?mode=ro")
    try:
        db = sqlite3.connect(uri, uri=True, timeout=15)
    except sqlite3.Error as exc:
        raise DivergenceInputError(f"event store cannot be opened read-only: {exc}") from exc
    try:
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id")]
        except sqlite3.Error as exc:
            raise DivergenceInputError(f"event store has no readable `events` table: {exc}") from exc
    finally:
        db.close()


def detect(queue_path: str | Path, store_path: str | Path, *, now: float | None = None,
           max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[Divergence]:
    """Read both sources and compare them. Raises DivergenceInputError when either is unusable."""
    queue = read_queue(queue_path)
    events = read_events(store_path)
    return compare(queue, events, now=now, max_attempts=max_attempts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only divergence check: JSON queue vs EventStore.")
    ap.add_argument("--queue", required=True, help="path to the JSON queue file")
    ap.add_argument("--store", required=True, help="path to the SQLite EventStore")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ns = ap.parse_args(argv)
    try:
        found = detect(ns.queue, ns.store)
    except DivergenceInputError as exc:
        if ns.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        else:
            print(f"error: {exc}")
        return 2
    if ns.json:
        print(json.dumps({"ok": not found, "divergences": [d.as_dict() for d in found]}, ensure_ascii=True, indent=1))
    else:
        for d in found:
            print(f"{d.code:18} {d.key}: {d.detail}")
        print(f"divergence: {len(found)} finding(s)")
    return 0 if not found else 1


if __name__ == "__main__":
    sys.exit(main())
