"""Quiescence gate: a read-only proof that no work is active (issue #19, FP-062).

Generic core: each probe returns Evidence about one source at one instant. The gate is quiescent
only when:
- the store snapshot is identified (the `store` source, always, whatever `required` says),
- every required source is positively `idle`, with fresh and well-formed evidence,
- no source is missing, duplicated, busy or unknown, and `required` is not empty.

Fail closed:
- A probe that raises, returns invalid evidence, or reports an unknown status blocks, and the reason
  names the source.
- Evidence older than its max age, or from the future, blocks.
- Non-finite instants, timings or offsets block. They never raise out of `evaluate`.
- The store read refuses if the database changed during the read (see divergence.read_events).

Not a proof by itself:
- The absence of a WAL file, or two equal reads, is never taken as quiescence.
- Transactions a producer has open but not committed are invisible to any reader. Only the
  producer's own lock (executor and bridge) can exclude them, which is why the lock probes exist.
- A lock probe takes and releases the lock within the call. No file content is written and no file
  is created, but a starting process can see the lock busy for that instant.
- A lock released by a process that just died can read as busy for a moment on Windows. That is a
  false busy, which fails closed.

Executor-specific facts (lock file, lock byte offset, heartbeat file) enter through probes the caller
builds. The offset is the lock owner's contract: each owner names its own byte. The core knows no
executor file names. Read-only: nothing is started, stopped or killed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runtime import divergence as dv

IDLE = "idle"
BUSY = "busy"
UNKNOWN = "unknown"
STORE_SOURCE = "store"
_STATUSES = frozenset({IDLE, BUSY, UNKNOWN})
_STORE_STATUSES = frozenset({"PENDING", "RUNNING", "DONE", "DLQ", "SUPERSEDED", "RECORDED"})
CLOCK_SKEW_S = 1.0
# Errors that mean "another process holds the lock". Names, not numbers, so each platform's value is used.
_LOCK_HELD_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN})
_MAX_TS_MAGNITUDE = 1e15  # epoch seconds far beyond any real heartbeat; larger values are malformed
_MAX_LOCK_OFFSET = 2**31 - 1  # a lock byte beyond any real lock file is a configuration error


@dataclass(frozen=True)
class Evidence:
    source: str
    status: str
    observed_at: float
    max_age_s: float
    detail: str
    snapshot_id: str = ""


Probe = Callable[[float], Evidence]


@dataclass(frozen=True)
class Report:
    quiescent: bool
    now: float | None
    snapshot_id: str
    required: tuple[str, ...]
    reasons: tuple[str, ...]
    sources: tuple[Evidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "quiescent": self.quiescent,
            "now": self.now,
            "snapshot_id": self.snapshot_id,
            "required": list(self.required),
            "reasons": list(self.reasons),
            "sources": [
                {"source": e.source, "status": e.status, "observed_at": e.observed_at,
                 "max_age_s": e.max_age_s, "detail": e.detail, "snapshot_id": e.snapshot_id}
                for e in self.sources
            ],
        }


def _is_finite_real(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _check_timing(max_age_s: float) -> None:
    if not _is_finite_real(max_age_s) or max_age_s <= 0:
        raise ValueError("max_age_s must be a finite positive number")


def _evidence_problem(ev: object) -> str:
    """'' when the evidence is well formed, else what is wrong (never the content)."""
    if not isinstance(ev, Evidence):
        return "returned no evidence"
    if not isinstance(ev.source, str) or not ev.source:
        return "returned evidence without a source"
    if not isinstance(ev.status, str) or ev.status not in _STATUSES:
        return "returned an unknown status"
    if not _is_finite_real(ev.observed_at) or not _is_finite_real(ev.max_age_s) or ev.max_age_s <= 0:
        return "returned non-finite or invalid timing"
    if not isinstance(ev.snapshot_id, str):
        return "returned an invalid snapshot id"
    if not isinstance(ev.detail, str):
        return "returned a non-text detail"
    return ""


def evaluate(probes: Sequence[Probe], *, now: float, required: frozenset[str]) -> Report:
    """Run every probe at `now` and decide. Never raises for a source or input problem: it blocks instead."""
    required_sorted = tuple(sorted(required))
    if not _is_finite_real(now):
        return Report(False, None, "", required_sorted, ("unknown: evaluation instant is not a finite number",), ())
    reasons: list[str] = []
    if not required:
        reasons.append("unknown: no required sources declared")
    if not probes:
        reasons.append("unknown: no evidence supplied")

    evidences: list[Evidence] = []
    seen: set[str] = set()
    for probe in probes:
        source = getattr(probe, "source", getattr(probe, "__name__", "probe"))
        try:
            ev = probe(now)
            problem = _evidence_problem(ev)
        except Exception as exc:  # noqa: BLE001 - fail closed: a probe that cannot answer blocks
            reasons.append(f"unknown: {source} probe raised {type(exc).__name__}")
            continue
        if problem:
            reasons.append(f"unknown: {source} {problem}")
            continue
        if ev.source in seen:
            reasons.append(f"unknown: duplicate evidence for source {ev.source}")
            continue
        seen.add(ev.source)
        evidences.append(ev)
        age = now - ev.observed_at
        if not math.isfinite(age):
            reasons.append(f"unknown: {ev.source} evidence age is not finite")
        elif age < -CLOCK_SKEW_S:
            reasons.append(f"unknown: {ev.source} evidence from the future")
        elif age > ev.max_age_s:
            reasons.append(f"unknown: {ev.source} stale evidence (age {age:.1f}s > max {ev.max_age_s:g}s)")
        elif ev.status == UNKNOWN:
            reasons.append(f"unknown: {ev.source}: {ev.detail}")
        elif ev.status == BUSY:
            reasons.append(f"busy: {ev.source}: {ev.detail}")

    for name in sorted(required - seen):
        reasons.append(f"unknown: required source {name} missing")
    store = next((e for e in evidences if e.source == STORE_SOURCE and e.snapshot_id), None)
    if store is None:
        reasons.append("unknown: store snapshot not identified")
    snapshot_id = store.snapshot_id if store is not None else ""
    return Report(quiescent=not reasons, now=now, snapshot_id=snapshot_id, required=required_sorted,
                  reasons=tuple(reasons), sources=tuple(evidences))


def _tag(probe: Probe, source: str) -> Probe:
    probe.source = source  # type: ignore[attr-defined]  # read by evaluate() to name the source in reasons
    return probe


def store_probe(store_path: str | Path, *, require_drained: bool = True, max_age_s: float = 60.0) -> Probe:
    """Evidence from every event of the store, not only the JSON mirror. Always named `store`.

    RUNNING events (live or expired lease) are busy: a live lease is work in progress and an expired
    one is work the reaper has yet to resolve. PENDING events are busy when the store must be drained.
    Unknown statuses are unknown. The snapshot id is a hash of the exact rows that were read."""
    _check_timing(max_age_s)
    p = Path(store_path)

    def probe(now: float) -> Evidence:
        if not p.is_file():
            return Evidence(STORE_SOURCE, UNKNOWN, now, max_age_s, "event store missing")
        try:
            events = dv.read_events(p)
        except dv.DivergenceInputError as exc:
            return Evidence(STORE_SOURCE, UNKNOWN, now, max_age_s, f"store unreadable: {exc}")
        unknown = sorted({str(e.get("status")) for e in events} - _STORE_STATUSES)
        if unknown:
            return Evidence(STORE_SOURCE, UNKNOWN, now, max_age_s, f"unknown event status: {unknown}")
        snapshot = hashlib.sha256(json.dumps(events, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        running = [e for e in events if e["status"] == "RUNNING"]
        live = sum(1 for e in running if (e.get("lease_until") or 0) > now)
        pending = sum(1 for e in events if e["status"] == "PENDING")
        if running:
            return Evidence(STORE_SOURCE, BUSY, now, max_age_s,
                            f"{len(running)} RUNNING event(s): {live} with live lease, {len(running) - live} expired",
                            snapshot)
        if require_drained and pending:
            return Evidence(STORE_SOURCE, BUSY, now, max_age_s, f"{pending} PENDING event(s) not drained", snapshot)
        return Evidence(STORE_SOURCE, IDLE, now, max_age_s, f"{len(events)} event(s), none active", snapshot)

    return _tag(probe, STORE_SOURCE)


def _byte_held(fd: int, pos: int) -> bool:
    """Another process holds the lock? Takes and releases it; writes nothing to the file.

    Windows: exclusive byte lock at `pos`. POSIX: exclusive flock on the whole file (`pos` unused there);
    LOCK_EX conflicts with both shared and exclusive holders, so no holder goes unseen."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, pos, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # raises OSError if held
        os.lseek(fd, pos, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return False
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError if any holder exists
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def lock_probe(name: str, lock_path: str | Path, *, pos: int, max_age_s: float = 60.0) -> Probe:
    """Is the process lock free at this instant? Never creates the lock file.

    `pos` is the byte the lock owner holds on Windows. Each owner names its own: the outbox lock holds
    byte 0, the executor locks byte 0 (event_executor.py). A wrong offset would see a busy owner as free,
    so the caller passes the owner's byte, never a default. On POSIX the owner must use flock on the whole
    file (the executor and the outbox do); `pos` is then ignored, and a lockf/fcntl owner would be invisible.
    A missing lock file is unknown, not idle: it cannot tell "never ran" from "cleaned up"."""
    if isinstance(pos, bool) or not isinstance(pos, int) or not 0 <= pos <= _MAX_LOCK_OFFSET:
        raise ValueError("pos must be a non-negative integer offset within the file-size limit")
    _check_timing(max_age_s)
    p = Path(lock_path)

    def probe(now: float) -> Evidence:
        try:
            fd = os.open(p, os.O_RDONLY)  # read-only: a missing file raises, nothing is created
        except FileNotFoundError:
            return Evidence(name, UNKNOWN, now, max_age_s, "lock file absent: cannot tell never-run from cleaned up")
        except OSError as exc:
            return Evidence(name, UNKNOWN, now, max_age_s, f"lock file unreadable ({type(exc).__name__})")
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return Evidence(name, UNKNOWN, now, max_age_s, "lock path is not a regular file")
            try:
                _byte_held(fd, pos)
                return Evidence(name, IDLE, now, max_age_s, "lock free at observation")
            except OSError as exc:
                if isinstance(exc, BlockingIOError) or exc.errno in _LOCK_HELD_ERRNOS:
                    return Evidence(name, BUSY, now, max_age_s, "lock held by another process")
                return Evidence(name, UNKNOWN, now, max_age_s, f"lock state not provable ({type(exc).__name__})")
        finally:
            os.close(fd)

    return _tag(probe, name)


def heartbeat_probe(name: str, heartbeat_path: str | Path, *, max_age_s: float = 60.0) -> Probe:
    """A heartbeat proves activity only while it is fresh.

    `running` and recent: busy. `stopped` and recent: idle (graceful stop). Anything stale, future,
    missing, unreadable or malformed is unknown: old evidence cannot prove the process stopped."""
    _check_timing(max_age_s)
    p = Path(heartbeat_path)

    def probe(now: float) -> Evidence:
        if not _is_finite_real(now):
            return Evidence(name, UNKNOWN, 0.0, max_age_s, "evaluation instant is not finite")
        try:
            raw = p.read_bytes()
        except FileNotFoundError:
            return Evidence(name, UNKNOWN, now, max_age_s, "heartbeat absent")
        except OSError as exc:
            return Evidence(name, UNKNOWN, now, max_age_s, f"heartbeat unreadable ({type(exc).__name__})")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return Evidence(name, UNKNOWN, now, max_age_s, "heartbeat is not valid JSON")
        if not isinstance(data, dict):
            return Evidence(name, UNKNOWN, now, max_age_s, "heartbeat root is not an object")
        ts, state = data.get("ts"), data.get("state")
        if not _is_finite_real(ts) or abs(float(ts)) > _MAX_TS_MAGNITUDE:
            return Evidence(name, UNKNOWN, now, max_age_s, "heartbeat ts is not a finite, sane number")
        if state not in ("running", "stopped"):
            return Evidence(name, UNKNOWN, now, max_age_s, "heartbeat state not recognised")
        age = now - float(ts)
        if age < -CLOCK_SKEW_S:
            return Evidence(name, UNKNOWN, now, max_age_s, "heartbeat from the future")
        if age > max_age_s:
            return Evidence(name, UNKNOWN, now, max_age_s, f"heartbeat stale (age {age:.1f}s)")
        if state == "running":
            return Evidence(name, BUSY, now, max_age_s, "recent heartbeat: process reports running")
        return Evidence(name, IDLE, now, max_age_s, "recent heartbeat: process reports stopped")

    return _tag(probe, name)
