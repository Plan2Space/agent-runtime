"""Quiescence gate (issue #19, FP-062). Read-only proof that no work is active.

Contract:
- Generic core: evidence from named sources, evaluated at one instant. Quiescent only if every
  required source is positively `idle`, with fresh evidence, and the store snapshot is known.
- Fail closed: an unreadable source, unknown state, stale evidence, a missing required source or a
  missing store snapshot all block. Nothing is inferred from the absence of WAL or from two equal reads.
- Executor-specific facts (lock file path, heartbeat file, lock byte) enter through probes the caller
  builds. The core never knows the executor's file names.
- Read-only: no process is started, stopped or killed; no store row, lock content or file is written.
"""
import json
import sqlite3
import subprocess
import sys
import textwrap

from agent_runtime import divergence as dv
from agent_runtime import quiescence as q
from agent_runtime.event_store import EventStore

NOW = 1_800_000_000.0
REQUIRED = frozenset({"store", "executor_lock", "bridge_lock", "executor_heartbeat"})


def _store(tmp_path, *, lease_until=None, status="PENDING", producer="event_bridge",
           event_type="CORRECTION_REQUESTED"):
    db = tmp_path / "events.sqlite"
    store = EventStore(db)
    store.publish(event_type, "k:1", "pr:1", {"pr": 1}, producer=producer)
    if status != "PENDING" or lease_until is not None:
        conn = sqlite3.connect(db)
        try:
            conn.execute("UPDATE events SET status=?, lease_until=?, lease_owner='x', lease_token='t'",
                         (status, lease_until or 0))
            conn.commit()
        finally:
            conn.close()
    return db


# --- store evidence: all events, leases, snapshot ------------------------------

def test_empty_drained_store_is_idle_and_has_a_snapshot_id(tmp_path):
    db = tmp_path / "events.sqlite"
    EventStore(db)
    ev = q.store_probe(db, require_drained=True)(NOW)
    assert ev.status == "idle" and ev.snapshot_id != ""


def test_live_lease_on_a_running_event_is_busy(tmp_path):
    db = _store(tmp_path, status="RUNNING", lease_until=NOW + 60)
    ev = q.store_probe(db)(NOW)
    assert ev.status == "busy" and "lease" in ev.detail


def test_expired_lease_on_a_running_event_is_still_busy_recoverable_work(tmp_path):
    db = _store(tmp_path, status="RUNNING", lease_until=NOW - 60)
    assert q.store_probe(db)(NOW).status == "busy"


def test_sqlite_only_event_without_a_json_counterpart_is_considered(tmp_path):
    # The gate reads every event, not only the mirror: a coordinator-produced RUNNING event counts.
    db = _store(tmp_path, status="RUNNING", lease_until=NOW + 60, producer="coordinator",
                event_type="IMPLEMENT_REQUESTED")
    assert q.store_probe(db)(NOW).status == "busy"


def test_pending_work_blocks_when_the_store_must_be_drained(tmp_path):
    db = _store(tmp_path, status="PENDING")
    assert q.store_probe(db, require_drained=True)(NOW).status == "busy"
    assert q.store_probe(db, require_drained=False)(NOW).status == "idle"


def test_store_evidence_snapshot_id_changes_when_content_changes(tmp_path):
    db = _store(tmp_path, status="PENDING")
    before = q.store_probe(db, require_drained=False)(NOW).snapshot_id
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE events SET attempt=attempt+1")
        conn.commit()
    finally:
        conn.close()
    after = q.store_probe(db, require_drained=False)(NOW).snapshot_id
    assert before != after


def test_missing_store_is_unknown_and_is_not_created(tmp_path):
    db = tmp_path / "events.sqlite"
    ev = q.store_probe(db)(NOW)
    assert ev.status == "unknown"
    assert not db.exists()


