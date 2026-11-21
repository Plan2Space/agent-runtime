import pytest

from agent_runtime.approval import (ApprovalError, ApprovalRecord, ApprovalStore, classify_risk, from_github_review,
                                    merge_decision)
from agent_runtime.event_store import EventStore

H1 = "a" * 40
H2 = "b" * 40


def rec(sha=H1, who="fmodesto30", risk="HIGH"):
    return ApprovalRecord("Plan2Space/agent-runtime#60", sha, risk, who, "2026-11-20T23:00:00Z",
                          "https://github.com/Plan2Space/agent-runtime/pull/60#pullrequestreview-1")


def test_docs_and_new_tests_only_is_low():
    # Changed on purpose (review of #60, S00-05 risk table): a test file is LOW only when it is provably NEW and
    # does not weaken anything. Without status/patch it is MEDIUM.
    new = {"tests/test_a.py": {"status": "added", "patch": "+def test_a():\n+    assert f() == 1"}}
    # Changed again on purpose (human decision 2026-11-21, S00-05): executable code is never LOW, not even a new test.
    assert classify_risk(["docs/x.md", "tests/test_a.py"], file_changes=new)[0] == "MEDIUM"
    assert classify_risk(["docs/x.md", "docs/guide.rst"])[0] == "LOW"
    assert classify_risk(["docs/conf.py"])[0] == "MEDIUM"          # executable even under docs/
    assert classify_risk(["docs/x.md", "tests/test_a.py"])[0] == "MEDIUM"


def test_contract_permission_and_ci_paths_are_high():
    for f in ("docs/contracts/agent_event.v1.schema.json", "src/agent_runtime/agents/tools.py", ".github/workflows/ci.yml",
              "src/agent_runtime/event_store.py"):
        assert classify_risk([f, "docs/a.md"])[0] == "HIGH", f


def test_other_code_is_medium_and_label_can_raise_to_high():
    assert classify_risk(["src/agent_runtime/agents/bench.py"])[0] == "MEDIUM"
    assert classify_risk(["docs/a.md"], labels=["geometry"])[0] == "HIGH"
    assert classify_risk([])[0] == "MEDIUM"


def test_low_with_green_ci_merges_automatically():
    d = merge_decision(pr="Plan2Space/agent-runtime#60", head_sha=H1, risk="LOW", ci_state="success", evidence_valid=True, approvals=[])
    assert d.eligible and d.automatic


def test_high_needs_human_approval_at_exact_head():
    assert not merge_decision(pr="Plan2Space/agent-runtime#60", head_sha=H1, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[]).eligible
    d = merge_decision(pr="Plan2Space/agent-runtime#60", head_sha=H1, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[rec(H1)])
    assert d.eligible and not d.automatic and d.approval.head_sha == H1


def test_new_commit_invalidates_previous_approval():
    d = merge_decision(pr="Plan2Space/agent-runtime#60", head_sha=H2, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[rec(H1)])
    assert not d.eligible and any("invalidated" in r for r in d.reasons)


@pytest.mark.parametrize("ci,evidence", [("failure", True), ("pending", True), ("success", False)])
def test_failing_ci_or_invalid_evidence_blocks_even_with_approval(ci, evidence):
    for risk in ("LOW", "HIGH"):
        d = merge_decision(pr="Plan2Space/agent-runtime#60", head_sha=H1, risk=risk, ci_state=ci, evidence_valid=evidence, approvals=[rec(H1)])
        assert not d.eligible


def test_bot_reviews_are_ignored_and_bot_cannot_be_approver():
    review = {"state": "APPROVED", "commit_id": H1, "user": {"login": "claude[bot]", "type": "Bot"},
              "submitted_at": "t", "html_url": "https://github.com/x/y/pull/1#r"}
    assert from_github_review(review, pr="x/y#1", risk="HIGH", approvers={"HIGH": ["claude[bot]"]}) is None
    with pytest.raises(ApprovalError):
        rec(who="claude[bot]")


def test_human_github_review_maps_to_record():
    review = {"state": "APPROVED", "commit_id": H1, "user": {"login": "fmodesto30", "type": "User"},
              "submitted_at": "2026-11-20T23:00:00Z", "html_url": "https://github.com/x/y/pull/1#pullrequestreview-9"}
    cfg = {"HIGH": ["fmodesto30"]}
    r = from_github_review(review, pr="x/y#1", risk="HIGH", approvers=cfg)
    assert r.head_sha == H1 and r.approver == "fmodesto30"
    assert from_github_review({**review, "state": "COMMENTED"}, pr="x/y#1", risk="HIGH", approvers=cfg) is None


