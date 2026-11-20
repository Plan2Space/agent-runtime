"""Testes da própria verificação de instalação (scripts/verify_install.py).

Cada teste cria um ambiente real, com pip e as dependências fixadas. São lentos por natureza;
rodam no job install-check do CI e localmente com `pytest tests/install`.
"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_install as vi  # noqa: E402

VERSION = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


@pytest.fixture(scope="session")
def wheel(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(out), str(REPO)],
        check=True, capture_output=True, text=True,
    )
    built = sorted(out.glob("agent_runtime-*.whl"))
    assert len(built) == 1, built
    return built[0]


def test_the_installed_wheel_passes_and_is_not_masked_by_a_leaked_pythonpath(wheel, monkeypatch, capsys):
    # PYTHONPATH apontando para o checkout NÃO pode mascarar o pacote instalado.
    monkeypatch.setenv("PYTHONPATH", str(REPO / "src"))
    assert vi.main(["--wheel", str(wheel), "--expect-version", VERSION]) == 0
    assert "[ok]" in capsys.readouterr().out


def test_a_version_mismatch_is_refused(wheel, capsys):
    assert vi.main(["--wheel", str(wheel), "--expect-version", "9.9.9"]) == 1
    assert "incompatibilidade de versão" in capsys.readouterr().err


def test_a_corrupt_wheel_fails_the_installation(tmp_path, capsys):
    broken = tmp_path / f"agent_runtime-{VERSION}-py3-none-any.whl"
    broken.write_bytes(b"isto nao e um zip")
    assert vi.main(["--wheel", str(broken), "--expect-version", VERSION]) == 1
    assert "comando falhou" in capsys.readouterr().err


def test_a_missing_wheel_is_refused_before_any_install(tmp_path, capsys):
    assert vi.main(["--wheel", str(tmp_path / "nao-existe.whl"), "--expect-version", VERSION]) == 1
    assert "wheel não encontrado" in capsys.readouterr().err


def test_rollback_with_the_same_version_is_refused_not_reported_as_passed(wheel, capsys):
    # Rollback exige duas versões diferentes e publicadas. Mesma versão nunca vira prova.
    code = vi.main([
        "--wheel", str(wheel), "--expect-version", VERSION,
        "--rollback-wheel", str(wheel), "--rollback-version", VERSION,
    ])
    assert code == 1
    assert "duas versões" in capsys.readouterr().err


def test_the_contract_suite_is_the_only_suite_the_rollback_runs():
    # Garantia estática: a prova de rollback só enxerga a suíte de contrato, nunca tests/ específicos da versão.
    assert vi.CONTRACT_SUITE == REPO / "tests" / "contract"
    assert all(p.name.startswith("test_") for p in vi.CONTRACT_SUITE.glob("*.py"))
    assert not any(p.parent != vi.CONTRACT_SUITE for p in vi.CONTRACT_SUITE.rglob("test_*.py"))
