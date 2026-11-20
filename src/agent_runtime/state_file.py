"""Durable JSON state file: atomic write and tolerant read (issue #20, FP-062).

Contract:
- `write_atomic`: the target never holds a partial write. The new content goes through
  `durable_file.write_durable` (temporary file, flush, fsync, replace). If the write fails,
  the temporary file is removed and the old target is kept. A failure while removing the
  temporary file never hides the original error.
- `load_state`: a corrupt file (empty, truncated, not UTF-8, a UTF-8 BOM, not a JSON object)
  is moved aside to `<name>.corrupt-<timestamp>-<hex>` and `StateCorrupt` is raised. The bytes
  are preserved for analysis and never deleted. Nothing is recovered automatically. If the
  quarantine itself fails, `StateCorrupt` is still raised, the file stays in place, and the
  original cause is chained.
- A missing file is `StateMissing` unless the caller passes a `default`, and a missing parent
  directory is always `StateMissing`. A missing file is never taken as a first run by accident:
  the caller must say so, because adopting an empty default silently resets deduplication and
  attempt budgets.

Premises (not enforced here):
- Single writer per state file. This module takes no lock. Two writers would share the fixed
  temporary name `<name>.tmp` and could interleave. The bridge's lock lives on the executor side,
  outside this repository, so the premise is the caller's responsibility until #19 wires it.
- A concurrent writer that replaces the file between the read and the quarantine rename would
  have its new file moved aside. This is the same single-writer premise.
- The loader checks bytes and JSON structure only. It does not validate the schema of the state;
  a valid JSON object is returned as is.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import secrets
from pathlib import Path
from typing import Any

from agent_runtime.durable_file import write_durable

QUARANTINE_ATTEMPTS = 5


class StateCorrupt(RuntimeError):
    """The state file was unreadable or invalid. It has been quarantined when possible, never deleted."""


class StateMissing(RuntimeError):
    """The state file does not exist (or its directory does) and the caller gave no default."""


def _quarantine(path: Path) -> Path:
    """Rename the corrupt file to a fresh name. Never overwrites: a name collision is retried."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    for _ in range(QUARANTINE_ATTEMPTS):
        dst = path.with_name(f"{path.name}.corrupt-{stamp}-{secrets.token_hex(3)}")
        if not dst.exists():
            path.rename(dst)
            return dst
    raise FileExistsError(f"no free quarantine name after {QUARANTINE_ATTEMPTS} attempts")


def _parse(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw.decode("utf-8"))  # a BOM is not stripped: write_atomic never writes one
    if not isinstance(data, dict):
        raise TypeError("root of the state is not a JSON object")
    return data


def load_state(path: str | Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the state. Missing: the default (a copy) or `StateMissing`. Corrupt: quarantine and `StateCorrupt`."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        if default is None or not p.parent.is_dir():
            raise StateMissing(f"state file missing: {p.name}") from None
        return copy.deepcopy(default)
    try:
        return _parse(raw)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
        try:
            moved = _quarantine(p)
        except OSError as qexc:
            raise StateCorrupt(f"state file corrupt ({type(exc).__name__}) and not quarantined "
                               f"({type(qexc).__name__}): {p.name} left in place") from exc
        raise StateCorrupt(f"state file corrupt ({type(exc).__name__}); quarantined as {moved.name}") from exc


def write_atomic(path: str | Path, data: dict[str, Any]) -> None:
    """Replace the state file durably through `durable_file.write_durable`, the single durable writer.

    `write_durable` leaves its temporary file behind when the replace fails. This function removes it,
    but a failure while removing it is suppressed so the original error is the one the caller sees."""
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    try:
        write_durable(p, json.dumps(data, ensure_ascii=True, indent=1))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
