"""Read-only divergence detector between a JSON queue mirror and the EventStore.

Issue #21 (FP-062). Replaces the sketchup-mcp `shadow_divergence` (PR #634) with
the generic core only. Executor policy (daily cap, instance lock, pause flags)
stays with the executor and is gated in the cutover issue (#19).

Contract:
- Reads the queue file and the SQLite database; never writes to either one and
  never creates a file (no store, no schema, no WAL/SHM sidecar).
- The database is not opened through `EventStore`: its `_conn` runs
  `PRAGMA journal_mode=WAL`, which writes.
- Snapshot by content: the bytes of the database and of its -wal are read before
  and after the read, and so are the queue bytes. Size and mtime are not used:
  a same-size rewrite within one mtime tick is invisible to stat(). A change in
  between means a writer committed during the read, so the result is refused.
  A rollback journal present means a transaction is in progress, also refused.
- A missing, unreadable or invalid source is an error, never an empty set and
  never `consistent`. Any unexpected failure inside detection is also an error.
- Unknown queue states are reported, never assumed equal.
- A consistent result is not cutover authorization. #19 gates the cutover with
  executor and bridge quiescence, which this module does not prove.
"""

from __future__ import annotations

import argparse
import json
import math
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
REVIEW_PREFIX = "review:"
REQUIRED_COLUMNS = frozenset({
    "id", "event_type", "idempotency_key", "job_id", "producer", "payload",
    "status", "attempt", "max_attempts", "available_at",
})

# Codes that are reported but do not make the result inconsistent (unless strict).
WARNING_CODES = frozenset({"ORPHAN_IN_STORE"})

# Executor contract values, pinned here and checked against the executor by the
# #19 gate. Changing them in one place without the other is a divergence source.
PICKABLE = frozenset({"QUEUED", "FAILED", "BLOCKED_CLI", "BLOCKED_CREDENTIALS", "BLOCKED_WORKTREE"})
RETRYABLE = frozenset({"FAILED", "BLOCKED_CLI", "BLOCKED_CREDENTIALS", "BLOCKED_WORKTREE", "BLOCKED_REVIEW_FETCH"})
FINISHED = frozenset({"DONE_AWAITING_REREVIEW", "SUPERSEDED", "REFUSED", "PARKED_UNSUPPORTED"})
EXECUTABLE_EVENT = frozenset({"PENDING", "RUNNING"})

# Expected event status for each queue state (queue state -> allowed statuses).
# BLOCKED_REVIEW_FETCH is resumed by the bridge, never picked by the executor, so a
# PENDING event for it would be work only the store could run: only DLQ is allowed.
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
    "BLOCKED_REVIEW_FETCH": frozenset({"DLQ"}),
}

CANONICAL_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class DivergenceInputError(ValueError):
    """A source cannot be compared safely. Reported as an error, not a divergence."""


@dataclass(frozen=True)
class Divergence:
    code: str
    key: str
    detail: str

    @property
    def is_warning(self) -> bool:
        return self.code in WARNING_CODES

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "key": self.key, "detail": self.detail}


def mirror_key(item: Mapping[str, Any]) -> str:
    """The idempotency key the bridge publishes: `review:<id>:<pr>:<head_sha>`.

    The `review:` prefix is removed explicitly, so an id containing ':' cannot
    collide with another item. `invalid_item` guarantees the prefix is present."""
    event_id = str(item.get("event_id", ""))
    review_id = event_id.removeprefix(REVIEW_PREFIX)
    return f"review:{review_id}:{item.get('pr')}:{item.get('head_sha')}"


