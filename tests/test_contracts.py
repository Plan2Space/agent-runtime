import pytest

from agent_runtime.contracts import ContractError, JobEnvelope


def _valid(**overrides):
    data = {
        "schema_version": "1.0.0",
        "job_id": "job-1",
        "kind": "IMPLEMENT_REQUESTED",
        "idempotency_key": "issue-123:implement:v1",
        "payload": {"issue": 123},
    }
    data.update(overrides)
    return data


def test_accepts_a_well_formed_envelope():
    job = JobEnvelope.from_dict(_valid())
    assert job.job_id == "job-1"
    assert job.kind == "IMPLEMENT_REQUESTED"
    assert job.payload["issue"] == 123


def test_envelope_is_immutable_and_payload_is_read_only():
    job = JobEnvelope.from_dict(_valid())
    with pytest.raises(AttributeError):
        job.job_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        job.payload["issue"] = 999  # type: ignore[index]


def test_rejects_a_contract_major_the_runtime_does_not_support():
    with pytest.raises(ContractError, match="unsupported contract major"):
        JobEnvelope.from_dict(_valid(schema_version="2.0.0"))


def test_rejects_a_malformed_schema_version():
    with pytest.raises(ContractError, match="malformed"):
        JobEnvelope.from_dict(_valid(schema_version="one"))


def test_rejects_an_unknown_kind_instead_of_coercing_it():
    with pytest.raises(ContractError, match="unknown kind"):
        JobEnvelope.from_dict(_valid(kind="DELETE_EVERYTHING"))


@pytest.mark.parametrize("field_name", ["job_id", "idempotency_key", "kind"])
def test_rejects_missing_or_blank_required_fields(field_name):
    with pytest.raises(ContractError, match=f"{field_name} must be a non-empty string"):
        JobEnvelope.from_dict(_valid(**{field_name: "   "}))


def test_rejects_a_non_mapping_payload():
    with pytest.raises(ContractError, match="payload must be a mapping"):
        JobEnvelope.from_dict(_valid(payload=["not", "a", "mapping"]))


def test_rejects_a_non_mapping_envelope():
    with pytest.raises(ContractError, match="must be a mapping"):
        JobEnvelope.from_dict("not a dict")  # type: ignore[arg-type]
