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
    "src/agent_runtime/agents/events.py", "src/agent_runtime/agents/harness.py",
    "src/agent_runtime/agents/mcp_server.py", "src/agent_runtime/outbox_publisher.py", ".github/*",
    "*tolerance*", "*geometry*", "schemas/*",
    # test/gate configuration and agent permissions (S00-05 risk table): changing them can switch a gate off
    "conftest.py", "*/conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "*/pyproject.toml",
    ".coveragerc", "noxfile.py", ".claude/*",
)
TEST_PATTERNS = ("tests/*", "*/tests/*", "test_*.py", "*/test_*.py", "*_test.py")
MEDIUM_FLOOR_PATTERNS = ("claude.md", "*/claude.md", "agents.md", "*/agents.md")
LOW_PATTERNS = ("docs/*", "*.md", "benchmark/*/summary.md")
HIGH_LABELS = frozenset({"risk:high", "contract", "geometry", "permissions"})
# A patch weakens a test when it drops an assertion or adds a skip/xfail (S00-05 risk table).
_REMOVED_ASSERT = re.compile(r"^-(?!--)\s*.*\bassert", re.MULTILINE)
_ADDED_SKIP = re.compile(r"^\+(?!\+\+).*(\bskip\b|\bskipif\b|\bxfail\b|importorskip|pytest\.skip\(|unittest\.skip)",
                         re.MULTILINE | re.IGNORECASE)


class ApprovalError(ValueError):
    pass


def _norm(path: str) -> str:
    """Patterns are matched case-insensitively on a normalised POSIX path, the same on Windows and Linux."""
    p = path.replace("\\", "/").lower()
    while p.startswith("./"):
        p = p[2:]
    return p


def _match(path: str, patterns: Iterable[str]) -> bool:
    p = _norm(path)
    return any(fnmatch.fnmatchcase(p, pat.lower()) for pat in patterns)


def weakens_tests(patch: str | None) -> bool:
    return bool(patch) and bool(_REMOVED_ASSERT.search(patch) or _ADDED_SKIP.search(patch))


def classify_risk(changed_files: Sequence[str], labels: Iterable[str] = (), *,
                  file_changes: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[str, list[str]]:
    """Deterministic risk: HIGH beats MEDIUM beats LOW. Empty change sets are MEDIUM (nothing to prove LOW).

    ``file_changes`` maps a path to its GitHub ``status`` (added/modified/removed/renamed) and ``patch``.
    Without it a test file cannot be proven new, so it is never LOW."""
    changes = file_changes or {}
    labels = {lbl.lower() for lbl in labels}
    high: list[str] = [f"high-risk path: {f}" for f in sorted(changed_files) if _match(f, HIGH_PATTERNS)]
    medium: list[str] = []
    for f in sorted(changed_files):
        info = changes.get(f) or {}
        status, patch = info.get("status"), info.get("patch")
        if not _match(f, TEST_PATTERNS):
            if _match(f, MEDIUM_FLOOR_PATTERNS):
                medium.append(f"agent instructions changed: {f}")
            continue
        if weakens_tests(patch):
            high.append(f"patch weakens tests (removed assert or added skip/xfail): {f}")
        if status in ("removed", "renamed"):
            high.append(f"test file {status}: {f}")
        elif status != "added":
            medium.append(f"test file modified or not provably new: {f}")
    high += [f"label: {lbl}" for lbl in sorted(labels & HIGH_LABELS)]
    if high:
        return "HIGH", high
    if medium:
        return "MEDIUM", medium
    if changed_files and all(_match(f, LOW_PATTERNS) or _match(f, TEST_PATTERNS) for f in changed_files):
        return "LOW", ["only docs and new, non-weakening tests"]
    return "MEDIUM", ["code change outside the LOW allowlist" if changed_files else "empty change set"]


@dataclass(frozen=True)
class ApprovalRecord:
    pr: str          # "owner/repo#n"
    head_sha: str
    risk: str
    approver: str
    approved_at: str
    source: str      # URL of the GitHub review or comment
    kind: str = "review"   # review | comment (records written before the field existed are reviews)

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
        if self.kind not in ("review", "comment"):
            raise ApprovalError("kind must be review or comment")

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


# One line, whole line: "APROVADO HIGH <40 hex>" (or APPROVED). The SHA in the text IS the approved HEAD.
APPROVAL_LINE = re.compile(r"^\s*(?:APROVADO|APPROVED)\s+(LOW|MEDIUM|HIGH)\s+([0-9a-f]{40})\s*$", re.MULTILINE)
_LOOSE_APPROVAL = re.compile(r"(?:APROVADO|APPROVED)\s+(?:LOW|MEDIUM|HIGH)\s+[0-9a-fA-F]{7,40}", re.IGNORECASE)


def contains_approval_grammar(text: str) -> bool:
    """True for anything resembling an approval line, case-insensitive (S00-05 R16: runtime writers refuse it)."""
    return bool(_LOOSE_APPROVAL.search(text or ""))


def from_github_comment(comment: Mapping[str, Any], *, pr: str, approvers: Iterable[str]) -> ApprovalRecord | None:
    """Map a PR (issue) comment to a record, or None.

    Accepted only from a configured approver login (a human account), never edited after creation, with exactly
    one approval line. The PR author can always comment, which is why this path exists: GitHub refuses an
    APPROVED review from the author of the PR."""
    user = comment.get("user") or {}
    login = str(user.get("login", ""))
    allowed = {a.lower() for a in approvers}
    if user.get("type") != "User" or login.lower() not in allowed or login.endswith("[bot]"):
        return None
    created, updated = comment.get("created_at"), comment.get("updated_at")
    if not created or (updated and updated != created):
        return None
    lines = APPROVAL_LINE.findall(str(comment.get("body") or ""))
    if len(lines) != 1:
        return None
    risk, sha = lines[0]
    return ApprovalRecord(pr=pr, head_sha=sha, risk=risk, approver=login, approved_at=str(created),
                          source=str(comment.get("html_url", "")), kind="comment")


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
    at_head = [a for a in approvals if a.head_sha == head_sha]
    current = [a for a in at_head if RISKS.index(a.risk) >= RISKS.index(risk)]   # R9a: MEDIUM does not cover HIGH
    stale = [a for a in approvals if a.head_sha != head_sha]
    if current:
        return MergeDecision(True, False, risk, head_sha, (f"human approval by {current[-1].approver} at HEAD",),
                             current[-1])
    reasons.append(f"{risk} requires a human approval at HEAD {head_sha[:12]}")
    if len(at_head) > len(current):
        reasons.append(f"{len(at_head) - len(current)} approval(s) at HEAD declare a risk lower than {risk}")
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
