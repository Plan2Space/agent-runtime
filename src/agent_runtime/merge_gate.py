"""Merge gate (S00-14): the runtime reads a PR from GitHub, decides, and merges LOW and approved MEDIUM.

Policy (Felipe, 2026-11-21):

- LOW + CI green + valid evidence -> the runtime merges.
- MEDIUM + CI green + valid evidence + a GitHub review in state APPROVED, at the exact HEAD, by a login configured
  as a MEDIUM approver (e.g. a dedicated reviewer-agent account), whose latest review is that approval, and who is
  not the PR author -> the runtime merges. Anything less -> WAITING_HUMAN.
- HIGH -> the runtime NEVER merges, whatever approvals exist (defence in depth). HIGH is approved by the human in
  chat and merged by the coordinator, pinned to the HEAD. State: HIGH_HUMAN_ONLY.

Approvers per level are configuration (``{"MEDIUM": ["login"]}``); no login is a constant here. PR comments never
trigger a merge: only GitHub reviews do.

Mechanics: dry-run by default (``execute=True`` to merge); the PR is re-read right before merging (HEAD changed ->
no merge) and GitHub is called with ``--match-head-commit <sha>``; never ``--admin``/``--auto``; branch protection is
never touched; a GitHub refusal is BLOCKED with GitHub's reason. Every run records a ``MERGE_DECISION`` fact and a
real merge also ``PR_MERGED``; no fact carries approval grammar (S00-05 R16).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol

from .approval import (ApprovalError, ApprovalRecord, classify_risk, from_github_review, merge_decision,
                       payload_contains_approval_grammar)

GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
AUTO_MERGE_LEVELS = ("LOW", "MEDIUM")   # HIGH is never merged by the runtime


class GateStatus(str, Enum):
    DRY_RUN_WOULD_MERGE = "DRY_RUN_WOULD_MERGE"
    MERGED = "MERGED"
    WAITING_HUMAN = "WAITING_HUMAN"        # MEDIUM without a valid APPROVED review at HEAD
    HIGH_HUMAN_ONLY = "HIGH_HUMAN_ONLY"    # HIGH: approval in chat + merge by the coordinator, never the runtime
    NOT_ELIGIBLE = "NOT_ELIGIBLE"          # CI not green, evidence invalid
    HEAD_CHANGED = "HEAD_CHANGED"
    BLOCKED = "BLOCKED"                    # GitHub refused the merge
    NOT_OPEN = "NOT_OPEN"


@dataclass(frozen=True)
class PrSnapshot:
    repo: str
    number: int
    state: str
    draft: bool
    head_sha: str
    base: str
    body: str
    files: list[str]
    file_changes: dict[str, dict]
    labels: list[str]
    checks: list[dict]
    reviews: list[dict]
    comments: list[dict]
    url: str = ""
    author: str = ""


@dataclass(frozen=True)
class MergeOutcome:
    ok: bool
    detail: str
    merge_commit: str | None


class GitHubPort(Protocol):
    def snapshot(self, repo: str, number: int) -> PrSnapshot: ...

    def merge(self, repo: str, number: int, head_sha: str) -> MergeOutcome: ...


@dataclass
class GateResult:
    status: GateStatus
    pr: str
    head_sha: str
    risk: str | None
    ci_state: str | None
    reasons: list[str] = field(default_factory=list)
    reason: str | None = None
    merge_commit: str | None = None
    dry_run: bool = True
    approval: ApprovalRecord | None = None


def ci_state(checks: Iterable[Mapping[str, Any]]) -> str:
    checks = list(checks)
    if not checks:
        return "missing"
    if any(c.get("status") != "completed" for c in checks):
        return "pending"
    if any((c.get("conclusion") or "") not in GREEN_CONCLUSIONS for c in checks):
        return "failure"
    return "success"


def evidence_reasons(s: PrSnapshot, base: str) -> list[str]:
    out = []
    if s.draft:
        out.append("PR is a draft")
    if not (s.body or "").strip():
        out.append("evidence missing: PR body is empty")
    if s.base != base:
        out.append(f"base is {s.base!r}, expected {base!r}")
    return out


def normalize_approvers(approvers: Mapping[str, Iterable[str]] | None) -> dict[str, frozenset[str]]:
    """``{"MEDIUM": ["login", ...]}`` -> lower-cased sets. Unknown levels are rejected; HIGH is accepted but ignored."""
    out: dict[str, frozenset[str]] = {}
    for level, logins in (approvers or {}).items():
        lv = level.upper()
        if lv not in ("LOW", "MEDIUM", "HIGH"):
            raise ValueError(f"unknown approver level {level!r}")
        out[lv] = frozenset(x.strip().lower() for x in logins if x.strip())
    return out


def review_approvals(s: PrSnapshot, pr: str, risk: str, allowed: frozenset[str]) -> tuple[list[ApprovalRecord], list[str]]:
    """APPROVED reviews that can merge: configured login, not the PR author, latest review of that login is the
    approval (a later CHANGES_REQUESTED/DISMISSED withdraws it). HEAD matching is left to ``merge_decision``."""
    notes: list[str] = []
    latest: dict[str, Mapping[str, Any]] = {}
    for rv in s.reviews:                       # GitHub lists reviews chronologically
        login = str((rv.get("user") or {}).get("login", "")).lower()
        if rv.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = rv
    out = []
    for login, rv in latest.items():
        if login not in allowed:
            if rv.get("state") == "APPROVED":
                notes.append(f"APPROVED review by {login} ignored: not a configured {risk} approver")
            continue
        if login == s.author.lower():
            notes.append(f"review by {login} ignored: approver is the PR author")
            continue
        if rv.get("state") != "APPROVED":
            notes.append(f"latest review by {login} is {rv.get('state')}: no approval")
            continue
        try:
            a = from_github_review(rv, pr=pr, risk=risk)
        except ApprovalError as exc:
            notes.append(f"review by {login} rejected: {exc}")
            continue
        if a:
            out.append(a)
    return out, notes


def run_gate(gh: GitHubPort, store, repo: str, number: int, *,
             approvers: Mapping[str, Iterable[str]] | None = None, execute: bool = False, base: str = "main",
             clock: Callable[[], float] = time.time) -> GateResult:
    levels = normalize_approvers(approvers)
    pr = f"{repo}#{number}"
    s = gh.snapshot(repo, number)
    if s.state != "open":
        res = GateResult(GateStatus.NOT_OPEN, pr, s.head_sha, None, None, [f"PR state is {s.state!r}"],
                         dry_run=not execute)
        _record(store, res, s, clock, risk_reasons=[], levels=levels)
        return res
    risk, risk_reasons = classify_risk(s.files, s.labels, file_changes=s.file_changes)
    ci = ci_state(s.checks)
    ev = evidence_reasons(s, base)
    notes: list[str] = []
    approvals: list[ApprovalRecord] = []
    if risk == "MEDIUM":
        approvals, notes = review_approvals(s, pr, risk, levels.get("MEDIUM", frozenset()))
    d = merge_decision(pr=pr, head_sha=s.head_sha, risk=risk, ci_state=ci, evidence_valid=not ev, approvals=approvals)
    reasons = list(d.reasons) + ev + notes
    if risk == "HIGH":
        res = GateResult(GateStatus.HIGH_HUMAN_ONLY if ci == "success" and not ev else GateStatus.NOT_ELIGIBLE, pr,
                         s.head_sha, risk, ci,
                         reasons + ["HIGH is never merged by the runtime: human approval in chat, merge by the "
                                    "coordinator pinned to HEAD"], dry_run=not execute)
    elif not d.eligible:
        waiting = risk == "MEDIUM" and ci == "success" and not ev
        res = GateResult(GateStatus.WAITING_HUMAN if waiting else GateStatus.NOT_ELIGIBLE, pr, s.head_sha, risk, ci,
                         reasons, dry_run=not execute)
    elif not execute:
        res = GateResult(GateStatus.DRY_RUN_WOULD_MERGE, pr, s.head_sha, risk, ci, reasons, dry_run=True,
                         approval=d.approval)
    else:
        again = gh.snapshot(repo, number)
        if again.head_sha != s.head_sha:
            res = GateResult(GateStatus.HEAD_CHANGED, pr, s.head_sha, risk, ci,
                             reasons + [f"HEAD moved to {again.head_sha} after the decision"], dry_run=False)
        else:
            out = gh.merge(repo, number, s.head_sha)
            if out.ok:
                res = GateResult(GateStatus.MERGED, pr, s.head_sha, risk, ci, reasons, merge_commit=out.merge_commit,
                                 dry_run=False, approval=d.approval)
            else:
                res = GateResult(GateStatus.BLOCKED, pr, s.head_sha, risk, ci, reasons, reason=out.detail,
                                 dry_run=False, approval=d.approval)
    _record(store, res, s, clock, risk_reasons=risk_reasons, levels=levels)
    return res


def _needs(res: GateResult, levels: Mapping[str, frozenset[str]]) -> str | None:
    if res.status == GateStatus.WAITING_HUMAN:
        who = sorted(levels.get("MEDIUM", frozenset()))
        return (f"APPROVED GitHub review at HEAD {res.head_sha} by one of {who}" if who
                else "no MEDIUM approver configured: a human must review and merge")
    if res.status == GateStatus.HIGH_HUMAN_ONLY:
        return f"human approval in chat for HEAD {res.head_sha}; the coordinator merges"
    if res.status == GateStatus.BLOCKED:
        return "human: GitHub refused the merge"
    return None


def _record(store, res: GateResult, s: PrSnapshot, clock, *, risk_reasons: list[str],
            levels: Mapping[str, frozenset[str]]) -> None:
    if store is None:
        return
    a = res.approval
    payload = {"pr": res.pr, "url": s.url, "author": s.author, "head_sha": res.head_sha, "base": s.base,
               "status": res.status.value, "risk": res.risk, "risk_reasons": risk_reasons, "ci_state": res.ci_state,
               "checks": [{"name": c.get("name"), "conclusion": c.get("conclusion")} for c in s.checks],
               "reasons": res.reasons, "reason": res.reason, "needs": _needs(res, levels), "dry_run": res.dry_run,
               "approvers": {k: sorted(v) for k, v in levels.items()},
               "approval": None if a is None else {"approver": a.approver, "kind": a.kind, "risk": a.risk,
                                                   "head_sha": a.head_sha, "source": a.source},
               "decided_at": clock(), "policy": "LOW auto; MEDIUM auto with APPROVED review at HEAD; HIGH never"}
    _publish(store, "MERGE_DECISION", payload, res.pr)
    if res.status == GateStatus.MERGED:
        _publish(store, "PR_MERGED", {"pr": res.pr, "head_sha": res.head_sha, "merge_commit": res.merge_commit,
                                      "risk": res.risk, "merged_by": "agent-runtime merge gate",
                                      "approval_source": None if a is None else a.source, "merged_at": clock()},
                 res.pr)


def _publish(store, etype: str, payload: dict, pr: str) -> None:
    if payload_contains_approval_grammar(payload):          # R16: never write something readable as an approval
        raise ApprovalError(f"refusing to record {etype}: payload contains approval grammar")
    store.publish(etype, f"merge-gate:{etype}:{pr}:{uuid.uuid4().hex}", pr, payload, aggregate_type="pull_request",
                  producer="merge_gate", actor="merge_gate")


# ---------------------------------------------------------------------------------------------- gh adapter
class GhCliError(RuntimeError):
    pass


class GhCli:
    """GitHubPort over the ``gh`` CLI (keyring auth; GH_TOKEN/GITHUB_TOKEN are dropped from the child env)."""

    def __init__(self, exe: str | None = None, timeout_s: float = 120):
        self.exe = exe or shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
        self.timeout_s = timeout_s

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        return subprocess.run([self.exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=self.timeout_s, env=env, check=False)

    def _api(self, path: str) -> Any:
        r = self._run(["api", "-H", "Accept: application/vnd.github+json", path])
        if r.returncode != 0:
            raise GhCliError(f"gh api {path} failed: {r.stderr.strip()[:300]}")
        return json.loads(r.stdout)

    def snapshot(self, repo: str, number: int) -> PrSnapshot:
        p = self._api(f"repos/{repo}/pulls/{number}")
        files = self._api(f"repos/{repo}/pulls/{number}/files?per_page=100")
        if len(files) >= 100:
            raise GhCliError("100+ changed files: not paginated here, refusing to classify a partial change set")
        head = p["head"]["sha"]
        runs = self._api(f"repos/{repo}/commits/{head}/check-runs?per_page=100").get("check_runs", [])
        reviews = self._api(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
        comments = self._api(f"repos/{repo}/issues/{number}/comments?per_page=100")
        state = "merged" if p.get("merged") else p["state"]
        return PrSnapshot(
            repo=repo, number=number, state=state, draft=bool(p.get("draft")), head_sha=head,
            base=p["base"]["ref"], body=p.get("body") or "",
            files=[f["filename"] for f in files],
            file_changes={f["filename"]: {"status": f.get("status"), "patch": f.get("patch"),
                                          "previous_filename": f.get("previous_filename")} for f in files},
            labels=[lb["name"] for lb in p.get("labels", [])],
            checks=[{"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion")} for c in runs],
            reviews=reviews, comments=comments, url=p.get("html_url", ""),
            author=str((p.get("user") or {}).get("login", "")))

    def merge(self, repo: str, number: int, head_sha: str) -> MergeOutcome:
        # merge commit (no squash) keeps the PR's SHAs; no --admin, no --auto, no --delete-branch
        r = self._run(["pr", "merge", str(number), "--repo", repo, "--merge", "--match-head-commit", head_sha])
        if r.returncode != 0:
            return MergeOutcome(False, (r.stderr or r.stdout).strip()[:500] or f"gh exit {r.returncode}", None)
        p = self._api(f"repos/{repo}/pulls/{number}")
        if not p.get("merged"):
            return MergeOutcome(False, "gh reported success but the PR is not merged", None)
        return MergeOutcome(True, (r.stdout or "merged").strip()[:300], p.get("merge_commit_sha"))


def result_dict(r: GateResult) -> dict:
    d = asdict(r)
    d["status"] = r.status.value
    return d
