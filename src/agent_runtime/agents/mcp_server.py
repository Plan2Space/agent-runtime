"""Minimal MCP stdio server exposing the harness tools (stdlib only).

Used so a delegating backend (Claude Code CLI) reaches exactly the same tools, policy and
tool log as the Ollama backends. Launched by the harness as:

    python -m agent_runtime.agents.mcp_server --workspace W --policy P.json --log L.jsonl [--context C.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract import ToolCall
from .tools import ToolExecutor, ToolPolicy

PROTOCOL_VERSION = "2025-06-18"


def handle(ex: ToolExecutor, req: dict) -> dict | None:
    method, rid = req.get("method"), req.get("id")
    if rid is None:  # notification
        return None
    if method == "initialize":
        result = {"protocolVersion": req.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "harness", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": [s.as_mcp() for s in ex.specs()]}
    elif method == "tools/call":
        params = req.get("params", {})
        r = ex.execute(ToolCall(str(params.get("name", "")), params.get("arguments") or {}))
        result = {"content": [{"type": "text", "text": r.for_model()}], "isError": not r.ok}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--context")
    ap.add_argument("--tool-timeout", type=float, default=120.0)
    ap.add_argument("--max-tool-calls", type=int, default=None,
                    help="per-attempt budget: calls beyond it are refused (error_kind=budget) and logged")
    a = ap.parse_args(argv)
    policy = ToolPolicy.from_dict(json.loads(Path(a.policy).read_text(encoding="utf-8")))
    ctx = json.loads(Path(a.context).read_text(encoding="utf-8")) if a.context else {}
    ex = ToolExecutor(Path(a.workspace), policy, tool_timeout_s=a.tool_timeout, log_path=Path(a.log), context=ctx,
                      max_calls=a.max_tool_calls)
    stdin = sys.stdin.buffer
    for raw in stdin:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(ex, req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
