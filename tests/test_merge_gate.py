import json
import subprocess

import pytest

from agent_runtime.approval import payload_contains_approval_grammar
from agent_runtime.event_store import EventStore
from agent_runtime.merge_gate import (GateStatus, GhCli, MergeOutcome, PrSnapshot, ci_state, normalize_approvers,
                                      run_gate)

REPO = "Plan2Space/agent-runtime"
H1 = "1" * 40
H2 = "2" * 40
GREEN = [{"name": "test", "status": "completed", "conclusion": "success"},
         {"name": "install-check", "status": "completed", "conclusion": "success"}]
LOW_FILES = ("docs/notes.md",)
MEDIUM_FILES = ("src/agent_runtime/agents/bench.py",)
HIGH_FILES = ("src/agent_runtime/merge_gate.py",)
REVIEWER = "reviewer-agent"           # stands for the dedicated reviewer account; never a constant in the code
CFG = {"MEDIUM": [REVIEWER]}


def snap(*, files=LOW_FILES, head=H1, checks=GREEN, state="open", draft=False, body="Evidence: run 1",
         comments=(), reviews=(), base="main", labels=(), author="author-agent"):
    changes = {f: {"status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"} for f in files}
    return PrSnapshot(repo=REPO, number=7, state=state, draft=draft, head_sha=head, base=base, body=body,
                      files=list(files), file_changes=changes, labels=list(labels), checks=list(checks),
                      reviews=list(reviews), comments=list(comments), url=f"https://github.com/{REPO}/pull/7",
                      author=author)


def review(login=REVIEWER, state="APPROVED", commit=H1, n=1):
    return {"state": state, "commit_id": commit, "user": {"login": login, "type": "User"},
            "submitted_at": f"2026-11-21T00:00:0{n}Z", "html_url": f"https://github.com/{REPO}/pull/7#pullrequestreview-{n}"}


class FakeGitHub:
    """Fake of our own GitHubPort: snapshots in order, records merge calls, optional refusal."""

    def __init__(self, *snaps, refuse=None):
        self.snaps = list(snaps)
        self.merges = []
        self.refuse = refuse

    def snapshot(self, repo, number):
        return self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]

    def merge(self, repo, number, head_sha):
        self.merges.append((repo, number, head_sha))
        if self.refuse:
            return MergeOutcome(False, self.refuse, None)
        return MergeOutcome(True, "merged", "f" * 40)


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "es.sqlite")


def facts(store, etype):
    return [json.loads(e["payload"]) if isinstance(e["payload"], str) else e["payload"]
            for e in store.events() if e["event_type"] == etype]


def go(store, s, **kw):
    gh = FakeGitHub(*s) if isinstance(s, tuple) else FakeGitHub(s)
    kw.setdefault("approvers", CFG)
    return run_gate(gh, store, REPO, 7, **kw), gh


# ---------------------------------------------------------------- LOW
def test_dry_run_is_the_default_and_never_merges(store):
    r, gh = go(store, snap())
    assert r.status == GateStatus.DRY_RUN_WOULD_MERGE and gh.merges == []
    assert facts(store, "MERGE_DECISION")[0]["dry_run"] is True


def test_low_with_green_ci_merges_with_match_head(store):
    r, gh = go(store, snap(), execute=True)
    assert r.status == GateStatus.MERGED and gh.merges == [(REPO, 7, H1)]
    m = facts(store, "PR_MERGED")[0]
    assert m["head_sha"] == H1 and m["merge_commit"] == "f" * 40


# ---------------------------------------------------------------- MEDIUM
def test_medium_with_approved_review_at_head_by_configured_reviewer_merges(store):
    r, gh = go(store, snap(files=MEDIUM_FILES, reviews=(review(),)), execute=True)
    assert r.status == GateStatus.MERGED and gh.merges == [(REPO, 7, H1)] and r.risk == "MEDIUM"
    assert facts(store, "PR_MERGED")[0]["approval_source"].endswith("pullrequestreview-1")


def test_medium_without_review_does_not_merge(store):
    r, gh = go(store, snap(files=MEDIUM_FILES), execute=True)
    assert r.status == GateStatus.WAITING_HUMAN and gh.merges == []


def test_medium_with_approved_review_from_another_login_does_not_merge(store):
    r, gh = go(store, snap(files=MEDIUM_FILES, reviews=(review(login="someone-else"),)), execute=True)
    assert r.status == GateStatus.WAITING_HUMAN and gh.merges == []
    assert any("not a configured MEDIUM approver" in x for x in r.reasons)


