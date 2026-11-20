"""EventStore.release_blocked: a blocked cycle returns the claimed event to PENDING without
consuming an attempt (issue #19, FP-062).

Default decided for #19: a claim consumes one attempt (store semantics). The JSON executor does
not consume an attempt for BLOCKED_CLI, BLOCKED_CREDENTIALS or BLOCKED_WORKTREE, so the store must
give back the attempt the claim took. Otherwise three blocked cycles dead-letter an event that the
JSON queue would still run (historical PR #636, HIGH findings).
"""
import pytest

from agent_runtime.event_store import EventStore, LeaseLost


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    s = EventStore(tmp_path / "events.sqlite", clock=clock)
    s._clock_for_test = clock
    return s


def mirror(store):
    return [e for e in store.events() if e["event_type"] == "CORRECTION_REQUESTED"][-1]


def _publish(store, key="review:1:7:abc"):
    store.publish("CORRECTION_REQUESTED", key, "pr:7", {"pr": 7}, producer="event_bridge")
    return mirror(store)


def test_release_blocked_returns_the_event_to_pending_and_refunds_the_claim_attempt(store):
    _publish(store)
    ev = store.claim(("CORRECTION_REQUESTED",), "exec", 60)
    assert ev["attempt"] == 1
    store.release_blocked(ev, "CLI indisponivel", retry_delay=30)
    after = store.get(ev["id"])
    assert after["status"] == "PENDING"
    assert after["attempt"] == 0  # the claim's attempt was refunded
    assert after["lease_token"] is None and after["lease_owner"] is None
    assert after["available_at"] == 1030.0  # clock 1000 + retry_delay 30


def test_three_blocked_cycles_never_dead_letter_the_event(store):
    _publish(store)
    for _ in range(3):
        ev = store.claim(("CORRECTION_REQUESTED",), "exec", 60)
        assert ev is not None, "a blocked event must stay claimable"
        store.release_blocked(ev, "credenciais ausentes", retry_delay=0)
    final = mirror(store)
    assert final["status"] == "PENDING"
    assert final["attempt"] == 0
    assert not any(e["status"] == "DLQ" for e in store.events() if e["event_type"] == "CORRECTION_REQUESTED")


def test_plain_fail_still_consumes_attempts_and_dead_letters(store):
    # Unchanged behaviour for real failures: only blocked releases refund.
    _publish(store)
    for _ in range(3):
        ev = store.claim(("CORRECTION_REQUESTED",), "exec", 60)
        store.fail(ev, "erro real", retry_delay=0)
    assert mirror(store)["status"] == "DLQ"


def test_release_blocked_by_a_non_owner_is_refused(store):
    _publish(store)
    ev = store.claim(("CORRECTION_REQUESTED",), "exec", 60)
    impostor = dict(ev, lease_token="outro-token")
    with pytest.raises(LeaseLost):
        store.release_blocked(impostor, "tentativa de outro dono", retry_delay=0)
    assert store.get(ev["id"])["status"] == "RUNNING"  # nothing was released


def test_release_blocked_records_a_transition_with_the_reason(store):
    _publish(store)
    ev = store.claim(("CORRECTION_REQUESTED",), "exec", 60)
    store.release_blocked(ev, "worktree bloqueada", retry_delay=0)
    last = store.transitions()[-1]
    assert last["to_status"] == "PENDING"
    assert "worktree bloqueada" in last["detail"]


def test_release_blocked_never_drives_attempts_negative(store):
    _publish(store)
    ev = store.claim(("CORRECTION_REQUESTED",), "exec", 60)
    store.release_blocked(ev, "primeira", retry_delay=0)
    ev2 = store.claim(("CORRECTION_REQUESTED",), "exec", 60)
    store.release_blocked(ev2, "segunda", retry_delay=0)
    assert store.get(ev2["id"])["attempt"] == 0
