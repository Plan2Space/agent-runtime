"""Negative cases from the independent review of PR #31 (second round)."""
import json
import os
import sqlite3

import pytest

from agent_runtime import divergence as dv
from agent_runtime.event_store import EventStore

NOW = 1_800_000_000.0


def item(state="QUEUED", pr=7, head="abc123", attempts=0, review="r42", key="k1"):
    return {"kind": "review", "key": key, "state": state, "pr": pr, "head_sha": head,
            "event_id": f"review:{review}", "issue": 9, "attempts": attempts, "retry_after": None}


def ev_for(it, status="PENDING", attempt=0, max_attempts=3, available_at=NOW, payload=None, **over):
    body = payload if payload is not None else {
        "pr": it["pr"], "head_sha": it["head_sha"], "review_id": it["event_id"][len("review:"):], "issue": it["issue"]}
    e = {"idempotency_key": dv.mirror_key(it), "producer": "event_bridge", "event_type": "CORRECTION_REQUESTED",
         "job_id": f"pr:{it['pr']}", "payload": json.dumps(body), "status": status,
         "attempt": attempt, "max_attempts": max_attempts, "available_at": available_at}
    e.update(over)
    return e


def codes(found):
    return sorted(d.code for d in found)


def write_queue(path, queue):
    path.write_text(json.dumps({"queue": queue}), encoding="utf-8")


def make_store(path, it):
    store = EventStore(path)
    store.publish("CORRECTION_REQUESTED", dv.mirror_key(it), f"pr:{it['pr']}",
                  {"pr": it["pr"], "head_sha": it["head_sha"], "review_id": it["event_id"][len("review:"):], "issue": it["issue"]},
                  producer="event_bridge")
    return store


# --- snapshot by content, not by size or mtime --------------------------------

def test_same_size_queue_rewrite_with_restored_mtime_is_refused(tmp_path, monkeypatch):
    """`QUEUED` -> `FAILED` keeps the file size. A stat()-based check passes it when the
    mtime is restored; the content check must refuse it."""
    before_item = item("QUEUED", attempts=0)
    db = tmp_path / "events.sqlite"
    make_store(db, before_item)
    q = tmp_path / "queue.json"
    write_queue(q, [before_item])
    st = os.stat(q)
    real_read_events = dv.read_events

    def read_then_same_size_rewrite(path):
        rows = real_read_events(path)
        write_queue(q, [item("FAILED", attempts=0)])  # same length as QUEUED
        os.utime(q, ns=(st.st_atime_ns, st.st_mtime_ns))  # the mtime tick is not visible
        return rows

    monkeypatch.setattr(dv, "read_events", read_then_same_size_rewrite)
    with pytest.raises(dv.DivergenceInputError, match="queue file changed"):
        dv.detect(q, db, now=NOW)


def test_rollback_journal_present_is_refused(tmp_path):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    (tmp_path / "events.sqlite-journal").write_bytes(b"journal em andamento")
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    with pytest.raises(dv.DivergenceInputError, match="rollback journal"):
        dv.detect(q, db, now=NOW)


def test_empty_wal_is_accepted_and_no_shm_is_created(tmp_path):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    (tmp_path / "events.sqlite-wal").write_bytes(b"")  # zero bytes: no frames, allowed
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    assert dv.detect(q, db, now=NOW) == []
    assert not (tmp_path / "events.sqlite-shm").exists()


# --- fail-closed on hostile or malformed input ---------------------------------

def test_deeply_nested_queue_json_is_structured_error(tmp_path):
    q = tmp_path / "q.json"
    q.write_text('{"queue": ' + "[" * 100_000, encoding="utf-8")
    with pytest.raises(dv.DivergenceInputError, match="not valid JSON"):
        dv.read_queue(q)


def test_deeply_nested_payload_is_an_input_error_not_a_divergence():
    it = item()
    deep = ev_for(it, payload=None)
    deep["payload"] = "[" * 100_000
    with pytest.raises(dv.DivergenceInputError, match="nested too deeply"):
        dv.compare([it], [deep], now=NOW)


def test_unknown_item_kind_in_queue_is_refused_not_ignored(tmp_path):
    q = tmp_path / "q.json"
    write_queue(q, [{"kind": "implement", "key": "i1", "state": "QUEUED"}])
    with pytest.raises(dv.DivergenceInputError, match="unsupported item kind"):
        dv.read_queue(q)


