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
    payload = {"pr": it["pr"], "head_sha": it["head_sha"], "review_id": it["event_id"].split(":")[-1], "issue": it["issue"]}
    e = {"idempotency_key": dv.mirror_key(it), "producer": "event_bridge", "event_type": "CORRECTION_REQUESTED",
         "job_id": f"pr:{it['pr']}", "payload": json.dumps(payload), "status": status,
         "attempt": attempt, "max_attempts": max_attempts, "available_at": available_at}
    e.update(over)
    return e


def codes(found):
    return sorted(d.code for d in found)


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


def test_terminal_event_without_queue_item_is_only_a_warning_code():
    it = item()
    assert codes(dv.compare([], [ev_for(it, status="DONE")], now=NOW)) == ["ORPHAN_IN_STORE"]


def test_finished_item_with_pending_event_is_stale_pending():
    it = item("SUPERSEDED")
    assert codes(dv.compare([it], [ev_for(it, status="PENDING")], now=NOW)) == ["STALE_PENDING"]


def test_running_queue_item_blocks_as_json_running():
    it = item("RUNNING")
    assert "JSON_RUNNING" in codes(dv.compare([it], [ev_for(it)], now=NOW))


def test_event_with_same_key_but_other_head_is_payload_mismatch():
    it = item()
    wrong = ev_for(it)
    wrong["payload"] = json.dumps({"pr": 7, "head_sha": "OTHER", "review_id": "r42", "issue": 9})
    assert codes(dv.compare([it], [wrong], now=NOW)) == ["PAYLOAD_MISMATCH"]


def test_unknown_queue_state_is_never_assumed_equal():
    it = item("WEIRD")
    assert codes(dv.compare([it], [ev_for(it)], now=NOW)) == ["UNKNOWN_STATE"]


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


def test_event_available_before_retry_after_is_reported():
    it = item("FAILED", attempts=1, retry_after="2027-01-15T10:00:00Z")
    ev = ev_for(it, status="PENDING", attempt=1, available_at=NOW)
    assert codes(dv.compare([it], [ev], now=dv._epoch("2027-01-15T09:00:00Z"))) == ["STATE_MISMATCH"]


def test_event_available_much_later_than_queue_is_reported():
    it = item("FAILED", attempts=1, retry_after=None)
    ev = ev_for(it, status="PENDING", attempt=1, available_at=NOW + 3600)
    assert codes(dv.compare([it], [ev], now=NOW)) == ["STATE_MISMATCH"]


def test_invalid_queue_item_raises_instead_of_reporting_ok():
    bad = item()
    bad["pr"] = "7"
    with pytest.raises(dv.DivergenceInputError):
        dv.compare([bad], [], now=NOW)


def test_invalid_retry_after_raises():
    bad = item(retry_after="amanha")
    with pytest.raises(dv.DivergenceInputError):
        dv.compare([bad], [], now=NOW)


# --- source readers ---------------------------------------------------------

def write_queue(path: Path, queue):
    path.write_text(json.dumps({"queue": queue}), encoding="utf-8")


def make_store(path: Path, it, status="PENDING"):
    store = EventStore(path)
    store.publish("CORRECTION_REQUESTED", dv.mirror_key(it), f"pr:{it['pr']}",
                  {"pr": it["pr"], "head_sha": it["head_sha"], "review_id": it["event_id"].split(":")[-1], "issue": it["issue"]},
                  producer="event_bridge")
    if status != "PENDING":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE events SET status=?", (status,))
    return store


def test_missing_queue_file_is_an_error_not_an_empty_queue(tmp_path):
    with pytest.raises(dv.DivergenceInputError):
        dv.read_queue(tmp_path / "nope.json")


def test_queue_without_queue_key_is_an_error(tmp_path):
    p = tmp_path / "q.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(dv.DivergenceInputError):
        dv.read_queue(p)


def test_missing_store_is_an_error_and_is_not_created(tmp_path):
    db = tmp_path / "events.sqlite"
    with pytest.raises(dv.DivergenceInputError):
        dv.read_events(db)
    assert not db.exists()
    assert list(tmp_path.iterdir()) == []


def test_read_only_detection_does_not_modify_store_or_queue(tmp_path):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    before_db = db.read_bytes()
    before_q = q.read_bytes()
    before_names = sorted(p.name for p in tmp_path.iterdir())
    found = dv.detect(q, db, now=NOW)
    assert found == []
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


def test_detect_end_to_end_finds_missing_mirror(tmp_path):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    EventStore(db)  # empty store with schema, no mirror event
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    assert codes(dv.detect(q, db, now=NOW)) == ["MISSING_IN_STORE"]


def test_cli_exit_codes(tmp_path, capsys):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    assert dv.main(["--queue", str(q), "--store", str(db), "--json"]) == 0
    capsys.readouterr()
    assert dv.main(["--queue", str(tmp_path / "missing.json"), "--store", str(db), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and "queue file missing" in report["error"]
