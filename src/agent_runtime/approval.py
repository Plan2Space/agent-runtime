"""Risk classification, human approval bound to the exact HEAD, and merge eligibility (S00-14).

Record shape follows engineering-specs S00-02 §C9: ``pr``, ``head_sha``, ``risk``, ``approver``,
``approved_at``, ``source``. Approvals come from GitHub reviews (auditable there) and are persisted
by the runtime as ``GITHUB_REVIEW_SUBMITTED`` facts in the EventStore; the Agent Bar only reads them.

Rules:
- LOW with green CI and valid evidence is eligible for automatic merge.
- MEDIUM/HIGH need a human approval whose ``head_sha`` equals the current HEAD. A new commit makes
  every earlier approval stale.
- Failing/pending CI or invalid evidence blocks the merge, whatever the approvals say.
- Bot/LLM reviews are ignored: they are neither required nor sufficient.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

RISKS = ("LOW", "MEDIUM", "HIGH")
_SHA = re.compile(r"^[0-9a-f]{40}$")

HIGH_PATTERNS = (
    "docs/contracts/*", "src/agent_runtime/contracts.py", "src/agent_runtime/event_store.py",
    "src/agent_runtime/approval.py", "src/agent_runtime/agents/tools.py", "src/agent_runtime/agents/claude_cli.py",
    "src/agent_runtime/agents/events.py", "src/agent_runtime/agents/harness.py", ".github/*",
    "*tolerance*", "*geometry*", "schemas/*",
)
LOW_PATTERNS = ("docs/*", "*.md", "tests/*", "benchmark/*/summary.md")
HIGH_LABELS = frozenset({"risk:high", "contract", "geometry", "permissions"})


class ApprovalError(ValueError):
    pass


def _match(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def classify_risk(changed_files: Sequence[str], labels: Iterable[str] = ()) -> tuple[str, list[str]]:
    """Deterministic risk: HIGH beats MEDIUM beats LOW. Empty change sets are MEDIUM (nothing to prove LOW)."""
    reasons: list[str] = []
    labels = {lbl.lower() for lbl in labels}
    high = sorted(f for f in changed_files if _match(f, HIGH_PATTERNS))
    if high or labels & HIGH_LABELS:
        reasons += [f"high-risk path: {f}" for f in high] + [f"label: {lbl}" for lbl in sorted(labels & HIGH_LABELS)]
        return "HIGH", reasons
    if changed_files and all(_match(f, LOW_PATTERNS) for f in changed_files):
        return "LOW", ["only docs/tests changed"]
    return "MEDIUM", ["code change outside the LOW allowlist" if changed_files else "empty change set"]


@dataclass(frozen=True)
class ApprovalRecord:
    pr: str          # "owner/repo#n"
    head_sha: str
    risk: str
    approver: str
    approved_at: str
    source: str      # URL of the GitHub review

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", self.pr):
            raise ApprovalError(f"pr must be owner/repo#n, got {self.pr!r}")
        if not _SHA.match(self.head_sha):
            raise ApprovalError("head_sha must be a full 40-char lowercase SHA")
        if self.risk not in RISKS:
            raise ApprovalError(f"risk must be one of {RISKS}")
        if not self.approver or self.approver.endswith("[bot]"):
            raise ApprovalError("approver must be a human GitHub login")
        if not self.source.startswith("https://github.com/"):
            raise ApprovalError("source must be a github.com URL (auditable)")

    def to_dict(self) -> dict:
        return asdict(self)


def from_github_review(review: Mapping[str, Any], *, pr: str, risk: str) -> ApprovalRecord | None:
    """Map a GitHub review payload to a record; None unless it is an APPROVED review by a human user."""
    user = review.get("user") or {}
    if review.get("state") != "APPROVED" or user.get("type") != "User":
        return None
    return ApprovalRecord(pr=pr, head_sha=str(review.get("commit_id", "")), risk=risk,
                          approver=str(user.get("login", "")), approved_at=str(review.get("submitted_at", "")),
                          source=str(review.get("html_url", "")))


@dataclass(frozen=True)
class MergeDecision:
    eligible: bool
    automatic: bool
    risk: str
    head_sha: str
    reasons: tuple[str, ...]
    approval: ApprovalRecord | None = None


def merge_decision(*, head_sha: str, risk: str, ci_state: str, evidence_valid: bool,
                   approvals: Iterable[ApprovalRecord]) -> MergeDecision:
    if risk not in RISKS:
        raise ApprovalError(f"unknown risk {risk!r}")
    reasons: list[str] = []
    if ci_state != "success":
        reasons.append(f"CI is {ci_state!r}, not success")
    if not evidence_valid:
        reasons.append("evidence invalid or missing")
    if reasons:
        return MergeDecision(False, False, risk, head_sha, tuple(reasons))
    if risk == "LOW":
        return MergeDecision(True, True, risk, head_sha, ("LOW risk with green CI and valid evidence",))
    approvals = list(approvals)
    current = [a for a in approvals if a.head_sha == head_sha]
    stale = [a for a in approvals if a.head_sha != head_sha]
    if current:
        return MergeDecision(True, False, risk, head_sha, (f"human approval by {current[-1].approver} at HEAD",),
                             current[-1])
    reasons.append(f"{risk} requires a human approval at HEAD {head_sha[:12]}")
    if stale:
        reasons.append(f"{len(stale)} approval(s) invalidated by newer commits")
    return MergeDecision(False, False, risk, head_sha, tuple(reasons))


class ApprovalStore:
    """Persists approvals as GITHUB_REVIEW_SUBMITTED facts (RECORDED, not consumable) in the EventStore."""

    def __init__(self, store):
        self.store = store

    def record(self, a: ApprovalRecord) -> int:
        key = f"approval:{a.pr}:{a.head_sha}:{a.approver}"
        eid, _ = self.store.publish("GITHUB_REVIEW_SUBMITTED", key, a.pr, a.to_dict(), aggregate_type="approval",
                                    producer="approval", actor="approval")
        return eid

    def approvals(self, pr: str) -> list[ApprovalRecord]:
        out = []
        for e in self.store.events(pr):
            if e["event_type"] == "GITHUB_REVIEW_SUBMITTED":
                import json
                payload = e["payload"] if isinstance(e["payload"], dict) else json.loads(e["payload"])
                out.append(ApprovalRecord(**payload))
        return out