def test_executable_foreign_store_event_is_a_divergence():
    foreign = {"idempotency_key": "impl:1", "producer": "coordinator", "event_type": "IMPLEMENT_REQUESTED",
               "job_id": "pr:1", "payload": "{}", "status": "PENDING", "attempt": 0, "max_attempts": 3,
               "available_at": NOW}
    assert codes(dv.compare([], [foreign], now=NOW)) == ["FOREIGN_EXECUTABLE"]


def test_non_executable_foreign_store_event_is_ignored():
    foreign = {"idempotency_key": "impl:1", "producer": "coordinator", "event_type": "IMPLEMENT_REQUESTED",
               "job_id": "pr:1", "payload": "{}", "status": "RECORDED", "attempt": 0, "max_attempts": 3,
               "available_at": NOW}
    assert dv.compare([], [foreign], now=NOW) == []


def test_duplicate_mirror_key_in_store_is_refused_even_when_called_directly():
    it = item()
    with pytest.raises(dv.DivergenceInputError, match="duplicate mirror idempotency key"):
        dv.compare([it], [ev_for(it), ev_for(it, status="DONE")], now=NOW)


def test_error_report_keeps_the_full_schema(tmp_path, capsys):
    q = tmp_path / "q.json"
    q.write_text("nao e json", encoding="utf-8")
    db = tmp_path / "events.sqlite"
    EventStore(db)
    assert dv.main(["--queue", str(q), "--store", str(db), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["failed_on"] == "input error"
    assert report["divergences"] == [] and report["warnings"] == []


def test_broken_output_exits_two_not_one(tmp_path, monkeypatch):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"queue": [it]}), encoding="utf-8")

    def broken_pipe(*a, **k):
        raise BrokenPipeError("stdout fechado")
    monkeypatch.setattr("builtins.print", broken_pipe)
    assert dv.main(["--queue", str(q), "--store", str(db), "--json"]) == 2


def test_infinite_available_at_is_structured_error():
    it = item("FAILED", attempts=1)
    ev = ev_for(it, status="PENDING", attempt=1, available_at=float("inf"))
    with pytest.raises(dv.DivergenceInputError, match="finite number"):
        dv.compare([it], [ev], now=NOW)


def test_non_integer_event_attempt_is_structured_error():
    it = item("FAILED", attempts=1)
    ev = ev_for(it, status="PENDING", attempt="abc")
    with pytest.raises(dv.DivergenceInputError, match="integer"):
        dv.compare([it], [ev], now=NOW)


def test_negative_max_attempts_is_structured_error():
    with pytest.raises(dv.DivergenceInputError, match="max_attempts"):
        dv.compare([item()], [], now=NOW, max_attempts=-1)


def test_unexpected_exception_inside_detection_becomes_structured_error(tmp_path, monkeypatch):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])

    def boom(*a, **k):
        raise KeyError("campo que nao existe")
    monkeypatch.setattr(dv, "compare", boom)
    with pytest.raises(dv.DivergenceInputError, match="inconsistent input"):
        dv.detect(q, db, now=NOW)


# --- typed payload comparison --------------------------------------------------

def test_boolean_true_in_payload_does_not_equal_integer_one():
    it = item(pr=1)
    ev = ev_for(it, payload={"pr": True, "head_sha": "abc123", "review_id": "r42", "issue": 9})
    assert codes(dv.compare([it], [ev], now=NOW)) == ["PAYLOAD_MISMATCH"]


def test_float_seven_in_payload_does_not_equal_integer_seven():
    it = item(pr=7)
    ev = ev_for(it, payload={"pr": 7.0, "head_sha": "abc123", "review_id": "r42", "issue": 9})
    assert codes(dv.compare([it], [ev], now=NOW)) == ["PAYLOAD_MISMATCH"]


# --- duplicate work with different keys ---------------------------------------

def test_two_items_for_same_review_pr_and_head_are_refused():
    a = item(key="k1")
    b = item(key="k2")  # same event_id, pr and head: one event would stand for both
    with pytest.raises(dv.DivergenceInputError, match="duplicate review work item"):
        dv.compare([a, b], [ev_for(a)], now=NOW)


def test_duplicate_work_is_refused_through_detect(tmp_path):
    a = item(key="k1")
    b = item(key="k2")
    db = tmp_path / "events.sqlite"
    make_store(db, a)
    q = tmp_path / "queue.json"
    write_queue(q, [a, b])
    with pytest.raises(dv.DivergenceInputError, match="duplicate review work item"):
        dv.detect(q, db, now=NOW)


# --- executor-mirrored states --------------------------------------------------

def test_blocked_review_fetch_with_pending_event_is_state_mismatch():
    it = item("BLOCKED_REVIEW_FETCH", attempts=1)
    assert codes(dv.compare([it], [ev_for(it, status="PENDING", attempt=1)], now=NOW)) == ["STATE_MISMATCH"]