def test_store_changed_during_the_read_is_unknown_not_idle(tmp_path, monkeypatch):
    # Concurrency: a writer commits while the store is read. The consistent read refuses; the gate blocks.
    db = _store(tmp_path, status="PENDING")

    def changed(path):
        raise dv.DivergenceInputError("event store changed during the read")
    monkeypatch.setattr(dv, "read_events", changed)
    assert q.store_probe(db, require_drained=False)(NOW).status == "unknown"


def test_non_empty_wal_is_unknown_even_if_the_rows_look_idle(tmp_path):
    db = tmp_path / "events.sqlite"
    EventStore(db)
    (tmp_path / "events.sqlite-wal").write_bytes(b"x" * 64)
    assert q.store_probe(db, require_drained=False)(NOW).status == "unknown"


# --- lock evidence: executor and bridge active ----------------------------------

HOLD_LOCK = textwrap.dedent("""
    import os, sys, time
    path, ready = sys.argv[1], sys.argv[2]
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    if os.name == "nt":
        import msvcrt
        os.write(fd, b"\\0" * 4200)
        os.lseek(fd, 4096, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
    open(ready, "w").close()
    time.sleep(30)
""")


def _holder(tmp_path, name):
    lock = tmp_path / f"{name}.lock"
    ready = tmp_path / f"{name}.ready"
    proc = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, str(lock), str(ready)])
    for _ in range(300):
        if ready.exists():
            break
        import time
        time.sleep(0.02)
    assert ready.exists(), "holder did not start"
    return proc, lock


def test_executor_holding_its_lock_is_busy(tmp_path):
    proc, lock = _holder(tmp_path, "executor")
    try:
        ev = q.lock_probe("executor_lock", lock, pos=4096)(NOW)
        assert ev.status == "busy"
    finally:
        proc.kill()
        proc.wait()


def test_bridge_holding_its_lock_is_busy(tmp_path):
    proc, lock = _holder(tmp_path, "bridge")
    try:
        assert q.lock_probe("bridge_lock", lock, pos=4096)(NOW).status == "busy"
    finally:
        proc.kill()
        proc.wait()


def test_free_lock_is_idle_and_the_probe_leaves_the_file_as_it_was(tmp_path):
    lock = tmp_path / "executor.lock"
    lock.write_bytes(b"\0" * 4200)
    before = lock.read_bytes()
    assert q.lock_probe("executor_lock", lock, pos=4096)(NOW).status == "idle"
    assert lock.read_bytes() == before


def test_missing_lock_file_is_unknown_and_is_not_created(tmp_path):
    lock = tmp_path / "never.lock"
    assert q.lock_probe("executor_lock", lock, pos=4096)(NOW).status == "unknown"
    assert not lock.exists()


# --- heartbeat evidence ----------------------------------------------------------

def _hb(tmp_path, ts, state="running"):
    p = tmp_path / "hb.json"
    p.write_text(json.dumps({"ts": ts, "state": state}), encoding="utf-8")
    return p


def test_recent_running_heartbeat_is_busy(tmp_path):
    p = _hb(tmp_path, NOW - 5)
    assert q.heartbeat_probe("executor_heartbeat", p, max_age_s=60)(NOW).status == "busy"


def test_recent_stopped_heartbeat_is_idle(tmp_path):
    p = _hb(tmp_path, NOW - 5, state="stopped")
    assert q.heartbeat_probe("executor_heartbeat", p, max_age_s=60)(NOW).status == "idle"


def test_stale_running_heartbeat_is_unknown_not_idle(tmp_path):
    # Obsolete evidence cannot prove the process stopped.
    p = _hb(tmp_path, NOW - 3600)
    assert q.heartbeat_probe("executor_heartbeat", p, max_age_s=60)(NOW).status == "unknown"


def test_stale_stopped_heartbeat_is_unknown(tmp_path):
    p = _hb(tmp_path, NOW - 3600, state="stopped")
    assert q.heartbeat_probe("executor_heartbeat", p, max_age_s=60)(NOW).status == "unknown"


def test_future_heartbeat_is_unknown(tmp_path):
    p = _hb(tmp_path, NOW + 3600)
    assert q.heartbeat_probe("executor_heartbeat", p, max_age_s=60)(NOW).status == "unknown"