def test_medium_review_on_an_old_head_does_not_merge(store):
    r, gh = go(store, snap(files=MEDIUM_FILES, head=H2, reviews=(review(commit=H1),)), execute=True)
    assert r.status == GateStatus.WAITING_HUMAN and gh.merges == []
    assert any("invalidated by newer commits" in x for x in r.reasons)


def test_medium_review_by_the_pr_author_is_ignored(store):
    r, gh = go(store, snap(files=MEDIUM_FILES, author=REVIEWER, reviews=(review(),)), execute=True)
    assert r.status == GateStatus.WAITING_HUMAN and gh.merges == []
    assert any("approver is the PR author" in x for x in r.reasons)


def test_medium_approval_withdrawn_by_a_later_changes_requested_does_not_merge(store):
    rv = (review(n=1), review(state="CHANGES_REQUESTED", n=2))
    r, gh = go(store, snap(files=MEDIUM_FILES, reviews=rv), execute=True)
    assert r.status == GateStatus.WAITING_HUMAN and gh.merges == []


def test_medium_with_no_configured_approver_never_merges(store):
    r, gh = go(store, snap(files=MEDIUM_FILES, reviews=(review(),)), execute=True, approvers={})
    assert r.status == GateStatus.WAITING_HUMAN and gh.merges == []
    assert "no MEDIUM approver configured" in facts(store, "MERGE_DECISION")[-1]["needs"]


def test_pr_comments_never_trigger_a_merge(store):
    c = {"user": {"login": REVIEWER, "type": "User"}, "created_at": "t", "updated_at": "t",
         "body": f"APROVADO MEDIUM {H1}", "html_url": f"https://github.com/{REPO}/pull/7#issuecomment-1"}
    r, gh = go(store, snap(files=MEDIUM_FILES, comments=(c,)), execute=True)
    assert r.status == GateStatus.WAITING_HUMAN and gh.merges == []


# ---------------------------------------------------------------- HIGH
def test_high_with_approved_review_by_the_reviewer_is_never_merged(store):
    for cfg in (CFG, {"MEDIUM": [REVIEWER], "HIGH": [REVIEWER]}):
        r, gh = go(store, snap(files=HIGH_FILES, reviews=(review(),)), execute=True, approvers=cfg)
        assert r.status == GateStatus.HIGH_HUMAN_ONLY and gh.merges == [] and r.risk == "HIGH"
    assert "coordinator merges" in facts(store, "MERGE_DECISION")[-1]["needs"]


# ---------------------------------------------------------------- shared rules
def test_head_changed_between_decision_and_merge_does_not_merge(store):
    r, gh = go(store, (snap(head=H1), snap(head=H2)), execute=True)
    assert r.status == GateStatus.HEAD_CHANGED and gh.merges == []


@pytest.mark.parametrize("checks,state", [
    ([{"name": "test", "status": "completed", "conclusion": "failure"}], "failure"),
    ([{"name": "test", "status": "in_progress", "conclusion": None}], "pending"),
    ([], "missing")])
def test_red_pending_or_missing_ci_does_not_merge(store, checks, state):
    for files, reviews in ((LOW_FILES, ()), (MEDIUM_FILES, (review(),))):
        r, gh = go(store, snap(files=files, checks=checks, reviews=reviews), execute=True)
        assert r.status == GateStatus.NOT_ELIGIBLE and gh.merges == [] and r.ci_state == state


def test_github_refusal_becomes_blocked_with_reason(store):
    gh = FakeGitHub(snap(), refuse="GraphQL: Base branch policy prohibits the merge")
    r = run_gate(gh, store, REPO, 7, approvers=CFG, execute=True)
    assert r.status == GateStatus.BLOCKED and "policy prohibits" in r.reason
    assert facts(store, "MERGE_DECISION")[-1]["status"] == "BLOCKED" and facts(store, "PR_MERGED") == []


@pytest.mark.parametrize("over,reason", [({"draft": True}, "draft"), ({"body": "  "}, "evidence"),
                                         ({"base": "develop"}, "base")])
def test_draft_empty_evidence_or_wrong_base_is_not_eligible(store, over, reason):
    r, gh = go(store, snap(**over), execute=True)
    assert r.status == GateStatus.NOT_ELIGIBLE and gh.merges == [] and any(reason in x for x in r.reasons)