def test_blocked_review_fetch_with_dlq_event_is_concordant():
    it = item("BLOCKED_REVIEW_FETCH", attempts=1)
    assert dv.compare([it], [ev_for(it, status="DLQ", attempt=3)], now=NOW) == []


def test_executor_contract_constants_are_pinned():
    # #19 must check these against the executor source; changing one side alone is a bug.
    assert dv.PICKABLE == frozenset({"QUEUED", "FAILED", "BLOCKED_CLI", "BLOCKED_CREDENTIALS", "BLOCKED_WORKTREE"})
    assert dv.DEFAULT_MAX_ATTEMPTS == 3


# --- CLI exit codes -------------------------------------------------------------

def test_cli_internal_failure_exits_two_never_one(tmp_path, capsys, monkeypatch):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    write_queue(q, [it])

    def internal(*a, **k):
        raise RuntimeError("bug interno")
    monkeypatch.setattr(dv, "detect", internal)
    assert dv.main(["--queue", str(q), "--store", str(db), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["consistent"] is False and "internal error" in report["error"]


def test_cli_strict_failure_reports_failed_on_warning(tmp_path, capsys):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO events (event_id, event_type, idempotency_key, job_id, producer, payload, status, "
                     "created_at, updated_at, available_at) VALUES ('e9','CORRECTION_REQUESTED','review:old:1:aaa','pr:1',"
                     "'event_bridge','{}','DONE',0,0,0)")
        conn.commit()
    finally:
        conn.close()
    q = tmp_path / "queue.json"
    write_queue(q, [it])
    assert dv.main(["--queue", str(q), "--store", str(db), "--json", "--strict"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed_on"] == "warning (strict)" and report["divergences"] == []


def test_broken_output_with_value_error_exits_two_never_one(tmp_path, monkeypatch):
    it = item("QUEUED")
    db = tmp_path / "events.sqlite"
    make_store(db, it)
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"queue": [it]}), encoding="utf-8")

    def closed_file(*a, **k):
        raise ValueError("I/O operation on closed file")
    monkeypatch.setattr("builtins.print", closed_file)
    assert dv.main(["--queue", str(q), "--store", str(db), "--json"]) == 2


def test_text_mode_input_error_exits_two(tmp_path, capsys):
    db = tmp_path / "events.sqlite"
    EventStore(db)
    assert dv.main(["--queue", str(tmp_path / "missing.json"), "--store", str(db)]) == 2
    assert "queue file missing" in capsys.readouterr().out


def test_queued_comment_is_an_unmirrored_divergence_not_silence():
    comment = {"kind": "comment", "key": "c1", "state": "QUEUED"}
    assert codes(dv.compare([comment], [], now=NOW)) == ["UNMIRRORED_ITEM"]


def test_comment_without_key_is_refused():
    with pytest.raises(dv.DivergenceInputError, match="without `key`"):
        dv.compare([{"kind": "comment", "state": "DONE"}], [], now=NOW)


def test_bridge_event_of_other_type_executable_is_foreign():
    other = {"idempotency_key": "rev:1", "producer": "event_bridge", "event_type": "REVIEW_REQUESTED",
             "job_id": "pr:1", "payload": "{}", "status": "PENDING", "attempt": 0, "max_attempts": 3,
             "available_at": NOW}
    assert codes(dv.compare([], [other], now=NOW)) == ["FOREIGN_EXECUTABLE"]


def test_running_foreign_event_is_foreign():
    running = {"idempotency_key": "impl:2", "producer": "coordinator", "event_type": "IMPLEMENT_REQUESTED",
               "job_id": "pr:2", "payload": "{}", "status": "RUNNING", "attempt": 1, "max_attempts": 3,
               "available_at": NOW}
    assert codes(dv.compare([], [running], now=NOW)) == ["FOREIGN_EXECUTABLE"]


def test_null_event_attempt_is_an_input_error_not_a_default():
    it = item("FAILED", attempts=1)
    ev = ev_for(it, status="PENDING", attempt=None)
    with pytest.raises(dv.DivergenceInputError, match="`attempt` is missing"):
        dv.compare([it], [ev], now=NOW)


def test_null_event_max_attempts_is_an_input_error_not_a_default():
    it = item("FAILED", attempts=1)
    ev = ev_for(it, status="PENDING", max_attempts=None)
    with pytest.raises(dv.DivergenceInputError, match="`max_attempts` is missing"):
        dv.compare([it], [ev], now=NOW)