def _rv(login):
    return {"state": "APPROVED", "commit_id": H1, "user": {"login": login, "type": "User"},
            "submitted_at": "2026-11-21T00:00:00Z", "html_url": "https://github.com/x/y/pull/1#pullrequestreview-1"}


def test_reviewer_account_approved_on_high_is_not_an_approval():
    # the MEDIUM reviewer-agent account never counts for HIGH, even if it is also listed under MEDIUM
    cfg = {"MEDIUM": ["fmmodesto"], "HIGH": ["fmodesto30"]}
    assert from_github_review(_rv("fmmodesto"), pr="x/y#1", risk="HIGH", approvers=cfg) is None
    assert from_github_review(_rv("FMModesto"), pr="x/y#1", risk="MEDIUM", approvers=cfg).approver == "FMModesto"


def test_review_from_a_login_outside_the_list_is_not_an_approval():
    assert from_github_review(_rv("someone"), pr="x/y#1", risk="MEDIUM", approvers={"MEDIUM": ["fmmodesto"]}) is None


def test_empty_or_missing_list_means_nobody_approves():
    for cfg in ({}, {"MEDIUM": []}, {"HIGH": []}):
        for risk in ("LOW", "MEDIUM", "HIGH"):
            assert from_github_review(_rv("fmmodesto"), pr="x/y#1", risk=risk, approvers=cfg) is None


def test_review_by_the_pr_author_is_not_an_approval():
    cfg = {"MEDIUM": ["fmmodesto"]}
    assert from_github_review(_rv("fmmodesto"), pr="x/y#1", risk="MEDIUM", approvers=cfg, author="FMMODESTO") is None


def test_approvers_are_mandatory_fail_closed():
    with pytest.raises(TypeError):
        from_github_review(_rv("fmmodesto"), pr="x/y#1", risk="MEDIUM")   # no approvers -> refuse to guess


def test_review_on_an_old_commit_does_not_count_at_head():
    a = from_github_review(_rv("fmmodesto"), pr="Plan2Space/agent-runtime#60", risk="MEDIUM",
                           approvers={"MEDIUM": ["fmmodesto"]})
    a = ApprovalRecord(pr="Plan2Space/agent-runtime#60", head_sha=a.head_sha, risk="MEDIUM", approver=a.approver,
                       approved_at=a.approved_at, source=a.source)
    d = merge_decision(pr="Plan2Space/agent-runtime#60", head_sha=H2, risk="MEDIUM", ci_state="success",
                       evidence_valid=True, approvals=[a])
    assert not d.eligible


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


# ---------------------------------------------------------------- review of #60: risk classification (P0.2)
from agent_runtime.approval import contains_approval_grammar, from_github_comment  # noqa: E402


@pytest.mark.parametrize("path,info,expected", [
    ("tests/test_a.py", {"status": "modified", "patch": "-x = 1\n+x = 2"}, "MEDIUM"),
    ("tests/test_a.py", {"status": "removed"}, "HIGH"),
    ("tests/test_a.py", {"status": "renamed"}, "HIGH"),
    ("tests/test_a.py", {"status": "added", "patch": "+import pytest\n+@pytest.mark.skip\n+def test_a(): pass"}, "HIGH"),
    ("tests/test_a.py", {"status": "added", "patch": "+@pytest.mark.xfail\n+def test_a(): pass"}, "HIGH"),
    ("tests/test_a.py", {"status": "modified", "patch": "-    assert add(2, 3) == 5\n+    pass"}, "HIGH"),
    ("tests/test_a.py", {"status": "modified", "patch": "+pytest.importorskip('numpy')"}, "HIGH"),
    (r"Tests\Test_A.py", None, "MEDIUM"),          # case/separator variants are still test files, never LOW
    ("./tests/test_a.py", None, "MEDIUM"),
])
def test_changes_that_weaken_or_touch_existing_tests_are_never_low(path, info, expected):
    changes = {path: info} if info else None
    assert classify_risk([path, "docs/x.md"], file_changes=changes)[0] == expected


@pytest.mark.parametrize("path", ["conftest.py", "tests/conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
                                  "pyproject.toml", ".coveragerc", "noxfile.py", ".claude/settings.json",
                                  "src/agent_runtime/agents/mcp_server.py", "src/agent_runtime/outbox_publisher.py"])
def test_gate_configuration_and_agent_permissions_are_high(path):
    assert classify_risk([path])[0] == "HIGH"


def test_agent_instructions_are_at_least_medium_and_skip_word_in_source_is_not_high():
    assert classify_risk(["CLAUDE.md"])[0] == "MEDIUM" and classify_risk(["AGENTS.md", "docs/a.md"])[0] == "MEDIUM"
    src = {"src/agent_runtime/divergence.py": {"status": "modified", "patch": "+    skip = True"}}
    assert classify_risk(list(src), file_changes=src)[0] == "MEDIUM"


