import json
import sqlite3
from pathlib import Path

import pytest

from agent_runtime import divergence as dv
from agent_runtime.event_store import EventStore

NOW = 1_800_000_000.0


def item(state="QUEUED", pr=7, head="abc123", attempts=0, retry_after=None, review="r42", key="k1"):
    return {"kind": "review", "key": key, "state": state, "pr": pr, "head_sha": head,
            "event_id": f"review:{review}", "issue": 9, "attempts": attempts, "retry_after": retry_after}


def ev_for(it, status="PENDING", attempt=0, max_attempts=3, available_at=NOW, **over):
    payload = {"pr": it["pr"], "head_sha": it["head_sha"], "review_id": it["event_id"][len("review:"):], "issue": it["issue"]}
    e = {"idempotency_key": dv.mirror_key(it), "producer": "event_bridge", "event_type": "CORRECTION_REQUESTED",
         "job_id": f"pr:{it['pr']}", "payload": json.dumps(payload), "status": status,
         "attempt": attempt, "max_attempts": max_attempts, "available_at": available_at}
    e.update(over)
    return e


def codes(found):
    return sorted(d.code for d in found)


def write_queue(path: Path, queue, *, bom=False):
    text = json.dumps({"queue": queue})
    path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")


def make_store(path: Path, it, status="PENDING"):
    store = EventStore(path)
    store.publish("CORRECTION_REQUESTED", dv.mirror_key(it), f"pr:{it['pr']}",
                  {"pr": it["pr"], "head_sha": it["head_sha"], "review_id": it["event_id"][len("review:"):], "issue": it["issue"]},
                  producer="event_bridge")
    if status != "PENDING":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE events SET status=?", (status,))
    return store


# --- pure comparison ---------------------------------------------------------

def test_concordant_state_reports_nothing():
    it = item("QUEUED")
    assert dv.compare([it], [ev_for(it)], now=NOW) == []


def test_done_item_with_done_event_is_concordant():
    it = item("DONE_AWAITING_REREVIEW", attempts=1)
    assert dv.compare([it], [ev_for(it, status="DONE", attempt=1)], now=NOW) == []


def test_item_without_mirror_event_is_missing_in_store():
    assert codes(dv.compare([item()], [], now=NOW)) == ["MISSING_IN_STORE"]


def test_pending_event_without_queue_item_is_orphan_executable():
    it = item()
    assert codes(dv.compare([], [ev_for(it)], now=NOW)) == ["ORPHAN_EXECUTABLE"]


def test_terminal_event_without_queue_item_is_a_warning_not_a_divergence():
    it = item()
    found = dv.compare([], [ev_for(it, status="DONE")], now=NOW)
    assert codes(found) == ["ORPHAN_IN_STORE"]
    assert all(d.is_warning for d in found)


def test_finished_item_with_pending_event_is_stale_pending():
    it = item("SUPERSEDED")
    assert codes(dv.compare([it], [ev_for(it, status="PENDING")], now=NOW)) == ["STALE_PENDING"]


def test_running_queue_item_blocks_as_json_running():
    it = item("RUNNING")
    assert "JSON_RUNNING" in codes(dv.compare([it], [ev_for(it)], now=NOW))


def test_running_item_is_not_hidden_by_a_payload_mismatch():
    it = item("RUNNING")
    wrong = ev_for(it)
    wrong["payload"] = json.dumps({"pr": 7, "head_sha": "OTHER", "review_id": "r42", "issue": 9})
    assert codes(dv.compare([it], [wrong], now=NOW)) == ["JSON_RUNNING", "PAYLOAD_MISMATCH"]


def test_non_review_running_item_blocks_as_json_running():
    comment = {"kind": "comment", "key": "c1", "state": "RUNNING"}
    assert codes(dv.compare([comment], [], now=NOW)) == ["JSON_RUNNING"]


