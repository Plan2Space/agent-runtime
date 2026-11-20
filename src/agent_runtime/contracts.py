"""Versioned job contract between the runtime and its consumers.

Boundary rules:
- No dependency on SketchUp, geometry, or UI code.
- Unknown contract majors and unknown kinds are rejected, never coerced.
- Validation happens at the edge, when the envelope is built from external data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

CONTRACT_VERSION = "1.0.0"
SUPPORTED_MAJOR = 1

ALLOWED_KINDS = frozenset({"IMPLEMENT_REQUESTED", "CORRECTION_REQUESTED"})


class ContractError(ValueError):
    """Raised when external data does not match the job contract."""


def _major(version: str) -> int:
    head, sep, _ = version.partition(".")
    if not sep or not head.isdigit():
        raise ContractError(f"schema_version malformed: {version!r}")
    return int(head)


def _required_text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{key} must be a non-empty string")
    return value


@dataclass(frozen=True)
class JobEnvelope:
    """One unit of work handed from a consumer to the runtime.

    `payload` is always coerced to a read-only mapping in `__post_init__`, so
    direct construction (bypassing `from_dict`) cannot leave it mutable.

    `__hash__` is keyed on identity fields only (not `payload`), because a
    `MappingProxyType` has no `__hash__` of its own: the dataclass-generated
    hash would raise `TypeError` on every call otherwise.
    """

    schema_version: str
    job_id: str
    kind: str
    idempotency_key: str
    payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def __hash__(self) -> int:
        return hash((self.schema_version, self.job_id, self.kind, self.idempotency_key))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JobEnvelope":
        if not isinstance(data, Mapping):
            raise ContractError("job envelope must be a mapping")

        version = _required_text(data, "schema_version")
        if _major(version) != SUPPORTED_MAJOR:
            raise ContractError(
                f"unsupported contract major {version!r}; runtime supports {SUPPORTED_MAJOR}.x"
            )

        kind = _required_text(data, "kind")
        if kind not in ALLOWED_KINDS:
            raise ContractError(f"unknown kind {kind!r}")

        payload = data.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ContractError("payload must be a mapping")

        return cls(
            schema_version=version,
            job_id=_required_text(data, "job_id"),
            kind=kind,
            idempotency_key=_required_text(data, "idempotency_key"),
            payload=MappingProxyType(dict(payload)),
        )
