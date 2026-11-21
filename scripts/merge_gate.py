"""Run the S00-14 phase-0 merge gate on one PR.

    python scripts/merge_gate.py --repo Plan2Space/agent-runtime --pr 63 --store <events.sqlite> --approver MEDIUM=<reviewer-login>
    python scripts/merge_gate.py ... --execute        # real merge (LOW; MEDIUM with APPROVED review); default dry-run

Prints one JSON line with the decision and appends it to --log (JSONL) when given. Never uses --admin.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_runtime.event_store import EventStore  # noqa: E402
from agent_runtime.merge_gate import GhCli, result_dict, run_gate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--store", required=True, type=Path)
    ap.add_argument("--approver", action="append", default=[], metavar="LEVEL=LOGIN",
                    help="login whose APPROVED review lets the runtime merge that level (repeatable); HIGH is ignored")
    ap.add_argument("--base", default="main")
    ap.add_argument("--execute", action="store_true", help="really merge LOW / approved MEDIUM. Default: dry-run")
    ap.add_argument("--log", type=Path)
    a = ap.parse_args()
    approvers: dict[str, list[str]] = {}
    for item in a.approver:
        level, sep, login = item.partition("=")
        if not sep or not login.strip():
            ap.error(f"--approver must be LEVEL=LOGIN, got {item!r}")
        approvers.setdefault(level.strip().upper(), []).append(login.strip())
    res = run_gate(GhCli(), EventStore(a.store), a.repo, a.pr, approvers=approvers, execute=a.execute, base=a.base)
    line = {"at": datetime.now(timezone.utc).isoformat(), "execute": a.execute, **result_dict(res)}
    out = json.dumps(line, ensure_ascii=False)
    print(out)
    if a.log:
        a.log.parent.mkdir(parents=True, exist_ok=True)
        with a.log.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