def test_event_with_same_key_but_other_head_is_payload_mismatch():
    it = item()
    wrong = ev_for(it)
    wrong["payload"] = json.dumps({"pr": 7, "head_sha": "OTHER", "review_id": "r42", "issue": 9})
    assert codes(dv.compare([it], [wrong], now=NOW)) == ["PAYLOAD_MISMATCH"]


def test_event_with_other_job_id_is_payload_mismatch():
    it = item()
    wrong = ev_for(it, job_id="pr:99")
    assert codes(dv.compare([it], [wrong], now=NOW)) == ["PAYLOAD_MISMATCH"]


def test_non_json_payload_is_payload_mismatch():
    it = item()
    wrong = ev_for(it, payload="{nao-json")
    assert codes(dv.compare([it], [wrong], now=NOW)) == ["PAYLOAD_MISMATCH"]


def test_unknown_queue_state_is_never_assumed_equal():
    it = item("WEIRD")
    assert codes(dv.compare([it], [ev_for(it)], now=NOW)) == ["UNKNOWN_STATE"]


@pytest.mark.parametrize("state", ["BLOCKED_CREDENTIALS", "BLOCKED_CLI", "BLOCKED_WORKTREE"])
def test_blocked_states_are_expected_pending_or_dlq_not_superseded(state):
    it = item(state, attempts=1)
    assert dv.compare([it], [ev_for(it, status="PENDING", attempt=1)], now=NOW) == []
    assert dv.compare([it], [ev_for(it, status="DLQ", attempt=3)], now=NOW) != []


def test_dlq_event_while_queue_can_still_pick_is_state_mismatch():
    it = item("FAILED", attempts=1)
    assert codes(dv.compare([it], [ev_for(it, status="DLQ", attempt=3)], now=NOW)) == ["STATE_MISMATCH"]


def test_pending_event_after_queue_exhausted_is_state_mismatch():
    it = item("FAILED", attempts=3)
    assert codes(dv.compare([it], [ev_for(it, status="PENDING")], now=NOW)) == ["STATE_MISMATCH"]


def test_attempt_budget_difference_is_reported():
    it = item("FAILED", attempts=1)  # queue has 2 attempts left
    ev = ev_for(it, status="PENDING", attempt=0, max_attempts=3)  # event has 3
    assert codes(dv.compare([it], [ev], now=NOW)) == ["STATE_MISMATCH"]


def test_queued_item_with_different_budget_is_reported():
    it = item("QUEUED", attempts=0)  # 3 left
    ev = ev_for(it, status="PENDING", attempt=1, max_attempts=3)  # 2 left
    assert codes(dv.compare([it], [ev], now=NOW)) == ["STATE_MISMATCH"]


def test_event_available_before_retry_after_is_reported():
    it = item("FAILED", attempts=1, retry_after="2027-01-15T10:00:00Z")
    ev = ev_for(it, status="PENDING", attempt=1, available_at=NOW)
    assert codes(dv.compare([it], [ev], now=dv._epoch("2027-01-15T09:00:00Z"))) == ["STATE_MISMATCH"]


def test_event_available_one_minute_late_is_tolerated_but_an_hour_late_is_not():
    it = item("FAILED", attempts=1, retry_after=None)
    late_ok = ev_for(it, status="PENDING", attempt=1, available_at=NOW + dv.LATE_TOLERANCE_S)
    late_bad = ev_for(it, status="PENDING", attempt=1, available_at=NOW + 3600)
    assert dv.compare([it], [late_ok], now=NOW) == []
    assert codes(dv.compare([it], [late_bad], now=NOW)) == ["STATE_MISMATCH"]


def test_duplicate_review_items_in_queue_are_rejected_by_reader(tmp_path):
    q = tmp_path / "q.json"
    write_queue(q, [item(key="dup"), item(key="dup", review="r99")])
    with pytest.raises(dv.DivergenceInputError, match="duplicate"):
        dv.read_queue(q)


