import pytest

from agent_runtime.approval import (ApprovalError, ApprovalRecord, ApprovalStore, classify_risk, from_github_review,
                                    merge_decision)
from agent_runtime.event_store import EventStore

H1 = "a" * 40
H2 = "b" * 40


def rec(sha=H1, who="fmodesto30", risk="HIGH"):
    return ApprovalRecord("Plan2Space/agent-runtime#60", sha, risk, who, "2026-11-20T23:00:00Z",
                          "https://github.com/Plan2Space/agent-runtime/pull/60#pullrequestreview-1")


def test_docs_and_tests_only_is_low():
    assert classify_risk(["docs/x.md", "tests/test_a.py"])[0] == "LOW"


def test_contract_permission_and_ci_paths_are_high():
    for f in ("docs/contracts/agent_event.v1.schema.json", "src/agent_runtime/agents/tools.py", ".github/workflows/ci.yml",
              "src/agent_runtime/event_store.py"):
        assert classify_risk([f, "docs/a.md"])[0] == "HIGH", f


def test_other_code_is_medium_and_label_can_raise_to_high():
    assert classify_risk(["src/agent_runtime/agents/bench.py"])[0] == "MEDIUM"
    assert classify_risk(["docs/a.md"], labels=["geometry"])[0] == "HIGH"
    assert classify_risk([])[0] == "MEDIUM"


def test_low_with_green_ci_merges_automatically():
    d = merge_decision(head_sha=H1, risk="LOW", ci_state="success", evidence_valid=True, approvals=[])
    assert d.eligible and d.automatic


def test_high_needs_human_approval_at_exact_head():
    assert not merge_decision(head_sha=H1, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[]).eligible
    d = merge_decision(head_sha=H1, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[rec(H1)])
    assert d.eligible and not d.automatic and d.approval.head_sha == H1


def test_new_commit_invalidates_previous_approval():
    d = merge_decision(head_sha=H2, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[rec(H1)])
    assert not d.eligible and any("invalidated" in r for r in d.reasons)


@pytest.mark.parametrize("ci,evidence", [("failure", True), ("pending", True), ("success", False)])
def test_failing_ci_or_invalid_evidence_blocks_even_with_approval(ci, evidence):
    for risk in ("LOW", "HIGH"):
        d = merge_decision(head_sha=H1, risk=risk, ci_state=ci, evidence_valid=evidence, approvals=[rec(H1)])
        assert not d.eligible


def test_bot_reviews_are_ignored_and_bot_cannot_be_approver():
    review = {"state": "APPROVED", "commit_id": H1, "user": {"login": "claude[bot]", "type": "Bot"},
              "submitted_at": "t", "html_url": "https://github.com/x/y/pull/1#r"}
    assert from_github_review(review, pr="x/y#1", risk="HIGH") is None
    with pytest.raises(ApprovalError):
        rec(who="claude[bot]")


def test_human_github_review_maps_to_record():
    review = {"state": "APPROVED", "commit_id": H1, "user": {"login": "fmodesto30", "type": "User"},
              "submitted_at": "2026-11-20T23:00:00Z", "html_url": "https://github.com/x/y/pull/1#pullrequestreview-9"}
    r = from_github_review(review, pr="x/y#1", risk="HIGH")
    assert r.head_sha == H1 and r.approver == "fmodesto30"
    assert from_github_review({**review, "state": "COMMENTED"}, pr="x/y#1", risk="HIGH") is None


def test_record_requires_full_sha_and_github_source():
    with pytest.raises(ApprovalError):
        rec(sha="abc123")
    with pytest.raises(ApprovalError):
        ApprovalRecord("x/y#1", H1, "HIGH", "u", "t", "https://evil.example/review")


def test_approvals_persist_in_event_store_idempotently(tmp_path):
    s = ApprovalStore(EventStore(tmp_path / "es.sqlite"))
    s.record(rec(H1))
    s.record(rec(H1))
    s.record(rec(H2))
    got = s.approvals("Plan2Space/agent-runtime#60")
    assert sorted(a.head_sha for a in got) == [H1, H2]