def _canon(value: Any) -> str:
    """Canonical JSON text. Distinguishes types that Python equates: True != 1, 7.0 != 7."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def payload_mismatch(item: Mapping[str, Any], event: Mapping[str, Any]) -> str:
    """'' when the event describes the same work as the queue item, else the reason.

    The idempotency key alone is not enough: the store keeps the first payload
    published for a key, so a colliding event with different PR/head would be
    accepted silently. Values are compared by canonical JSON, not by Python equality."""
    raw = event.get("payload")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, RecursionError):
        return "payload is not JSON"
    if not isinstance(payload, dict):
        return "event has no readable payload"
    review_id = str(item.get("event_id", ""))[len(REVIEW_PREFIX):]
    if event.get("job_id") != f"pr:{item.get('pr')}":
        return f"job_id {event.get('job_id')!r} != 'pr:{item.get('pr')}'"
    for field, want in (("pr", item.get("pr")), ("head_sha", item.get("head_sha")),
                        ("review_id", review_id), ("issue", item.get("issue"))):
        if _canon(payload.get(field)) != _canon(want):
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


def _int_field(source: Mapping[str, Any], name: str, default: int) -> int:
    value = source.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise DivergenceInputError(f"field `{name}` is not an integer")
    return value


def _num_field(source: Mapping[str, Any], name: str, default: float) -> float:
    value = source.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise DivergenceInputError(f"field `{name}` is not a finite number")
    return float(value)


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
    if not item["event_id"].startswith(REVIEW_PREFIX) or len(item["event_id"]) == len(REVIEW_PREFIX):
        return "review `event_id` without the `review:` prefix and an id"
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
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 0:
        raise DivergenceInputError("max_attempts must be an integer >= 0")
    mirror = {
        e["idempotency_key"]: e
        for e in events
        if e.get("producer") == MIRROR_PRODUCER and e.get("event_type") == MIRROR_EVENT_TYPE
    }
    found: list[Divergence] = []
    matched: set[str] = set()
    work_seen: set[str] = set()
    t_now = time.time() if now is None else now

    for n, item in enumerate(queue):
        problem = invalid_item(item)
        if problem:
            raise DivergenceInputError(f"invalid queue item #{n}: {problem}")
        key, state = item.get("key"), item.get("state")
        # Checked before any lookup: a RUNNING item is never hidden by a payload or
        # missing-event branch below.
        if state == "RUNNING":
            found.append(Divergence("JSON_RUNNING", str(key),
                                    "item RUNNING in the JSON queue: drain it before cutover"))
        if item.get("kind") != "review":
            continue

        work = mirror_key(item)
        if work in work_seen:
            # Two queue items for the same review, PR and head with different keys: one
            # event would stand for both and the second item's state would go unchecked.
            raise DivergenceInputError(f"duplicate review work item in queue at item #{n} (same review, PR and head)")
        work_seen.add(work)

        event = mirror.get(work)
        if event is None:
            found.append(Divergence("MISSING_IN_STORE", key, f"item {state} without event {work}"))
            continue
        matched.add(event["idempotency_key"])

        wrong = payload_mismatch(item, event)
        if wrong:
            found.append(Divergence("PAYLOAD_MISMATCH", key, f"same key but different work: {wrong}"))
            continue

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
    json_attempts = _int_field(item, "attempts", 0)
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
    event_max = _int_field(event, "max_attempts", max_attempts)
    event_attempt = _int_field(event, "attempt", 0)
    event_left = event_max - event_attempt
    if event_left <= 0:
        found.append(Divergence("STATE_MISMATCH", key,
                                f"PENDING event not claimable (max_attempts={event_max}, "
                                f"attempt={event_attempt}) but queue item has {json_left} attempt(s) left"))
        return
    if event_left != json_left:
        found.append(Divergence("STATE_MISMATCH", key,
                                f"attempt budget differs: queue has {json_left}, event has {event_left}"))

    retry_at = _epoch(item.get("retry_after"))
    json_ready = max(retry_at or 0.0, t_now)
    event_ready = max(_num_field(event, "available_at", 0.0), t_now)
    if event_ready < json_ready - 1:
        found.append(Divergence("STATE_MISMATCH", key,
                                "queue waits for `retry_after` but the event is available earlier"))
    elif event_ready > json_ready + LATE_TOLERANCE_S:
        found.append(Divergence("STATE_MISMATCH", key,
                                f"event available {int(event_ready - json_ready)}s after the queue could run it "
                                f"(tolerance {LATE_TOLERANCE_S}s)"))


def _read_queue_bytes(path: str | Path) -> bytes:
    p = Path(path)
    try:
        if not p.is_file():
            raise DivergenceInputError(f"queue file missing: {p.name}")
        return p.read_bytes()
    except OSError as exc:
        raise DivergenceInputError(f"queue file unreadable ({type(exc).__name__}): {p.name}") from exc


def _parse_queue(raw: bytes, label: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise DivergenceInputError(f"queue file is not valid JSON ({type(exc).__name__}): {label}") from exc
    queue = data.get("queue") if isinstance(data, dict) else None
    if not isinstance(queue, list) or any(not isinstance(i, dict) for i in queue):
        raise DivergenceInputError(f"queue file without a `queue` list of objects: {label}")
    seen: set[str] = set()
    for n, item in enumerate(queue):
        problem = invalid_item(item)
        if problem:
            raise DivergenceInputError(f"invalid queue item #{n}: {problem}")
        if item.get("kind") == "review":
            if item["key"] in seen:
                raise DivergenceInputError(f"duplicate review key in queue at item #{n}")
            seen.add(item["key"])
    return queue


def read_queue(path: str | Path) -> list[dict[str, Any]]:
    """The queue file must exist and carry an explicit `queue` list. Anything else is an error.

    A UTF-8 BOM is accepted (`utf-8-sig`). Duplicate review keys are an error: a
    duplicate would be compared once and hide the second item."""
    return _parse_queue(_read_queue_bytes(path), Path(path).name)


def _snapshot(path: Path) -> tuple[bytes, bytes | None]:
    """Bytes of the main database and of its -wal (None when absent). Read-only.

    Compared before and after the read. A rollback journal is refused: its presence
    means a transaction is in progress, and its content is not part of this snapshot."""
    if path.with_name(path.name + "-journal").exists():
        raise DivergenceInputError("event store has a rollback journal: a transaction is in progress; "
                                   "quiesce writers and retry")
    db = path.read_bytes()
    try:
        wal = path.with_name(path.name + "-wal").read_bytes()
    except FileNotFoundError:
        wal = None
    return db, wal


def _is_wal(path: Path) -> bool:
    """True when the database file is in WAL mode (header byte 18 == 2).

    A WAL store is opened `immutable=1`: no locks, no `-shm` created. That reads only
    the main file, so it is consistent only if the WAL holds no frames. A non-empty
    `-wal` is refused here, and `read_events` compares the snapshot again afterwards."""
    with path.open("rb") as fh:
        header = fh.read(20)
    if len(header) < 20 or header[:16] != SQLITE_MAGIC or header[18] != 2:
        return False
    try:
        wal_size = path.with_name(path.name + "-wal").stat().st_size
    except FileNotFoundError:
        wal_size = 0
    if wal_size > 0:
        raise DivergenceInputError("event store has a non-empty WAL: a writer is open or data is not checkpointed; "
                                   "quiesce writers and retry")
    return True


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Events from the store, read without writing, and only if the snapshot did not move."""
    p = Path(path)
    try:
        if not p.is_file():
            raise DivergenceInputError(f"event store missing: {p.name}")
        before = _snapshot(p)
        wal_mode = _is_wal(p)
        uri = p.resolve().as_uri() + ("?mode=ro&immutable=1" if wal_mode else "?mode=ro")
        rows = _select_events(uri)
        after = _snapshot(p)
    except OSError as exc:
        raise DivergenceInputError(f"event store unreadable ({type(exc).__name__}): {p.name}") from exc
    if before != after:
        raise DivergenceInputError("event store changed during the read (a writer committed or checkpointed): "
                                   "quiesce writers and retry")
    return rows


