"""Quiescence gate: a read-only proof that no work is active (issue #19, FP-062).

Generic core: each probe returns Evidence about one source at one instant. The gate is quiescent
only when every required source is positively `idle`, every piece of evidence is fresh, and the
store snapshot is identified. Anything else blocks, and the report says why.

Fail closed:
- A probe that raises, returns no evidence, or reports an unknown status blocks.
- Evidence older than its max age, or from the future, blocks.
- A required source that was not supplied blocks. No probes at all blocks.
- The store read refuses if the database changed during the read (see divergence.read_events).

Not a proof by itself:
- The absence of a WAL file, or two equal reads, is never taken as quiescence.
- A lock probe takes and releases the lock byte within the call. No file content is written and
  no file is created, but a starting process can see the lock busy for that instant.

Executor-specific facts (lock file, lock byte, heartbeat file) enter through probes the caller
builds. The core knows no executor file names. Read-only: nothing is started, stopped or killed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runtime import divergence as dv

IDLE = "idle"
BUSY = "busy"
UNKNOWN = "unknown"
_STATUSES = frozenset({IDLE, BUSY, UNKNOWN})
_STORE_STATUSES = frozenset({"PENDING", "RUNNING", "DONE", "DLQ", "SUPERSEDED", "RECORDED"})
CLOCK_SKEW_S = 1.0
_LOCK_HELD_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK, 33, 36})  # POSIX and Windows lock conflicts


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
    now: float
    snapshot_id: str
    reasons: tuple[str, ...]
    sources: tuple[Evidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "quiescent": self.quiescent,
            "now": self.now,
            "snapshot_id": self.snapshot_id,
            "reasons": list(self.reasons),
            "sources": [
                {"source": e.source, "status": e.status, "observed_at": e.observed_at,
                 "max_age_s": e.max_age_s, "detail": e.detail, "snapshot_id": e.snapshot_id}
                for e in self.sources
            ],
        }


def evaluate(probes: Sequence[Probe], *, now: float, required: frozenset[str]) -> Report:
    """Run every probe at `now` and decide. Never raises for a source problem: it blocks instead."""
    evidences: list[Evidence] = []
    reasons: list[str] = []
    for probe in probes:
        try:
            ev = probe(now)
        except Exception as exc:  # noqa: BLE001 - fail closed: a probe that cannot answer blocks
            reasons.append(f"unknown: probe raised {type(exc).__name__}")
            continue
        if not isinstance(ev, Evidence) or ev.status not in _STATUSES:
            reasons.append("unknown: probe returned no valid evidence")
            continue
        evidences.append(ev)
        age = now - ev.observed_at
        if age < -CLOCK_SKEW_S:
            reasons.append(f"unknown: {ev.source} evidence from the future")
        elif age > ev.max_age_s:
            reasons.append(f"unknown: {ev.source} stale evidence (age {int(age)}s > max {int(ev.max_age_s)}s)")
        elif ev.status == UNKNOWN:
            reasons.append(f"unknown: {ev.source}: {ev.detail}")
        elif ev.status == BUSY:
            reasons.append(f"busy: {ev.source}: {ev.detail}")

    if not evidences:
        reasons.append("unknown: no evidence supplied")
    seen = {e.source for e in evidences}
    for name in sorted(required - seen):
        reasons.append(f"unknown: required source {name} missing")

    store = next((e for e in evidences if e.snapshot_id), None)
    if store is None and "store" in required:
        reasons.append("unknown: store snapshot not identified")
    snapshot_id = store.snapshot_id if store is not None else ""
    return Report(quiescent=not reasons, now=now, snapshot_id=snapshot_id,
                  reasons=tuple(reasons), sources=tuple(evidences))


def store_probe(store_path: str | Path, *, name: str = "store", require_drained: bool = True,
                max_age_s: float = 60.0) -> Probe:
    """Evidence from every event of the store, not only the JSON mirror.

    RUNNING events (live or expired lease) are busy: a live lease is work in progress and an expired
    one is work the reaper has yet to resolve. PENDING events are busy when the store must be drained.
    Unknown statuses are unknown. The snapshot id is a hash of the exact rows that were read."""
    p = Path(store_path)

    def probe(now: float) -> Evidence:
        if not p.is_file():
            return Evidence(name, UNKNOWN, now, max_age_s, "event store missing")
        try:
            events = dv.read_events(p)
        except dv.DivergenceInputError as exc:
            return Evidence(name, UNKNOWN, now, max_age_s, f"store unreadable: {exc}")
        unknown = sorted({str(e.get("status")) for e in events} - _STORE_STATUSES)
        if unknown:
            return Evidence(name, UNKNOWN, now, max_age_s, f"unknown event status: {unknown}")
        snapshot = hashlib.sha256(json.dumps(events, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        running = [e for e in events if e["status"] == "RUNNING"]
        live = sum(1 for e in running if (e.get("lease_until") or 0) > now)
        pending = sum(1 for e in events if e["status"] == "PENDING")
        if running:
            return Evidence(name, BUSY, now, max_age_s,
                            f"{len(running)} RUNNING event(s): {live} with live lease, {len(running) - live} expired",
                            snapshot)
        if require_drained and pending:
            return Evidence(name, BUSY, now, max_age_s, f"{pending} PENDING event(s) not drained", snapshot)
        return Evidence(name, IDLE, now, max_age_s, f"{len(events)} event(s), none active", snapshot)

    return probe


def _byte_held(fd: int, pos: int) -> bool:
    """Another process holds the lock? Takes and releases it; writes nothing to the file."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, pos, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # raises OSError if held
        os.lseek(fd, pos, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return False
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # raises BlockingIOError if an exclusive holder exists
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def lock_probe(name: str, lock_path: str | Path, *, pos: int, max_age_s: float = 60.0) -> Probe:
    """Is the process lock free at this instant? Never creates the lock file.

    A missing lock file is unknown, not idle: it cannot tell "never ran" from "cleaned up".
    On Windows the lock is the byte `pos`; on POSIX it is the whole file (`pos` is ignored there)."""
    p = Path(lock_path)

    def probe(now: float) -> Evidence:
        try:
            fd = os.open(p, os.O_RDONLY)  # read-only: a missing file raises, nothing is created
        except FileNotFoundError:
            return Evidence(name, UNKNOWN, now, max_age_s, "lock file absent: cannot tell never-run from cleaned up")
        except OSError as exc:
            return Evidence(name, UNKNOWN, now, max_age_s, f"lock file unreadable ({type(exc).__name__})")
        try:
            try:
                _byte_held(fd, pos)
                return Evidence(name, IDLE, now, max_age_s, "lock free at observation")
            except OSError as exc:
                if exc.errno in _LOCK_HELD_ERRNOS or isinstance(exc, BlockingIOError):
                    return Evidence(name, BUSY, now, max_age_s, "lock held by another process")
                return Evidence(name, UNKNOWN, now, max_age_s, f"lock state not provable ({type(exc).__name__})")
        finally:
            os.close(fd)

    return probe


def heartbeat_probe(name: str, heartbeat_path: str | Path, *, max_age_s: float = 60.0) -> Probe:
    """A heartbeat proves activity only while it is fresh.

    `running` and recent: busy. `stopped` and recent: idle (graceful stop). Anything stale, future,
    missing, unreadable or malformed is unknown: old evidence cannot prove the process stopped."""
    p = Path(heartbeat_path)

    def probe(now: float) -> Evidence:
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
        if isinstance(ts, bool) or not isinstance(ts, int | float) or not math.isfinite(ts):
            return Evidence(name, UNKNOWN, now, max_age_s, "heartbeat ts is not a finite number")
        if state not in ("running", "stopped"):
            return Evidence(name, UNKNOWN, now, max_age_s, "heartbeat state not recognised")
        age = now - ts
        if age < -CLOCK_SKEW_S:
            return Evidence(name, UNKNOWN, now, max_age_s, "heartbeat from the future")
        if age > max_age_s:
            return Evidence(name, UNKNOWN, now, max_age_s, f"heartbeat stale (age {int(age)}s)")
        if state == "running":
            return Evidence(name, BUSY, now, max_age_s, "recent heartbeat: process reports running", "")
        return Evidence(name, IDLE, now, max_age_s, "recent heartbeat: process reports stopped", "")

    return probe
