from agent_runtime.agents.adapter import RESULT_MAP, TYPED_RESULTS, AgentAdapter
from agent_runtime.agents.contract import BackendUnavailable, Limits
from agent_runtime.agents.harness import Harness
from agent_runtime.agents.resources import NullSampler

from .test_harness import FIXED, Scripted, task, write  # noqa: F401  (fixture reuse)


def test_every_harness_outcome_maps_to_a_typed_result_and_only_success_is_succeeded():
    assert set(RESULT_MAP.values()) <= TYPED_RESULTS
    assert [k for k, v in RESULT_MAP.items() if v == "SUCCEEDED"] == ["SUCCESS"]


def test_start_refuses_with_reason_when_backend_is_not_discoverable(task, tmp_path):
    def factory():
        raise BackendUnavailable("model llama9:70b not listed")
    ad = AgentAdapter("llama", factory)
    assert not ad.discover().available
    r = ad.start(task, tmp_path / "wt", Limits(), run_id="x", out_dir=tmp_path / "o")
    assert r.status == "REFUSED" and "not listed" in r.reason


def test_start_runs_harness_and_result_is_typed(task, tmp_path):
    ad = AgentAdapter("fake", lambda: Scripted([write(FIXED), "ok"]), harness=Harness(sampler_factory=NullSampler))
    assert ad.capabilities()["unrestricted_permissions"] is False
    assert ad.start(task, tmp_path / "wt", Limits(), run_id="x", out_dir=tmp_path / "o") is None
    r = ad.result(timeout=120)
    assert r.status == "SUCCEEDED" and r.outcome == "SUCCESS"


def test_cancel_yields_cancelled(task, tmp_path):
    ad = AgentAdapter("fake", lambda: Scripted([]), harness=Harness(sampler_factory=NullSampler))
    ad.cancel()
    ad.start(task, tmp_path / "wt", Limits(), run_id="x", out_dir=tmp_path / "o")
    assert ad.result(timeout=120).status == "CANCELLED"
