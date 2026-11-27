"""One governance for Claude Code and Codex (adendo §1).

Both engineering agents register with the same identity shape and claim tasks through the same
EventStore facts, so the Agent Bar sees them side by side and a task has one owner whichever agent
it is. There is no Codex-specific path.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .event_store import EventStore

AGENTS = frozenset({"claude-code", "codex"})


class OwnershipConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentIdentity:
    agent: str
    session_id: str
    frente: str
    worktree: str
    task_id: str

    def __post_init__(self) -> None:
        if self.agent not in AGENTS:
            raise ValueError(f"unknown agent {self.agent!r}; expected one of {sorted(AGENTS)}")
        for k in ("session_id", "frente", "worktree", "task_id"):
            if not getattr(self, k):
                raise ValueError(f"{k} must be non-empty")


def register_session(store: EventStore, who: AgentIdentity) -> int:
    eid, _ = store.publish("AGENT_SESSION_REGISTERED", f"agent:{who.agent}:{who.session_id}:registered",
                           who.task_id, asdict(who), aggregate_type="agent_task", producer=who.agent)
    return eid


def current_owner(store: EventStore, task_id: str) -> dict | None:
    owner = None
    for e in store.events(job_id=task_id):
        if e["event_type"] == "TASK_OWNERSHIP_CLAIMED":
            owner = json.loads(e["payload"])
        elif e["event_type"] == "TASK_OWNERSHIP_RELEASED":
            owner = None
    return owner


def claim_task(store: EventStore, who: AgentIdentity) -> None:
    owner = current_owner(store, who.task_id)
    if owner is not None:
        if (owner["agent"], owner["session_id"]) == (who.agent, who.session_id):
            return
        raise OwnershipConflict(f"{who.task_id} is owned by {owner['agent']}:{owner['session_id']}")
    store.publish("TASK_OWNERSHIP_CLAIMED", f"agent:{who.task_id}:{who.agent}:{who.session_id}:claimed",
                  who.task_id, asdict(who), aggregate_type="agent_task", producer=who.agent)