def test_closed_pr_is_reported_not_merged(store):
    assert go(store, snap(state="closed"), execute=True)[0].status == GateStatus.NOT_OPEN


def test_recorded_facts_never_contain_approval_grammar(store):
    c = {"user": {"login": REVIEWER, "type": "User"}, "created_at": "t", "updated_at": "t",
         "body": f"APROVADO HIGH {H1}", "html_url": f"https://github.com/{REPO}/pull/7#issuecomment-1"}
    for files in (LOW_FILES, MEDIUM_FILES, HIGH_FILES):
        go(store, snap(files=files, comments=(c,), reviews=(review(),)), execute=True)
    for e in store.events():
        p = json.loads(e["payload"]) if isinstance(e["payload"], str) else e["payload"]
        assert not payload_contains_approval_grammar(p)


def test_waiting_state_is_readable_for_the_ui(store):
    go(store, snap(files=MEDIUM_FILES))
    f = facts(store, "MERGE_DECISION")[-1]
    assert f["status"] == "WAITING_HUMAN" and f["risk"] == "MEDIUM" and f["head_sha"] == H1
    assert f["pr"] == f"{REPO}#7" and REVIEWER in f["needs"] and f["approvers"] == {"MEDIUM": [REVIEWER]}


def test_approver_config_is_validated_and_case_insensitive():
    assert normalize_approvers({"medium": [" Reviewer-Agent "]}) == {"MEDIUM": frozenset({"reviewer-agent"})}
    with pytest.raises(ValueError):
        normalize_approvers({"CRITICAL": ["x"]})


def test_ci_state_rules():
    assert ci_state(GREEN) == "success"
    assert ci_state(GREEN + [{"name": "x", "status": "completed", "conclusion": "skipped"}]) == "success"
    assert ci_state([{"name": "x", "status": "completed", "conclusion": "cancelled"}]) == "failure"
    assert ci_state([]) == "missing"


# ---------------------------------------------------------------- gh adapter contract (canned REST shapes)
def canned(api, merge_rc=0, merge_err=""):
    class C(GhCli):
        calls = []

        def _api(self, path):
            return api[path.split("?")[0]]

        def _run(self, args):
            C.calls.append(args)
            if args[:2] == ["pr", "merge"] and merge_rc == 0:
                api[f"repos/{REPO}/pulls/7"]["merged"] = True     # GitHub state after a successful merge
            return subprocess.CompletedProcess(args, merge_rc, "", merge_err)
    return C(exe="gh")


def rest(files, reviews=(), merged=False, author="author-agent"):
    pr = f"repos/{REPO}/pulls/7"
    return {pr: {"state": "open", "merged": merged, "draft": False, "head": {"sha": H1}, "base": {"ref": "main"},
                 "body": "evidence", "labels": [], "html_url": f"https://github.com/{REPO}/pull/7",
                 "merge_commit_sha": "e" * 40, "user": {"login": author, "type": "User"}},
            pr + "/files": [{"filename": f, "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"} for f in files],
            f"repos/{REPO}/commits/{H1}/check-runs": {"check_runs": [{"name": "test", "status": "completed",
                                                                     "conclusion": "success"}]},
            pr + "/reviews": list(reviews), f"repos/{REPO}/issues/7/comments": []}


def test_contract_medium_review_payload_from_github_leads_to_a_match_head_merge(store):
    gh = canned(rest(MEDIUM_FILES, reviews=[review()]))
    r = run_gate(gh, store, REPO, 7, approvers=CFG, execute=True)
    args = type(gh).calls[-1]
    assert r.status == GateStatus.MERGED and r.risk == "MEDIUM" and r.merge_commit == "e" * 40
    assert args[:2] == ["pr", "merge"] and args[args.index("--match-head-commit") + 1] == H1
    assert "--admin" not in args and "--auto" not in args and "--merge" in args


def test_contract_snapshot_maps_author_files_checks():
    s = canned(rest(LOW_FILES, author="someone")).snapshot(REPO, 7)
    assert (s.head_sha, s.base, s.files, s.author) == (H1, "main", list(LOW_FILES), "someone")
    assert ci_state(s.checks) == "success"


def test_contract_github_refusal_is_not_ok_with_reason():
    out = canned(rest(LOW_FILES), merge_rc=1, merge_err="X the base branch policy prohibits the merge").merge(REPO, 7, H1)
    assert not out.ok and "policy prohibits" in out.detail
