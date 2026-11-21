"""Run the S00-11 benchmark for one backend and write benchmark/<backend>/.

Usage (from the repo, with the repo venv):
    python scripts/run_benchmark.py --backend qwen --repeats 3 --work I:/p2s-b-bench
    python scripts/run_benchmark.py --backend smoke-all --work I:/p2s-b-bench   # S00-05 real calls

One backend at a time (shared GPU). Unavailable backend => BLOCKED record, never simulated.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_runtime.agents.bench import BENCH_LIMITS, INSTRUCTIONS, build_task, load_scenarios
from agent_runtime.agents.claude_cli import ClaudeCliBackend
from agent_runtime.agents.contract import BackendUnavailable, ToolCall
from agent_runtime.agents.events import validate_benchmark_result
from agent_runtime.agents.harness import SYSTEM_PROMPT, Harness, RunContext
from agent_runtime.agents.ollama import OllamaChatBackend, describe_model
from agent_runtime.agents.tools import TOOL_SPECS, ToolExecutor, ToolPolicy

MODELS = {"qwen": "qwen3:14b", "llama": "llama3.1:8b", "claude": "claude-sonnet-5-5"}
NUM_CTX = 16384


def make_backend(name: str, seed: int):
    if name == "claude":
        return ClaudeCliBackend(MODELS["claude"])
    sampling = {"temperature": 0.3, "seed": seed, "num_ctx": NUM_CTX}
    if name == "qwen":
        sampling["think"] = False
    return OllamaChatBackend(describe_model(MODELS[name], backend=name, sampling=sampling), keep_alive="10m")


def git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()


def dirty_paths() -> list[str]:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True).stdout
    return [ln[3:] for ln in out.splitlines() if ln.strip()]


def core_sha256() -> dict[str, str]:
    """Hash of every module that shapes agent behaviour or evaluation: equal hashes across backends prove
    the three runs executed the same harness, tools, prompts, limits and scenarios."""
    import hashlib
    files = sorted((REPO / "src" / "agent_runtime" / "agents").glob("*.py")) + \
        sorted(p for p in (REPO / "benchmark" / "scenarios").rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    return {p.relative_to(REPO).as_posix(): hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for p in files if p.name not in ("adapter.py",)}


START_HEAD = git_head()        # code the runs actually execute, captured before any run
START_DIRTY = dirty_paths()
START_CORE = core_sha256()


def ollama_version() -> str | None:
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=5) as r:
            return json.loads(r.read())["version"]
    except Exception:  # noqa: BLE001 - informative only
        return None


def claude_version() -> str | None:
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30,
                              shell=sys.platform == "win32").stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def blocked_result(backend: str, scenario: str, repeat: int, base: str, reason: str) -> dict:
    return {"schema": "benchmark_result/1.0", "run_id": f"{backend}-{scenario}-r{repeat}", "scenario": scenario,
            "repeat": repeat, "backend": backend, "provider": "unknown", "model": MODELS.get(backend, "?"),
            "model_digest": None, "quantization": None, "base_commit": base, "outcome": "INFRA_FAILURE",
            "functional_success": False, "infra_failure": True, "human_intervention": False,
            "error": f"BLOCKED: backend unavailable: {reason}", "attempts": 0, "duration_s": 0.0, "tool_calls": 0,
            "tool_errors": 0, "tests_before": {}, "tests_after": None, "hidden_tests": None,
            "tokens": {"prompt": None, "completion": None, "measured": False, "reason": "not run"},
            "contract_violations": [], "evidence": {}}


def run_backend(name: str, repeats: int, work: Path, out: Path, scenarios_filter: list[str] | None,
               store_path: Path | None = None) -> None:
    scenarios = [s for s in load_scenarios(REPO / "benchmark" / "scenarios")
                 if not scenarios_filter or s.name in scenarios_filter]
    runs_dir = out / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    store = None
    if store_path:
        from agent_runtime.event_store import EventStore
        store = EventStore(store_path)
    harness = Harness(store=store)
    results = []
    for s in scenarios:
        task = build_task(s, work / "fixtures" / name, python=sys.executable)
        for r in range(1, repeats + 1):
            seed = 1000 + r
            run_id = f"{name}-{s.name}-r{r}"
            print(f"[{datetime.now():%H:%M:%S}] {run_id} base={task.base_commit[:12]}", flush=True)
            try:
                backend = make_backend(name, seed)
            except BackendUnavailable as exc:
                res = blocked_result(name, s.name, r, task.base_commit, str(exc))
            else:
                ctx = RunContext(run_id=run_id, out_dir=runs_dir / f"{s.name}_r{r}",
                                 worktree=work / "wt" / name / f"{s.name}_r{r}", repeat=r,
                                 extra={"seed": seed if name != "claude" else None,
                                        "difficulty": s.difficulty})
                res = harness.run(task, backend, BENCH_LIMITS, ctx)
            validate_benchmark_result(res)
            results.append(res)
            print(f"    -> {res['outcome']} attempts={res['attempts']} calls={res['tool_calls']} "
                  f"t={res['duration_s']}s err={res.get('error')}", flush=True)
            (runs_dir / "results.jsonl").open("a", encoding="utf-8", newline="\n").write(json.dumps(res) + "\n")
    write_backend_outputs(name, out, results)


def write_backend_outputs(name: str, out: Path, results: list[dict]) -> None:
    runs_dir = out / "runs"
    env = {"agent_runtime_commit": START_HEAD, "agent_runtime_dirty": START_DIRTY, "core_sha256": START_CORE, "python": sys.version.split()[0], "platform": platform.platform(),
           "ollama_version": ollama_version() if name != "claude" else None,
           "claude_cli_version": claude_version() if name == "claude" else None,
           "generated_at": datetime.now(timezone.utc).isoformat()}
    manifests = []
    for res in results:
        mp = runs_dir / f"{res['scenario']}_r{res['repeat']}" / "run_manifest.json"
        manifests.append(json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {"run_id": res["run_id"]})
    first = next((m for m in manifests if "model" in m), {})
    (out / "run_manifest.json").write_text(json.dumps({
        "backend": name, "model": first.get("model", MODELS.get(name)), "model_digest": first.get("model_digest"),
        "quantization": first.get("quantization"), "context_window_model": first.get("context_window"),
        "sampling": first.get("sampling"), "limits": BENCH_LIMITS.__dict__, "instructions": INSTRUCTIONS,
        "system_prompt": SYSTEM_PROMPT, "environment": env, "runs": manifests}, indent=2, default=str) + "\n",
        encoding="utf-8")
    with (out / "tool_calls.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for res in results:
            p = runs_dir / f"{res['scenario']}_r{res['repeat']}" / "tool_calls.jsonl"
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    fh.write(line + "\n")
    (out / "test_results.json").write_text(json.dumps([
        {"run_id": r["run_id"], "scenario": r["scenario"], "repeat": r["repeat"], "before": r["tests_before"],
         "after": r["tests_after"], "hidden": r["hidden_tests"]} for r in results], indent=2) + "\n", encoding="utf-8", newline="\n")
    with (out / "changes.patch").open("w", encoding="utf-8", newline="\n") as fh:
        for res in results:
            p = runs_dir / f"{res['scenario']}_r{res['repeat']}" / "changes.patch"
            fh.write(f"### run {res['run_id']} outcome={res['outcome']}\n")
            fh.write(p.read_text(encoding="utf-8") if p.exists() else "(no patch)\n")
    (out / "metrics.json").write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    evaluation = [{"run_id": r["run_id"], "scenario": r["scenario"], "outcome": r["outcome"],
                   "visible_tests_pass": bool(r["tests_after"] and r["tests_after"].get("passed_all")),
                   "hidden_tests_pass": bool(r["hidden_tests"] and r["hidden_tests"].get("passed_all")),
                   "protected_files_untouched": not r["contract_violations"],
                   "diff_only_under_src": all(f.startswith("src/") for f in r.get("diff", {}).get("files", [])),
                   "evaluator": "harness deterministic: visible tests re-run by harness + hidden tests + "
                                "protected-path check; not the evaluated agent"} for r in results]
    (out / "evaluation.json").write_text(json.dumps(evaluation, indent=2) + "\n", encoding="utf-8", newline="\n")
    lines = [f"# Benchmark S00-11 — {name}", "", f"Modelo: `{first.get('model', MODELS.get(name))}` · digest "
             f"`{first.get('model_digest')}` · quant `{first.get('quantization')}` · commit agent-runtime "
             f"`{env['agent_runtime_commit'][:12]}`", "",
             "| run | outcome | tentativas | tool calls | erros de tool | testes depois | ocultos | tempo (s) | tokens (p/c) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        ta, hd, tk = r["tests_after"] or {}, r["hidden_tests"] or {}, r["tokens"]
        lines.append(f"| {r['run_id']} | {r['outcome']} | {r['attempts']} | {r['tool_calls']} | {r['tool_errors']} | "
                     f"{ta.get('passed')}/{(ta.get('passed') or 0) + (ta.get('failed') or 0)} | "
                     f"{'PASS' if hd.get('passed_all') else ('FAIL' if hd else '—')} | {r['duration_s']} | "
                     f"{tk.get('prompt')}/{tk.get('completion')} |")
    ok = sum(r["functional_success"] for r in results)
    lines += ["", f"Sucesso funcional: **{ok}/{len(results)}**. Intervenção humana: nenhuma.", "",
              "Erros: " + ("; ".join(f"{r['run_id']}: {r['error']}" for r in results if r.get("error")) or "nenhum")]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def smoke_all(work: Path, out_file: Path) -> None:
    """S00-05: one real call per backend through the shared tool contract."""
    ws = work / "smoke_ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "hello.txt").write_text("hello\n", encoding="utf-8", newline="\n")
    pol = ToolPolicy(enabled=frozenset({"list_files", "read_file"}))
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "backends": {}}
    prompt = "Use the list_files tool once to list the workspace, then answer with the file names only."
    for name in ("qwen", "llama"):
        t0 = time.monotonic()
        try:
            be = make_backend(name, 1)
            reply = be.chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                            [TOOL_SPECS["list_files"], TOOL_SPECS["read_file"]], timeout_s=300)
            ex = ToolExecutor(ws, pol)
            results = [ex.execute(ToolCall(c.name, c.arguments)) for c in reply.tool_calls]
            report["backends"][name] = {"status": "OK" if reply.tool_calls else "NO_TOOL_CALL",
                                        "model": be.info.model, "digest": be.info.model_digest,
                                        "quantization": be.info.quantization, "context_window": be.info.context_window,
                                        "tool_calls": [{"name": c.name, "arguments": dict(c.arguments)}
                                                       for c in reply.tool_calls],
                                        "tool_results_ok": [r.ok for r in results],
                                        "usage": reply.usage.__dict__, "wall_s": round(time.monotonic() - t0, 2)}
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            report["backends"][name] = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
    t0 = time.monotonic()
    try:
        be = ClaudeCliBackend(MODELS["claude"])
        log = work / "smoke_claude_tools.jsonl"
        log.unlink(missing_ok=True)
        pf = work / "smoke_policy.json"
        pf.write_text(json.dumps(pol.to_dict()), encoding="utf-8")
        r = be.run(system_prompt=SYSTEM_PROMPT, task_prompt=prompt, workspace=ws, tool_log=log, policy_file=pf,
                   limits=BENCH_LIMITS, timeout_s=300)
        calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        report["backends"]["claude"] = {"status": "OK" if calls else "NO_TOOL_CALL", "model": be.info.model,
                                        "cli_exit": r.exit_code, "subtype": r.raw.get("subtype"),
                                        "model_usage": list((r.raw.get("modelUsage") or {}).keys()),
                                        "tool_calls": [{"name": c["tool"], "ok": c["ok"]} for c in calls],
                                        "final_text": r.final_text[:300], "usage": r.usage.__dict__,
                                        "total_cost_usd_reported": r.raw.get("total_cost_usd"),
                                        "wall_s": round(time.monotonic() - t0, 2), "stderr_tail": r.stderr_tail[-300:]}
    except Exception as exc:  # noqa: BLE001
        report["backends"]["claude"] = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: v.get("status") for k, v in report["backends"].items()}))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=["qwen", "llama", "claude", "smoke-all"])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--scenarios", nargs="*")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--store", type=Path, help="EventStore (schema 1) where each run is recorded as a job")
    a = ap.parse_args()
    if a.backend == "smoke-all":
        smoke_all(a.work, a.out or REPO / "docs" / "evidence" / "s00-05-real-calls.json")
        return 0
    run_backend(a.backend, a.repeats, a.work, a.out or REPO / "benchmark" / a.backend, a.scenarios, a.store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
