"""Durable JSON state file: atomic write and tolerant read (issue #20, FP-062).

Contract:
- `write_atomic`: the target never holds a partial write. The new content goes to a
  temporary file in the same directory, is flushed and fsynced, then replaces the target
  with `os.replace`. On any failure the temporary file is removed and the old target stays.
- `load_state`: a corrupt file (empty, truncated, not UTF-8, not a JSON object) is moved
  aside to `<name>.corrupt-<timestamp>-<hex>` and `StateCorrupt` is raised. The bytes are
  preserved for analysis and never deleted. Nothing is recovered automatically.
- A missing file is `StateMissing` unless the caller passes a `default`. A missing file is
  never taken as a first run by accident: the caller must say so, because adopting an empty
  default silently resets deduplication and attempt budgets.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import secrets
from pathlib import Path
from typing import Any

from agent_runtime.durable_file import write_durable


class StateCorrupt(RuntimeError):
    """The state file was unreadable or invalid. It has been quarantined, not deleted."""


class StateMissing(RuntimeError):
    """The state file does not exist and the caller gave no default."""


def _quarantine(path: Path) -> Path:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    dst = path.with_name(f"{path.name}.corrupt-{stamp}-{secrets.token_hex(3)}")
    path.rename(dst)
    return dst


def _parse(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise TypeError("root of the state is not a JSON object")
    return data


def load_state(path: str | Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the state. Missing: the default (a copy) or `StateMissing`. Corrupt: quarantine and `StateCorrupt`."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        if default is None:
            raise StateMissing(f"state file missing: {p.name}") from None
        return copy.deepcopy(default)
    try:
        return _parse(raw)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
        moved = _quarantine(p)
        raise StateCorrupt(f"state file corrupt ({type(exc).__name__}); quarantined as {moved.name}") from exc


def write_atomic(path: str | Path, data: dict[str, Any]) -> None:
    """Replace the state file durably (via `durable_file.write_durable`, the single durable writer).

    `write_durable` does not remove its temporary file when the replace fails; this function does,
    so a failed write leaves only the old target behind."""
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    try:
        write_durable(p, json.dumps(data, ensure_ascii=True, indent=1))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