def test_invalid_queue_item_raises_instead_of_reporting_ok():
    bad = item()
    bad["pr"] = "7"
    with pytest.raises(dv.DivergenceInputError):
        dv.compare([bad], [], now=NOW)


def test_event_id_without_review_prefix_is_invalid():
    bad = item()
    bad["event_id"] = "42"
    with pytest.raises(dv.DivergenceInputError):
        dv.compare([bad], [], now=NOW)


def test_invalid_retry_after_raises():
    bad = item(retry_after="amanha")
    with pytest.raises(dv.DivergenceInputError):
        dv.compare([bad], [], now=NOW)


# --- queue reader ------------------------------------------------------------

def test_missing_queue_file_is_an_error_not_an_empty_queue(tmp_path):
    with pytest.raises(dv.DivergenceInputError):
        dv.read_queue(tmp_path / "nope.json")


def test_queue_without_queue_key_is_an_error(tmp_path):
    p = tmp_path / "q.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(dv.DivergenceInputError):
        dv.read_queue(p)


def test_queue_with_utf8_bom_is_accepted(tmp_path):
    q = tmp_path / "q.json"
    write_queue(q, [item()], bom=True)
    assert len(dv.read_queue(q)) == 1


def test_queue_not_json_is_an_error(tmp_path):
    q = tmp_path / "q.json"
    q.write_bytes(b"\x00\x01 nao e json")
    with pytest.raises(dv.DivergenceInputError, match="not valid JSON"):
        dv.read_queue(q)


def test_queue_permission_error_is_structured(tmp_path, monkeypatch):
    q = tmp_path / "q.json"
    write_queue(q, [item()])

    def denied(self, *a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(dv.DivergenceInputError, match="unreadable"):
        dv.read_queue(q)


# --- store reader and snapshot -----------------------------------------------

def test_missing_store_is_an_error_and_is_not_created(tmp_path):
    db = tmp_path / "events.sqlite"
    with pytest.raises(dv.DivergenceInputError):
        dv.read_events(db)
    assert not db.exists()
    assert list(tmp_path.iterdir()) == []


def test_non_sqlite_file_is_a_structured_error(tmp_path):
    db = tmp_path / "events.sqlite"
    db.write_bytes(b"isto nao e um banco " * 20)
    with pytest.raises(dv.DivergenceInputError):
        dv.read_events(db)


def test_zero_byte_store_is_an_error_not_an_empty_store(tmp_path):
    db = tmp_path / "events.sqlite"
    db.write_bytes(b"")
    with pytest.raises(dv.DivergenceInputError, match="events"):
        dv.read_events(db)


def test_store_without_required_column_is_an_error(tmp_path):
    db = tmp_path / "events.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, event_type TEXT)")
    with pytest.raises(dv.DivergenceInputError, match="lacks columns"):
        dv.read_events(db)


def test_read_only_detection_does_not_modify_store_or_queue(tmp_path):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    before_db = db.read_bytes()
    before_q = q.read_bytes()
    before_names = sorted(p.name for p in tmp_path.iterdir())
    assert dv.detect(q, db, now=NOW) == []
    assert db.read_bytes() == before_db
    assert q.read_bytes() == before_q
    assert sorted(p.name for p in tmp_path.iterdir()) == before_names


def test_non_empty_wal_is_refused_not_compared(tmp_path):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    (tmp_path / "events.sqlite-wal").write_bytes(b"x" * 64)  # a writer is open or data is not checkpointed
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    with pytest.raises(dv.DivergenceInputError, match="non-empty WAL"):
        dv.detect(q, db, now=NOW)