def test_missing_or_garbled_heartbeat_is_unknown(tmp_path):
    assert q.heartbeat_probe("executor_heartbeat", tmp_path / "x.json", max_age_s=60)(NOW).status == "unknown"
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\x00\x00 nao e json")
    assert q.heartbeat_probe("executor_heartbeat", bad, max_age_s=60)(NOW).status == "unknown"


# --- evaluation: the combined verdict -------------------------------------------

def _all_idle_probes(tmp_path):
    db = tmp_path / "events.sqlite"
    EventStore(db)
    lock_b = tmp_path / "bridge.lock"
    lock_b.write_bytes(b"\0" * 4200)
    lock_e = tmp_path / "executor.lock"
    lock_e.write_bytes(b"\0" * 4200)
    hb = _hb(tmp_path, NOW, state="stopped")
    return [
        q.store_probe(db, require_drained=True, name="store"),
        q.lock_probe("executor_lock", lock_e, pos=4096),
        q.lock_probe("bridge_lock", lock_b, pos=4096),
        q.heartbeat_probe("executor_heartbeat", hb, max_age_s=60),
    ]


def test_all_sources_idle_and_fresh_is_quiescent_with_a_report(tmp_path):
    report = q.evaluate(_all_idle_probes(tmp_path), now=NOW, required=REQUIRED)
    assert report.quiescent is True
    assert list(report.reasons) == []
    body = json.loads(json.dumps(report.to_dict()))  # serialisable and auditable
    assert body["quiescent"] is True and body["snapshot_id"] and len(body["sources"]) == 4


def test_one_busy_source_blocks_and_names_the_reason(tmp_path):
    probes = _all_idle_probes(tmp_path)
    proc, lock = _holder(tmp_path, "executor2")
    try:
        probes[1] = q.lock_probe("executor_lock", lock, pos=4096)
        report = q.evaluate(probes, now=NOW, required=REQUIRED)
        assert report.quiescent is False
        assert any("executor_lock" in r and "busy" in r for r in report.reasons)
    finally:
        proc.kill()
        proc.wait()


def test_a_probe_that_raises_becomes_unknown_and_blocks_without_propagating(tmp_path):
    def broken(now):
        raise RuntimeError("fonte inacessivel")
    probes = _all_idle_probes(tmp_path)
    probes.append(broken)
    report = q.evaluate(probes, now=NOW, required=REQUIRED | {"broken"})
    assert report.quiescent is False
    assert any("unknown" in r for r in report.reasons)


def test_a_required_source_that_was_not_supplied_blocks(tmp_path):
    probes = _all_idle_probes(tmp_path)[:3]  # heartbeat omitted
    report = q.evaluate(probes, now=NOW, required=REQUIRED)
    assert report.quiescent is False
    assert any("executor_heartbeat" in r and "missing" in r for r in report.reasons)


def test_no_probes_is_never_quiescent(tmp_path):
    report = q.evaluate([], now=NOW, required=REQUIRED)
    assert report.quiescent is False


def test_stale_evidence_from_an_earlier_observation_blocks(tmp_path):
    # A cached observation taken an hour before the evaluation is not proof of the present.
    def cached_idle(now):
        return q.Evidence(source="executor_lock", status="idle", observed_at=now - 3600,
                          max_age_s=60.0, detail="observacao antiga reaproveitada")
    probes = _all_idle_probes(tmp_path)
    probes[1] = cached_idle
    report = q.evaluate(probes, now=NOW, required=REQUIRED)
    assert report.quiescent is False
    assert any("stale" in r and "executor_lock" in r for r in report.reasons)


def test_evaluation_never_writes_store_or_lock_or_creates_files(tmp_path):
    probes = _all_idle_probes(tmp_path)
    before = sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir() if p.is_file())
    q.evaluate(probes, now=NOW, required=REQUIRED)
    after = sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir() if p.is_file())
    assert before == after