# ---------------------------------------------------------------- review of #60: C9 by comment (P0.3)
SHA = "c" * 40


def comment(body=f"APROVADO HIGH {SHA}", login="fmodesto30", utype="User", created="2026-11-21T10:00:00Z",
            updated="2026-11-21T10:00:00Z"):
    return {"body": body, "user": {"login": login, "type": utype}, "created_at": created, "updated_at": updated,
            "html_url": "https://github.com/Plan2Space/agent-runtime/pull/60#issuecomment-1"}


def test_comment_with_exact_grammar_from_an_approver_is_an_approval_at_that_sha():
    r = from_github_comment(comment(), pr="Plan2Space/agent-runtime#60", approvers={"fmodesto30"})
    assert (r.head_sha, r.risk, r.approver, r.kind) == (SHA, "HIGH", "fmodesto30", "comment")
    r2 = from_github_comment(comment(body=f"Revisei.\nAPPROVED MEDIUM {SHA}\nRiscos aceitos."),
                             pr="Plan2Space/agent-runtime#60", approvers={"FMODESTO30"})
    assert r2.risk == "MEDIUM"


@pytest.mark.parametrize("c", [
    comment(login="someone-else"), comment(utype="Bot"), comment(login="helper[bot]"),
    comment(updated="2026-11-21T11:00:00Z"),                                 # edited after creation
    comment(created=None),
    comment(body=f"APROVADO HIGH {SHA[:12]}"), comment(body=f"APROVADO HIGH {SHA.upper()}"),
    comment(body=f"aprovado HIGH {SHA}"), comment(body=f"APROVADO CRITICAL {SHA}"), comment(body=f"LGTM {SHA}"),
    comment(body=f"Não APROVADO HIGH {SHA}"),                                # not a whole line
    comment(body=f"APROVADO HIGH {SHA}\nAPROVADO HIGH {'d' * 40}"),          # ambiguous: two lines
])
def test_comment_that_is_not_an_unambiguous_human_approval_is_ignored(c):
    assert from_github_comment(c, pr="Plan2Space/agent-runtime#60", approvers={"fmodesto30"}) is None


def test_author_can_approve_own_pr_by_comment_and_merge_follows_the_head_rule():
    a = from_github_comment(comment(), pr="Plan2Space/agent-runtime#60", approvers={"fmodesto30"})
    d = merge_decision(pr="Plan2Space/agent-runtime#60", head_sha=SHA, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[a])
    assert d.eligible and not d.automatic and d.approval.kind == "comment"
    stale = merge_decision(pr="Plan2Space/agent-runtime#60", head_sha="e" * 40, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[a])
    assert not stale.eligible and any("invalidated" in r for r in stale.reasons)


def test_approval_declaring_a_lower_risk_does_not_cover_the_pr():
    low = from_github_comment(comment(body=f"APROVADO MEDIUM {SHA}"), pr="Plan2Space/agent-runtime#60",
                              approvers={"fmodesto30"})
    d = merge_decision(pr="Plan2Space/agent-runtime#60", head_sha=SHA, risk="HIGH", ci_state="success", evidence_valid=True, approvals=[low])
    assert not d.eligible and any("lower than HIGH" in r for r in d.reasons)
    ok = merge_decision(pr="Plan2Space/agent-runtime#60", head_sha=SHA, risk="MEDIUM", ci_state="success", evidence_valid=True, approvals=[low])
    assert ok.eligible


def test_comment_approval_round_trips_through_the_store_and_old_records_read_as_review(tmp_path):
    store = ApprovalStore(EventStore(tmp_path / "e.db"))
    a = from_github_comment(comment(), pr="Plan2Space/agent-runtime#60", approvers={"fmodesto30"})
    store.record(a)
    store.record(rec())                         # written the old way (no explicit kind)
    kinds = sorted(r.kind for r in store.approvals("Plan2Space/agent-runtime#60"))
    assert kinds == ["comment", "review"]
    legacy = {k: v for k, v in rec().to_dict().items() if k != "kind"}
    assert ApprovalRecord(**legacy).kind == "review"


def test_approval_grammar_detector_catches_variants_the_runtime_must_never_post():
    for t in (f"APROVADO HIGH {SHA}", f"approved low {SHA[:7]}", f"x\nAprovado Medium {SHA.upper()}\ny"):
        assert contains_approval_grammar(t), t
    assert not contains_approval_grammar("approved the plan, see HIGH risk notes")


def test_merge_gate_module_is_high_risk():
    # the module that gives the runtime merge power can never be merged by that same power
    assert classify_risk(["src/agent_runtime/merge_gate.py"])[0] == "HIGH"
