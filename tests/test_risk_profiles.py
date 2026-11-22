"""Risk of a PR in ANOTHER Plan2Space repo (P2S-SESSION-OPERATING-MODEL §6: SPEC/contract, verify.py, validators and
gates are HIGH). Written red first from the harness audit (P2S-S01-H, probe_risk.py): without the repo, a contract SPEC
in engineering-specs came out LOW and the evidence verifier and the engine validators came out MEDIUM."""
import pytest

from agent_runtime.approval import classify_risk
from agent_runtime.merge_gate import GateStatus, PrSnapshot, run_gate

HIGH_CASES = [
    ("Plan2Space/engineering-specs", "plan2space/P2S-CONTRACTS-v1.md"),
    ("Plan2Space/engineering-specs", "plan2space/P2S-SESSION-OPERATING-MODEL.md"),
    ("Plan2Space/engineering-specs", "agent-bar/AGENT-BAR-COCKPIT-ACP.md"),
    ("Plan2Space/engineering-specs", "scripts/check-index.sh"),
    ("Plan2Space/engineering-specs", "plan2space/README.md"),
    ("Plan2Space/evidence", "tools/verify.py"),
    ("Plan2Space/evidence", "tools/check_human_authorship.py"),
    ("Plan2Space/evidence", "planta_74/suite_01/D1/run-1/verdicts/human.json"),
    ("Plan2Space/evidence", "planta_74/suite_01/cameras.v2.json"),
    ("Plan2Space/sketchup-mcp-next", "src/p2s_engine/harness/validate.py"),
    ("Plan2Space/sketchup-mcp-next", "src/p2s_engine/harness/plan_validate.py"),
    ("Plan2Space/sketchup-mcp-next", "src/p2s_engine/harness/functional.py"),
    ("Plan2Space/sketchup-mcp-next", "src/p2s_engine/harness/render_check.py"),
    ("Plan2Space/claude-engineering-skills", "scripts/sanitization-check.sh"),
    ("Plan2Space/claude-engineering-skills", "scripts/repo-integrity-check.sh"),
    ("Plan2Space/claude-engineering-skills", "skills/land-pr/scripts/land_pr.py"),
    ("Plan2Space/claude-engineering-skills", "skills/pr-review/scripts/runtime_risk.py"),
    ("Plan2Space/agent-bar", "agent_bar/cockpit/devmode.py"),
]


@pytest.mark.parametrize("repo,path", HIGH_CASES)
def test_specs_contracts_and_gates_of_other_repos_are_high(repo, path):
    risk, reasons = classify_risk([path], repo=repo)
    assert risk == "HIGH", (repo, path, reasons)


@pytest.mark.parametrize("repo,path", [
    ("Plan2Space/claude-engineering-skills", "skills/land-pr/SKILL.md"),
    ("Plan2Space/claude-engineering-skills", "skills/spec-author/SKILL.md"),
    ("Plan2Space/claude-engineering-skills", "lenses/architecture.md"),
])
def test_agent_instructions_in_the_skills_hub_are_never_low(repo, path):
    assert classify_risk([path], repo=repo)[0] in ("MEDIUM", "HIGH")


def test_renaming_a_gate_out_of_its_profile_stays_high():
    old, new = "tools/verify.py", "docs/verify_notes.md"
    changes = {new: {"status": "renamed", "previous_filename": old, "patch": None}}
    assert classify_risk([new], file_changes=changes, repo="Plan2Space/evidence")[0] == "HIGH"


def test_root_guide_of_the_specs_repo_is_not_a_spec():
    assert classify_risk(["README.md"], repo="Plan2Space/engineering-specs")[0] == "LOW"


def test_repo_name_is_case_insensitive_and_owner_optional():
    assert classify_risk(["tools/verify.py"], repo="plan2space/EVIDENCE")[0] == "HIGH"
    assert classify_risk(["tools/verify.py"], repo="evidence")[0] == "HIGH"


@pytest.mark.parametrize("repo,path,expected", [
    ("Plan2Space/evidence", "README.md", "LOW"),
    ("Plan2Space/sketchup-mcp-next", "docs/notes.md", "LOW"),
    ("Plan2Space/sketchup-mcp-next", "src/p2s_engine/core/planner.py", "MEDIUM"),
    ("Plan2Space/agent-bar", "agent_bar/cockpit/ui/chat.py", "MEDIUM"),
    ("Plan2Space/agent-runtime", "docs/notes.md", "LOW"),
])
def test_profiles_do_not_inflate_ordinary_changes(repo, path, expected):
    assert classify_risk([path], repo=repo)[0] == expected


def test_without_repo_the_classifier_is_unchanged():
    # Back-compat: callers that do not pass a repo keep the runtime's own rules.
    assert classify_risk(["tools/verify.py"])[0] == "MEDIUM"
    assert classify_risk(["src/agent_runtime/merge_gate.py"])[0] == "HIGH"


class _OneSnap:
    def __init__(self, s):
        self.s = s

    def snapshot(self, repo, number):
        return self.s

    def merge(self, repo, number, head_sha):  # pragma: no cover - must never be called
        raise AssertionError("HIGH must never be merged by the runtime")


def test_merge_gate_classifies_with_the_pr_repo(tmp_path):
    from agent_runtime.event_store import EventStore

    repo, f = "Plan2Space/engineering-specs", "plan2space/P2S-CONTRACTS-v1.md"
    s = PrSnapshot(repo=repo, number=9, state="open", draft=False, head_sha="1" * 40, base="develop",
                   body="Evidence: check-index OK", files=[f],
                   file_changes={f: {"status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}}, labels=[],
                   checks=[{"name": "spec index", "status": "completed", "conclusion": "success"}], reviews=[],
                   comments=[], url=f"https://github.com/{repo}/pull/9", author="author-agent")
    res = run_gate(_OneSnap(s), EventStore(tmp_path / "es.sqlite"), repo, 9, base="develop", execute=True)
    assert res.risk == "HIGH"
    assert res.status in (GateStatus.HIGH_HUMAN_ONLY, GateStatus.NOT_ELIGIBLE)
