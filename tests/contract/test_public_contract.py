"""Contrato público do agent-runtime, versão 0.1.x (ver docs/contracts/CONTRACT.md).

Esta suíte é a única usada na prova de rollback. Por isso:
- usa SOMENTE nomes listados como públicos em CONTRACT.md para 0.1.x;
- não importa nada de `_`-prefixado nem de módulo fora da superfície pública;
- não depende de nada que só exista em versões posteriores.

Se uma versão nova adicionar API, ela ganha teste em `tests/` (específico da versão),
nunca aqui. Assim a suíte continua válida para qualquer wheel 0.1.x anterior.
"""
from __future__ import annotations

import json

import pytest

from agent_runtime import contracts, durable_file, event_store, outbox_publisher
from agent_runtime.contracts import ContractError, JobEnvelope
from agent_runtime.event_store import EventStore
from agent_runtime.outbox_publisher import LedgerTarget, drain, publish_one


def _job(**overrides):
    data = {
        "schema_version": "1.0.0",
        "job_id": "job-1",
        "kind": "IMPLEMENT_REQUESTED",
        "idempotency_key": "issue-1:implement:v1",
        "payload": {"issue": 1},
    }
    data.update(overrides)
    return data


# -- versões e superfície -----------------------------------------------------

def test_public_version_constants_are_the_0_1_contract():
    assert contracts.CONTRACT_VERSION == "1.0.0"
    assert contracts.SUPPORTED_MAJOR == 1
    assert event_store.SCHEMA_VERSION == 1
    assert "IMPLEMENT_REQUESTED" in contracts.ALLOWED_KINDS


def test_public_names_exist():
    for name in ("write_durable", "fsync_dir", "replace_retry"):
        assert callable(getattr(durable_file, name))
    for name in ("publish_one", "drain", "LedgerTarget", "TargetError"):
        assert hasattr(outbox_publisher, name)
    for name in ("EventStore", "LeaseLost", "EVENT_TYPES", "CONSUMABLE"):
        assert hasattr(event_store, name)


# -- JobEnvelope ----------------------------------------------------------------

def test_envelope_accepts_a_well_formed_job():
    job = JobEnvelope.from_dict(_job())
    assert job.job_id == "job-1" and job.payload["issue"] == 1


def test_envelope_payload_is_read_only():
    job = JobEnvelope.from_dict(_job())
    with pytest.raises(TypeError):
        job.payload["issue"] = 2  # type: ignore[index]


@pytest.mark.parametrize("override", [
    {"schema_version": "2.0.0"},
    {"schema_version": "one"},
    {"kind": "DELETE_EVERYTHING"},
    {"job_id": "   "},
    {"payload": ["not", "a", "mapping"]},
])
def test_envelope_rejects_what_the_contract_rejects(override):
    with pytest.raises(ContractError):
        JobEnvelope.from_dict(_job(**override))


# -- EventStore -----------------------------------------------------------------

def test_publish_is_idempotent_on_the_idempotency_key(tmp_path):
    store = EventStore(tmp_path / "events.db")
    first, created_first = store.publish("IMPLEMENT_REQUESTED", "k1", "job:1", {"a": 1})
    second, created_second = store.publish("IMPLEMENT_REQUESTED", "k1", "job:1", {"a": 999})
    assert created_first is True and created_second is False and first == second
    assert json.loads(store.get(first)["payload"]) == {"a": 1}


def test_claim_then_complete_marks_done_and_is_not_reclaimed(tmp_path):
    store = EventStore(tmp_path / "events.db")
    event_id, _ = store.publish("IMPLEMENT_REQUESTED", "k1", "job:1", {})
    claimed = store.claim(("IMPLEMENT_REQUESTED",), "worker-a", 30.0)
    assert claimed is not None and claimed["id"] == event_id
    store.complete(claimed, {"ok": True})
    assert store.get(event_id)["status"] == "DONE"
    assert store.claim(("IMPLEMENT_REQUESTED",), "worker-b", 30.0) is None


def test_a_stale_owner_cannot_complete_after_losing_the_lease(tmp_path):
    clock = {"t": 1_000.0}
    store = EventStore(tmp_path / "events.db", clock=lambda: clock["t"])
    store.publish("IMPLEMENT_REQUESTED", "k1", "job:1", {})
    stale = store.claim(("IMPLEMENT_REQUESTED",), "worker-a", 5.0)
    clock["t"] += 10.0
    fresh = store.claim(("IMPLEMENT_REQUESTED",), "worker-b", 5.0)
    assert fresh is not None
    with pytest.raises(event_store.LeaseLost):
        store.complete(stale, {"late": True})


# -- Outbox ---------------------------------------------------------------------

def test_outbox_sends_once_and_reconciles_on_the_second_pass(tmp_path):
    store = EventStore(tmp_path / "events.db")
    event_id, _ = store.publish("PUBLISH_REQUESTED", "pub:1", "job:1", {})
    claimed = store.claim(("PUBLISH_REQUESTED",), "publisher", 30.0)
    store.complete(claimed, {}, outbox_entries=[{
        "target": "ledger", "operation": "post", "payload": {"text": "hello"},
        "idempotency_key": "out:1",
    }])
    target = LedgerTarget(tmp_path / "ledger.jsonl")
    first = publish_one(store, {"ledger": target}, "publisher-1")
    assert first["action"] == "SENT"
    assert [row["key"] for row in target.lines()] == ["out:1"]


def test_outbox_reconciles_instead_of_sending_a_second_time(tmp_path):
    store = EventStore(tmp_path / "events.db")
    event_id, _ = store.publish("PUBLISH_REQUESTED", "pub:1", "job:1", {})
    claimed = store.claim(("PUBLISH_REQUESTED",), "publisher", 30.0)
    store.complete(claimed, {}, outbox_entries=[{
        "target": "ledger", "operation": "post", "payload": {"text": "hello"},
        "idempotency_key": "out:1",
    }])
    target = LedgerTarget(tmp_path / "ledger.jsonl")
    target.send({"idempotency_key": "out:1", "operation": "post", "payload": {"text": "hello"}})
    result = publish_one(store, {"ledger": target}, "publisher-2")
    assert result["action"] == "RECONCILED"
    assert len(target.lines()) == 1


def test_drain_on_an_empty_queue_returns_nothing(tmp_path):
    store = EventStore(tmp_path / "events.db")
    assert drain(store, {}, "publisher") == []


# -- Escrita durável --------------------------------------------------------------

def test_write_durable_replaces_the_file_and_leaves_no_temporary(tmp_path):
    target = tmp_path / "state.json"
    durable_file.write_durable(target, '{"v": 1}\n')
    durable_file.write_durable(target, '{"v": 2}\n')
    assert target.read_text(encoding="utf-8") == '{"v": 2}\n'
    assert not (tmp_path / "state.json.tmp").exists()


def test_fsync_dir_never_raises_on_a_missing_directory(tmp_path):
    durable_file.fsync_dir(tmp_path / "does-not-exist")
