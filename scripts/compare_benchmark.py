"""Build benchmark/comparison.json + comparison.md from benchmark/{claude,qwen,llama}/metrics.json.

Recommendation per backend is a deterministic rule over the measured success rates (no subjective score):
    autonomo_controlado        : SUCCESS in >= 8/9 runs, every scenario >= 2/3, zero contract violations
    assistente_supervisionado  : SUCCESS in >= 2/3 of s1 and s2 runs, zero contract violations
    so_tarefas_simples         : SUCCESS in >= 2/3 of s1 runs
    inadequado                 : otherwise
    BLOCKED                    : backend unavailable (no data), never simulated
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKENDS = ("claude", "qwen", "llama")


def load(name: str) -> list[dict] | None:
    p = REPO / "benchmark" / name / "metrics.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 2) if xs else None


def summarize(name: str, runs: list[dict]) -> dict:
    by_s: dict[str, list[dict]] = {}
    for r in runs:
        by_s.setdefault(r["scenario"], []).append(r)
    per = {s: {"success": sum(r["functional_success"] for r in rs), "runs": len(rs),
               "outcomes": [r["outcome"] for r in rs],
               "median_duration_s": med(r["duration_s"] for r in rs),
               "median_attempts": med(r["attempts"] for r in rs),
               "median_tool_calls": med(r["tool_calls"] for r in rs)} for s, rs in sorted(by_s.items())}
    infra = sum(r["infra_failure"] for r in runs)
    violations = sum(bool(r["contract_violations"]) for r in runs)
    tok_p = [r["tokens"].get("prompt") for r in runs]
    tok_c = [r["tokens"].get("completion") for r in runs]
    gpu = [((r.get("resources") or {}).get("gpu_mem_used_mib") or {}).get("max") for r in runs]
    total_ok = sum(r["functional_success"] for r in runs)
    s1, s2 = per.get("s1_paginate", {}), per.get("s2_duration", {})

    def rate(d):
        return d["success"] / d["runs"] if d and d.get("runs") else 0.0
    if infra == len(runs):
        rec = "BLOCKED"
    elif total_ok >= 8 and all(rate(d) >= 2 / 3 for d in per.values()) and not violations:
        rec = "autonomo_controlado"
    elif rate(s1) >= 2 / 3 and rate(s2) >= 2 / 3 and not violations:
        rec = "assistente_supervisionado"
    elif rate(s1) >= 2 / 3:
        rec = "so_tarefas_simples"
    else:
        rec = "inadequado"
    first = runs[0]
    return {"backend": name, "model": first["model"], "model_digest": first.get("model_digest"),
            "quantization": first.get("quantization"), "provider": first["provider"],
            "success": total_ok, "runs": len(runs), "infra_failures": infra, "contract_violations": violations,
            "visible_tests_pass_runs": sum(bool(r["tests_after"] and r["tests_after"].get("passed_all")) for r in runs),
            "hidden_tests_pass_runs": sum(bool(r["hidden_tests"] and r["hidden_tests"].get("passed_all")) for r in runs),
            "median_attempts": med(r["attempts"] for r in runs),
            "median_duration_s": med(r["duration_s"] for r in runs),
            "median_tool_calls": med(r["tool_calls"] for r in runs),
            "tool_error_rate": round(sum(r["tool_errors"] for r in runs) / max(1, sum(r["tool_calls"] for r in runs)), 3),
            "median_prompt_tokens": med(tok_p), "median_completion_tokens": med(tok_c),
            "max_gpu_mem_used_mib_system": max([g for g in gpu if g is not None], default=None),
            "human_intervention_runs": sum(r["human_intervention"] for r in runs),
            "per_scenario": per, "recommendation": rec}


def main() -> int:
    out = {"generated_from": "benchmark/<backend>/metrics.json", "rule": __doc__.strip().splitlines()[2:7],
           "backends": {}}
    for b in BACKENDS:
        runs = load(b)
        out["backends"][b] = summarize(b, runs) if runs else {"backend": b, "recommendation": "BLOCKED",
                                                               "reason": "no benchmark data (not run or unavailable)"}
    bases = {}
    for b in BACKENDS:
        for r in load(b) or []:
            bases.setdefault(r["scenario"], set()).add(r["base_commit"])
    out["same_base_commit_per_scenario"] = {s: len(v) == 1 for s, v in bases.items()}
    out["base_commits"] = {s: sorted(v) for s, v in bases.items()}
    (REPO / "benchmark" / "comparison.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    L = ["# Benchmark S00-11 — comparação Claude × Qwen × Llama", "",
         "Mesmo commit base por cenário: " + ", ".join(f"`{s}` {'sim' if ok else 'NÃO'}"
                                                      for s, ok in out["same_base_commit_per_scenario"].items()), "",
         "| Backend | Modelo | Sucesso | Testes visíveis | Ocultos (qualidade) | Tentativas (mediana) | Tempo s (mediana) | "
         "Tool calls (mediana) | Taxa erro tool | Tokens p/c (mediana) | GPU máx MiB (sistema) | Intervenção humana | Recomendação |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for b, d in out["backends"].items():
        if "runs" not in d:
            L.append(f"| {b} | — | BLOCKED | — | — | — | — | — | — | — | — | — | BLOCKED: {d['reason']} |")
            continue
        L.append(f"| {b} | `{d['model']}` {d['quantization'] or ''} | {d['success']}/{d['runs']} | "
                 f"{d['visible_tests_pass_runs']}/{d['runs']} | {d['hidden_tests_pass_runs']}/{d['runs']} | "
                 f"{d['median_attempts']} | {d['median_duration_s']} | {d['median_tool_calls']} | {d['tool_error_rate']} | "
                 f"{d['median_prompt_tokens']}/{d['median_completion_tokens']} | {d['max_gpu_mem_used_mib_system']} | "
                 f"{d['human_intervention_runs']} | **{d['recommendation']}** |")
    L += ["", "## Por cenário (sucesso / execuções, desfechos)", "",
          "| Backend | " + " | ".join(sorted({s for d in out["backends"].values() for s in d.get("per_scenario", {})})) + " |",
          "|---|" + "---|" * len({s for d in out["backends"].values() for s in d.get("per_scenario", {})})]
    scen = sorted({s for d in out["backends"].values() for s in d.get("per_scenario", {})})
    for b, d in out["backends"].items():
        cells = []
        for s in scen:
            p = d.get("per_scenario", {}).get(s)
            cells.append(f"{p['success']}/{p['runs']} ({', '.join(p['outcomes'])})" if p else "—")
        L.append(f"| {b} | " + " | ".join(cells) + " |")
    L += ["", "## Regra de recomendação (determinística)", ""] + [f"- {x.strip()}" for x in out["rule"]]
    (REPO / "benchmark" / "comparison.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(json.dumps({b: d.get("recommendation") for b, d in out["backends"].items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
