"""Sonda real de modelos locais servidos pelo Ollama (S00-04, agent-runtime#53).

Mede, para cada modelo que o servidor REALMENTE lista (nada é presumido instalado):
  - carga a frio (load_duration) e VRAM observada pelo nvidia-smi;
  - divisão GPU/CPU reportada por /api/ps (size vs size_vram) e contexto efetivo;
  - throughput de prompt e de geração (tokens/s) em execuções quentes, com temperatura 0 e seed fixa;
  - duas tarefas verificáveis: aritmética com resposta exata e uma função Python executada com asserts
    em subprocesso isolado (`-I`, timeout). Saída que não passa é FAIL, nunca "quase";
  - concorrência: o que acontece quando um segundo modelo é pedido com o primeiro carregado.

Só biblioteca padrão. Sem servidor ou sem modelo, a sonda falha alto: não há resultado fabricado.

Uso:
  python scripts/local_model_probe.py --host http://127.0.0.1:11435 --out evidence.json
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ARITH_PROMPT = "Compute 17 * 23. Reply with only the integer, no words."
ARITH_EXPECTED = "391"
CODE_PROMPT = (
    "Write a Python function `is_palindrome(s: str) -> bool` that returns True when s reads the same "
    "forwards and backwards ignoring case and any non-alphanumeric characters. "
    "Reply with only one ```python code block containing the function, nothing else."
)
CODE_TESTS = """
assert is_palindrome("A man, a plan, a canal: Panama") is True
assert is_palindrome("race a car") is False
assert is_palindrome("") is True
assert is_palindrome("No 'x' in Nixon") is True
assert is_palindrome("ab") is False
print("CODE_TESTS_PASSED")
"""
THROUGHPUT_PROMPT = "Explain in about 150 words what a write-ahead log is and why databases use one."
OPTIONS = {"temperature": 0, "seed": 42, "num_ctx": 4096}


class ProbeError(RuntimeError):
    pass


# ---------- funções puras (testadas sem servidor) ----------

def tokens_per_second(count: int | None, duration_ns: int | None) -> float | None:
    if not count or not duration_ns or duration_ns <= 0:
        return None
    return round(count / (duration_ns / 1e9), 2)


def ns_to_ms(value: int | None) -> float | None:
    return None if value is None else round(value / 1e6, 1)


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def check_arithmetic(text: str) -> bool:
    """Exige exatamente o inteiro esperado como resposta (pontuação final tolerada)."""
    return strip_think(text).strip().rstrip(".") == ARITH_EXPECTED


def extract_python_block(text: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", strip_think(text), flags=re.S)
    return blocks[0] if len(blocks) == 1 else None


def run_code_check(code: str | None, timeout_s: float = 10.0) -> dict:
    if code is None:
        return {"passed": False, "reason": "resposta sem exatamente um bloco ```python"}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "candidate.py"
        path.write_text(code + "\n" + CODE_TESTS, encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, "-I", str(path)], cwd=tmp, capture_output=True,
                                  text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return {"passed": False, "reason": f"timeout {timeout_s}s"}
    passed = proc.returncode == 0 and "CODE_TESTS_PASSED" in proc.stdout
    return {"passed": passed, "returncode": proc.returncode,
            "stderr_tail": proc.stderr.strip().splitlines()[-1:] if not passed else []}


def gen_metrics(resp: dict, wall_s: float) -> dict:
    return {
        "wall_ms": round(wall_s * 1000, 1),
        "total_ms": ns_to_ms(resp.get("total_duration")),
        "load_ms": ns_to_ms(resp.get("load_duration")),
        "prompt_tokens": resp.get("prompt_eval_count"),
        "prompt_tps": tokens_per_second(resp.get("prompt_eval_count"), resp.get("prompt_eval_duration")),
        "eval_tokens": resp.get("eval_count"),
        "eval_tps": tokens_per_second(resp.get("eval_count"), resp.get("eval_duration")),
        "done_reason": resp.get("done_reason"),
    }


# ---------- I/O ----------

def http(host: str, path: str, payload: dict | None = None, timeout: float = 600) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(host + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.URLError as exc:
        raise ProbeError(f"{path}: {exc}") from exc


def gpu_snapshot() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    name, drv, total, used, free, util = [x.strip() for x in out.strip().splitlines()[0].split(",")]
    return {"name": name, "driver": drv, "total_mib": int(total), "used_mib": int(used),
            "free_mib": int(free), "util_pct": int(util)}


def unload_all(host: str) -> None:
    for m in http(host, "/api/ps").get("models", []):
        http(host, "/api/generate", {"model": m["name"], "keep_alive": 0})
    for _ in range(60):
        if not http(host, "/api/ps").get("models"):
            return
        time.sleep(0.5)
    raise ProbeError("modelos não descarregaram em 30s")


def ps_entry(host: str, model: str) -> dict | None:
    for m in http(host, "/api/ps").get("models", []):
        if m["name"] == model:
            return {"size_mib": m["size"] // 2**20, "size_vram_mib": m["size_vram"] // 2**20,
                    "gpu_fraction": round(m["size_vram"] / m["size"], 3) if m["size"] else None,
                    "context_length": m.get("context_length")}
    return None


def generate(host: str, model: str, prompt: str, think: bool | None) -> tuple[dict, dict]:
    payload = {"model": model, "prompt": prompt, "stream": False, "options": OPTIONS, "keep_alive": "10m"}
    if think is not None:
        payload["think"] = think
    t0 = time.perf_counter()
    resp = http(host, "/api/generate", payload)
    return resp, gen_metrics(resp, time.perf_counter() - t0)


def supports_thinking(show: dict) -> bool:
    return "thinking" in (show.get("capabilities") or [])


def probe_chat_model(host: str, model: str, warm_runs: int) -> dict:
    show = http(host, "/api/show", {"model": model})
    think = False if supports_thinking(show) else None
    unload_all(host)
    gpu_before = gpu_snapshot()
    _, cold = generate(host, model, ARITH_PROMPT, think)
    gpu_loaded = gpu_snapshot()
    placement = ps_entry(host, model)

    arith_resp, arith_m = generate(host, model, ARITH_PROMPT, think)
    code_resp, code_m = generate(host, model, CODE_PROMPT, think)
    code_check = run_code_check(extract_python_block(code_resp.get("response", "")))

    warm = [generate(host, model, THROUGHPUT_PROMPT, think)[1] for _ in range(warm_runs)]
    eval_tps = [w["eval_tps"] for w in warm if w["eval_tps"]]
    prompt_tps = [w["prompt_tps"] for w in warm if w["prompt_tps"]]
    return {
        "model": model,
        "details": show.get("details"),
        "capabilities": show.get("capabilities"),
        "think_param": think,
        "options": OPTIONS,
        "cold_start": cold,
        "placement": placement,
        "gpu_before": gpu_before,
        "gpu_loaded": gpu_loaded,
        "vram_delta_mib": (gpu_loaded["used_mib"] - gpu_before["used_mib"]) if gpu_before and gpu_loaded else None,
        "tasks": {
            "arithmetic": {"passed": check_arithmetic(arith_resp.get("response", "")),
                           "answer": strip_think(arith_resp.get("response", ""))[:80], "metrics": arith_m},
            "code_palindrome": {**code_check, "metrics": code_m},
        },
        "warm_runs": warm,
        "warm_eval_tps_median": statistics.median(eval_tps) if eval_tps else None,
        "warm_prompt_tps_median": statistics.median(prompt_tps) if prompt_tps else None,
    }


def probe_embed_model(host: str, model: str) -> dict:
    unload_all(host)
    t0 = time.perf_counter()
    r = http(host, "/api/embed", {"model": model, "input": ["write-ahead log", "planta baixa"]})
    cold_ms = round((time.perf_counter() - t0) * 1000, 1)
    t0 = time.perf_counter()
    r2 = http(host, "/api/embed", {"model": model, "input": ["write-ahead log", "planta baixa"]})
    warm_ms = round((time.perf_counter() - t0) * 1000, 1)
    emb = r.get("embeddings") or []
    return {"model": model, "cold_ms": cold_ms, "warm_ms": warm_ms, "load_ms": ns_to_ms(r.get("load_duration")),
            "vectors": len(emb), "dims": len(emb[0]) if emb else 0,
            "deterministic": emb == r2.get("embeddings"), "placement": ps_entry(host, model)}


def probe_concurrency(host: str, a: str, b: str) -> dict:
    unload_all(host)
    generate(host, a, ARITH_PROMPT, None if not supports_thinking(http(host, "/api/show", {"model": a})) else False)
    after_a = [m["name"] for m in http(host, "/api/ps").get("models", [])]
    think_b = False if supports_thinking(http(host, "/api/show", {"model": b})) else None
    _, metrics_b = generate(host, b, ARITH_PROMPT, think_b)
    loaded = {m["name"]: {"size_mib": m["size"] // 2**20, "size_vram_mib": m["size_vram"] // 2**20}
              for m in http(host, "/api/ps").get("models", [])}
    return {"first": a, "second": b, "loaded_after_first": after_a, "loaded_after_second": loaded,
            "first_evicted": a not in loaded, "second_request": metrics_b, "gpu": gpu_snapshot()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--warm-runs", type=int, default=3)
    args = ap.parse_args(argv)

    version = http(args.host, "/api/version")
    tags = http(args.host, "/api/tags").get("models", [])
    if not tags:
        raise ProbeError(f"{args.host} não lista nenhum modelo: nada para sondar")
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": args.host,
                    "ollama_version": version.get("version"), "python": platform.python_version(),
                    "listed_models": [{"name": t["name"], "digest": t["digest"], "size": t["size"],
                                       "details": t.get("details")} for t in tags],
                    "chat": [], "embed": [], "errors": []}
    chat_models = []
    for t in tags:
        name = t["name"]
        try:
            caps = http(args.host, "/api/show", {"model": name}).get("capabilities") or []
            if "embedding" in caps and "completion" not in caps:
                report["embed"].append(probe_embed_model(args.host, name))
            else:
                report["chat"].append(probe_chat_model(args.host, name, args.warm_runs))
                chat_models.append(name)
        except ProbeError as exc:
            report["errors"].append({"model": name, "error": str(exc)})
    if len(chat_models) >= 2:
        report["concurrency"] = probe_concurrency(args.host, chat_models[0], chat_models[1])
    unload_all(args.host)
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    failed = [c["model"] for c in report["chat"]
              if not (c["tasks"]["arithmetic"]["passed"] and c["tasks"]["code_palindrome"]["passed"])]
    print(json.dumps({"out": str(args.out), "chat": chat_models, "task_failures": failed,
                      "errors": report["errors"]}, ensure_ascii=False))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
