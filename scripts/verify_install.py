"""Verifica a instalação reproduzível do agent-runtime como pacote.

Fluxo (cada passo falha alto):
  1. cria um ambiente virtual novo;
  2. instala o wheel informado, sem dependências extras e sem o código-fonte no caminho;
  3. confirma a versão instalada e importa a superfície pública (CONTRACT.md);
  4. roda a suíte de contrato copiada para fora do repositório, contra o pacote instalado;
  5. com --rollback-wheel: reinstala a versão anterior no mesmo ambiente, roda a suíte de novo,
     volta para a versão atual e roda a suíte uma última vez. O rollback só conta se as três rodadas passam.

Uso:
  python scripts/verify_install.py --wheel dist/agent_runtime-0.1.0-py3-none-any.whl --expect-version 0.1.0
  python scripts/verify_install.py --wheel NEW.whl --expect-version 0.1.1 --rollback-wheel OLD.whl --rollback-version 0.1.0
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SMOKE = (
    "import agent_runtime as r;"
    "from agent_runtime import contracts, event_store, outbox_publisher, durable_file;"
    "print(r.__version__);"
    "assert contracts.SUPPORTED_MAJOR == 1;"
    "assert event_store.SCHEMA_VERSION == 1;"
    "assert callable(outbox_publisher.publish_one) and callable(durable_file.write_durable)"
)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def install(py: Path, wheel: Path) -> None:
    run([str(py), "-m", "pip", "install", "--quiet", "--no-deps", "--force-reinstall", str(wheel)])


def smoke(py: Path, expect: str) -> None:
    got = run([str(py), "-c", SMOKE]).strip().splitlines()[-1]
    if got != expect:
        raise SystemExit(f"FAILED: version installed is {got!r}, expected {expect!r}")


def contract_suite(py: Path, workdir: Path) -> None:
    # The suite runs from a copy outside the repository, so `src/` cannot shadow the installed package.
    tests = workdir / "tests"
    shutil.copytree(REPO / "tests", tests, dirs_exist_ok=True)
    run([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests)], cwd=workdir)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheel", type=Path, required=True)
    ap.add_argument("--expect-version", required=True)
    ap.add_argument("--rollback-wheel", type=Path)
    ap.add_argument("--rollback-version")
    args = ap.parse_args()
    if args.rollback_wheel and not args.rollback_version:
        ap.error("--rollback-version is required with --rollback-wheel")

    work = Path(tempfile.mkdtemp(prefix="agent-runtime-verify-"))
    try:
        venv = work / "venv"
        run([sys.executable, "-m", "venv", str(venv)])
        py = venv_python(venv)
        run([str(py), "-m", "pip", "install", "--quiet", "pytest>=8"])

        install(py, args.wheel.resolve())
        smoke(py, args.expect_version)
        contract_suite(py, work / "round1")
        print(f"[ok] {args.expect_version}: install, import and contract suite")

        if args.rollback_wheel:
            install(py, args.rollback_wheel.resolve())
            smoke(py, args.rollback_version)
            contract_suite(py, work / "rollback")
            print(f"[ok] rollback to {args.rollback_version}: contract suite")

            install(py, args.wheel.resolve())
            smoke(py, args.expect_version)
            contract_suite(py, work / "forward")
            print(f"[ok] forward again to {args.expect_version}: contract suite")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
