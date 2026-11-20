"""Verifica a instalação reproduzível do agent-runtime como pacote instalado.

Garantias que esta verificação prova (cada uma falha alto):
  - o ambiente de verificação é novo e as dependências são as fixadas em scripts/verify-requirements.txt;
  - o wheel é instalado sem dependências extras e sem o código-fonte;
  - as chamadas ao Python do ambiente usam modo isolado (`-I`): ignoram PYTHONPATH herdado,
    site do usuário e o diretório atual;
  - o módulo importado vem de dentro do ambiente instalado, nunca do checkout;
  - a versão instalada é exatamente a esperada, e a major é a suportada pelo contrato;
  - a suíte de contrato (tests/contract) roda contra o pacote instalado, fora do repositório.

Rollback (--rollback-wheel) é uma opção separada. Só é uma prova quando existirem duas versões
publicadas de verdade. Sem elas, a execução é recusada, e não há resultado "passou" fabricado.

Uso:
  python -m venv venv && venv/bin/python -m pip install -r scripts/verify-requirements.txt
  python scripts/verify_install.py --wheel dist/agent_runtime-0.1.0-py3-none-any.whl --expect-version 0.1.0
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO / "scripts" / "verify-requirements.txt"
CONTRACT_SUITE = REPO / "tests" / "contract"
SUPPORTED_MAJOR = 1

SMOKE = (
    "import sys, agent_runtime as r;"
    "from agent_runtime import contracts, event_store, outbox_publisher, durable_file;"
    "print('VERSION=' + r.__version__);"
    "print('FILE=' + r.__file__);"
    "print('PREFIX=' + sys.prefix);"
    "print('MAJOR=' + str(contracts.SUPPORTED_MAJOR));"
    "assert event_store.SCHEMA_VERSION == 1;"
    "assert callable(outbox_publisher.publish_one) and callable(durable_file.write_durable)"
)


class VerificationError(RuntimeError):
    """Uma etapa da verificação falhou. A mensagem diz qual e por quê."""


@dataclass(frozen=True)
class Installed:
    version: str
    file: Path
    prefix: Path
    major: int


def clean_env() -> dict[str, str]:
    """Ambiente sem nada que faça o Python achar código fora do ambiente instalado."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=clean_env(), capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
        raise VerificationError(f"comando falhou ({proc.returncode}): {' '.join(cmd[-3:])}\n" + "\n".join(tail))
    return proc.stdout


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def make_venv(venv: Path) -> Path:
    run([sys.executable, "-I", "-m", "venv", str(venv)])
    py = venv_python(venv)
    run([str(py), "-I", "-m", "pip", "install", "--quiet", "--no-cache-dir", "-r", str(REQUIREMENTS)])
    return py


def install_wheel(py: Path, wheel: Path) -> None:
    if not wheel.is_file():
        raise VerificationError(f"wheel não encontrado: {wheel}")
    run([str(py), "-I", "-m", "pip", "install", "--quiet", "--no-deps", "--force-reinstall", str(wheel)])


def probe(py: Path, venv: Path) -> Installed:
    out = run([str(py), "-I", "-c", SMOKE], cwd=venv.parent)
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    installed = Installed(
        version=values["VERSION"],
        file=Path(values["FILE"]).resolve(),
        prefix=Path(values["PREFIX"]).resolve(),
        major=int(values["MAJOR"]),
    )
    return installed


def check_installed(inst: Installed, venv: Path, expect: str) -> None:
    if inst.version != expect:
        raise VerificationError(f"incompatibilidade de versão: instalado {inst.version!r}, esperado {expect!r}")
    # A major verificada é a do CONTRATO exposto pelo pacote (SemVer de 0.x não é a major do contrato).
    if inst.major != SUPPORTED_MAJOR:
        raise VerificationError(f"contrato incompatível: pacote expõe major {inst.major}, esta verificação suporta {SUPPORTED_MAJOR}")
    venv_resolved = venv.resolve()
    if venv_resolved not in inst.file.parents:
        raise VerificationError(f"o módulo importado não está no ambiente instalado: {inst.file}")
    if REPO in inst.file.parents or inst.file.is_relative_to(REPO):
        raise VerificationError(f"o checkout mascarou o pacote instalado: {inst.file}")


def contract_suite(py: Path, work: Path) -> None:
    """A suíte de contrato roda FORA do repositório: copiada para uma pasta temporária."""
    target = work / "contract"
    shutil.copytree(CONTRACT_SUITE, target)
    run([str(py), "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(target)], cwd=work)


def verify(wheel: Path, expect: str, work: Path) -> Installed:
    py = make_venv(work / "venv")
    install_wheel(py, wheel)
    inst = probe(py, work / "venv")
    check_installed(inst, work / "venv", expect)
    contract_suite(py, work / "round")
    return inst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wheel", type=Path, required=True)
    ap.add_argument("--expect-version", required=True)
    ap.add_argument("--rollback-wheel", type=Path, help="prova de rollback: exige duas versões publicadas")
    ap.add_argument("--rollback-version")
    args = ap.parse_args(argv)
    if bool(args.rollback_wheel) != bool(args.rollback_version):
        ap.error("--rollback-wheel e --rollback-version andam juntos")

    if args.rollback_wheel and (args.rollback_version == args.expect_version or args.rollback_wheel.resolve() == args.wheel.resolve()):
        print("[falha] a prova de rollback exige duas versões diferentes e publicadas", file=sys.stderr)
        return 1

    work = Path(tempfile.mkdtemp(prefix="agent-runtime-verify-"))
    try:
        inst = verify(args.wheel.resolve(), args.expect_version, work)
        print(f"[ok] {inst.version}: instalado de {inst.file}, contrato verificado")
        if args.rollback_wheel:
            py = venv_python(work / "venv")
            install_wheel(py, args.rollback_wheel.resolve())
            old = probe(py, work / "venv")
            check_installed(old, work / "venv", args.rollback_version)
            contract_suite(py, work / "rollback")
            print(f"[ok] rollback para {old.version}: contrato verificado")
            install_wheel(py, args.wheel.resolve())
            back = probe(py, work / "venv")
            check_installed(back, work / "venv", args.expect_version)
            contract_suite(py, work / "forward")
            print(f"[ok] retorno para {back.version}: contrato verificado")
        return 0
    except VerificationError as exc:
        print(f"[falha] {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
