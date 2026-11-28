"""Cloud-token baseline, measured.

Engineering (before/after Local First): the ``usage`` block Claude Code writes on every assistant
message of its session transcripts (``~/.claude/projects/*/*.jsonl``) — API-reported counts, so
*measured*. A message split over several lines is counted once (``message.id`` + ``requestId``).
Codex usage is not read yet: its sessions directory is empty on this machine, so it is reported as
unknown by the caller, never as zero.

Production: only router ``TOKENS_CONSUMED`` facts with ``kind=cloud``. Measured and estimated are
returned apart and never summed; local tokens are not cloud spend.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .event_store import EventStore


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def claude_transcript_usage(root: str | Path, *, since: str, until: str) -> dict:
    lo, hi = _ts(since), _ts(until)
    seen: set[tuple] = set()
    models: dict[str, dict] = {}
    skipped = 0
    for f in sorted(Path(root).glob("*/*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            m = d.get("message")
            if d.get("type") != "assistant" or not isinstance(m, dict) or "usage" not in m:
                continue
            ts = d.get("timestamp")
            if not ts or not lo <= _ts(ts) < hi:
                continue
            key = (m.get("id"), d.get("requestId"))
            if key in seen:
                continue
            seen.add(key)
            u = m["usage"]
            agg = models.setdefault(m.get("model") or "unknown",
                                    {"messages": 0, "input": 0, "cache_creation": 0, "cache_read": 0, "output": 0})
            agg["messages"] += 1
            agg["input"] += u.get("input_tokens", 0) or 0
            agg["cache_creation"] += u.get("cache_creation_input_tokens", 0) or 0
            agg["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            agg["output"] += u.get("output_tokens", 0) or 0
    return {"kind": "measured", "scope": "engineering", "source": "claude-code transcripts message.usage",
            "since": since, "until": until, "models": dict(sorted(models.items())), "skipped_lines": skipped}


def production_cloud_tokens(store: EventStore) -> dict:
    measured = estimated = unmeasured = 0
    for e in store.events():
        if e["event_type"] != "TOKENS_CONSUMED":
            continue
        p = json.loads(e["payload"])
        if p.get("kind") != "cloud":
            continue
        if p.get("measured") is not None:
            measured += p["measured"]
        else:
            unmeasured += 1
            estimated += p.get("estimated") or 0
    return {"scope": "production", "cloud_measured": measured, "cloud_estimated": estimated,
            "cloud_unmeasured_events": unmeasured}