def _select_events(uri: str) -> list[dict[str, Any]]:
    try:
        db = sqlite3.connect(uri, uri=True, timeout=15)
    except sqlite3.Error as exc:
        raise DivergenceInputError(f"event store cannot be opened read-only: {exc}") from exc
    try:
        db.row_factory = sqlite3.Row
        try:
            cols = {r["name"] for r in db.execute("PRAGMA table_info(events)")}
            if not cols:
                raise DivergenceInputError("event store has no `events` table")
            missing = REQUIRED_COLUMNS - cols
            if missing:
                raise DivergenceInputError(f"event store `events` table lacks columns: {sorted(missing)}")
            return [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id")]
        except sqlite3.Error as exc:
            raise DivergenceInputError(f"event store is not a readable SQLite database: {exc}") from exc
    finally:
        db.close()


def detect(queue_path: str | Path, store_path: str | Path, *, now: float | None = None,
           max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[Divergence]:
    """Read both sources and compare them. Raises DivergenceInputError when either is unusable."""
    raw_before = _read_queue_bytes(queue_path)
    queue = _parse_queue(raw_before, Path(queue_path).name)
    events = read_events(store_path)
    if _read_queue_bytes(queue_path) != raw_before:
        raise DivergenceInputError("queue file changed during the read (a bridge cycle wrote it): "
                                   "quiesce the bridge and retry")
    try:
        return compare(queue, events, now=now, max_attempts=max_attempts)
    except DivergenceInputError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise DivergenceInputError(f"inconsistent input ({type(exc).__name__})") from exc


def main(argv: list[str] | None = None) -> int:
    """Exit 0: consistent (warnings allowed). Exit 1: divergence. Exit 2: input error or internal failure."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    ap = argparse.ArgumentParser(description="Read-only divergence check: JSON queue vs EventStore.")
    ap.add_argument("--queue", required=True, help="path to the JSON queue file")
    ap.add_argument("--store", required=True, help="path to the SQLite EventStore")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--strict", action="store_true", help="warnings (terminal orphans) also fail")
    ns = ap.parse_args(argv)
    try:
        found = detect(ns.queue, ns.store)
    except DivergenceInputError as exc:
        return _report_error(ns.json, str(exc))
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary: an internal failure is never exit 1
        return _report_error(ns.json, f"internal error ({type(exc).__name__})")
    errors = [d for d in found if not d.is_warning]
    warnings = [d for d in found if d.is_warning]
    failed_on = "divergence" if errors else ("warning (strict)" if ns.strict and warnings else None)
    consistent = failed_on is None
    if ns.json:
        print(json.dumps({"consistent": consistent, "authorizes_cutover": False, "failed_on": failed_on,
                          "divergences": [d.as_dict() for d in errors],
                          "warnings": [d.as_dict() for d in warnings]}, ensure_ascii=True, indent=1))
    else:
        for d in found:
            print(f"{d.code:18} {d.key}: {d.detail}")
        print(f"divergence: {len(errors)} divergence(s), {len(warnings)} warning(s)")
    return 0 if consistent else 1


def _report_error(as_json: bool, message: str) -> int:
    if as_json:
        print(json.dumps({"consistent": False, "authorizes_cutover": False, "error": message}, ensure_ascii=True))
    else:
        print(f"error: {message}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