def test_commit_after_wal_check_is_refused_by_post_read_fingerprint(tmp_path, monkeypatch):
    """BLOCKER from review: a writer commits after the WAL check. The immutable read would
    return the old snapshot silently; the post-read fingerprint must refuse it."""
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    real_is_wal = dv._is_wal

    def check_then_writer_commits(path):
        result = real_is_wal(path)
        EventStore(db).publish("CORRECTION_REQUESTED", "review:other:9:zzz", "pr:9", {"pr": 9},
                               producer="event_bridge")
        return result

    monkeypatch.setattr(dv, "_is_wal", check_then_writer_commits)
    with pytest.raises(dv.DivergenceInputError, match="changed during the read"):
        dv.detect(q, db, now=NOW)


def test_commit_between_fingerprints_is_refused_even_without_wal_check(tmp_path, monkeypatch):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    real_select = dv._select_events

    def read_then_writer_commits(uri):
        rows = real_select(uri)
        EventStore(db).publish("CORRECTION_REQUESTED", "review:other:9:zzz", "pr:9", {"pr": 9},
                               producer="event_bridge")
        return rows

    monkeypatch.setattr(dv, "_select_events", read_then_writer_commits)
    with pytest.raises(dv.DivergenceInputError, match="changed during the read"):
        dv.detect(q, db, now=NOW)


def test_queue_rewritten_during_store_read_is_refused(tmp_path, monkeypatch):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    real_read_events = dv.read_events

    def read_then_bridge_rewrites_queue(path):
        rows = real_read_events(path)
        write_queue(q, [it, item("QUEUED", review="r43", key="k2")])  # a bridge cycle writes the queue
        return rows

    monkeypatch.setattr(dv, "read_events", read_then_bridge_rewrites_queue)
    with pytest.raises(dv.DivergenceInputError, match="queue file changed"):
        dv.detect(q, db, now=NOW)


def test_os_error_during_store_access_is_structured(tmp_path, monkeypatch):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])

    def denied(path):
        raise PermissionError("denied")
    monkeypatch.setattr(dv, "_snapshot", denied)
    with pytest.raises(dv.DivergenceInputError, match="unreadable"):
        dv.detect(q, db, now=NOW)


def test_detect_end_to_end_finds_missing_mirror(tmp_path):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    EventStore(db)  # empty store with schema, no mirror event
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    assert codes(dv.detect(q, db, now=NOW)) == ["MISSING_IN_STORE"]


# --- CLI ----------------------------------------------------------------------

def test_cli_consistent_exits_zero_and_never_authorizes_cutover(tmp_path, capsys):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    assert dv.main(["--queue", str(q), "--store", str(db), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["consistent"] is True and report["authorizes_cutover"] is False


def test_cli_divergence_exits_one(tmp_path, capsys):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    EventStore(db)  # no mirror event
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    assert dv.main(["--queue", str(q), "--store", str(db), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["consistent"] is False


def test_cli_orphan_terminal_is_a_warning_exit_zero_unless_strict(tmp_path, capsys):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it, status="PENDING")
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO events (event_id, event_type, idempotency_key, job_id, producer, payload, status, "
                     "created_at, updated_at, available_at) VALUES ('e2','CORRECTION_REQUESTED','review:old:1:aaa','pr:1',"
                     "'event_bridge','{}','DONE',0,0,0)")
        conn.commit()
    finally:
        conn.close()  # the last close checkpoints the WAL and removes -wal; a bare `with` does not close
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    assert dv.main(["--queue", str(q), "--store", str(db), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["consistent"] is True and len(report["warnings"]) == 1
    assert dv.main(["--queue", str(q), "--store", str(db), "--json", "--strict"]) == 3


def test_cli_missing_queue_exits_two_structured(tmp_path, capsys):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    assert dv.main(["--queue", str(tmp_path / "missing.json"), "--store", str(db), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["consistent"] is False and "queue file missing" in report["error"]


def test_cli_unexpected_os_error_exits_two_not_one(tmp_path, capsys, monkeypatch):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])

    def denied(path):
        raise PermissionError("denied")
    monkeypatch.setattr(dv, "_snapshot", denied)
    assert dv.main(["--queue", str(q), "--store", str(db), "--json"]) == 2
