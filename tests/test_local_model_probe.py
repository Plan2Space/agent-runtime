"""Partes puras da sonda de modelos locais (scripts/local_model_probe.py). Sem servidor Ollama."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "local_model_probe", Path(__file__).resolve().parents[1] / "scripts" / "local_model_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

GOOD = ("```python\nimport re\ndef is_palindrome(s: str) -> bool:\n"
        "    t = re.sub(r'[^a-z0-9]', '', s.lower())\n    return t == t[::-1]\n```")


def test_tokens_per_second_from_ollama_nanoseconds():
    assert probe.tokens_per_second(100, 2_000_000_000) == 50.0


@pytest.mark.parametrize("count,dur", [(None, 1), (0, 1), (10, 0), (10, None)])
def test_tokens_per_second_is_none_when_data_missing(count, dur):
    assert probe.tokens_per_second(count, dur) is None


@pytest.mark.parametrize("text", ["391", " 391.\n", "<think>17*23...</think>391"])
def test_arithmetic_accepts_exact_answer(text):
    assert probe.check_arithmetic(text)


@pytest.mark.parametrize("text", ["The answer is 391", "390", "391 or 392", ""])
def test_arithmetic_rejects_anything_but_exact_integer(text):
    assert not probe.check_arithmetic(text)


def test_extract_requires_exactly_one_python_block():
    assert probe.extract_python_block(GOOD).startswith("import re")
    assert probe.extract_python_block("no code here") is None
    assert probe.extract_python_block(GOOD + "\n" + GOOD) is None


def test_correct_code_passes_the_executed_asserts():
    assert probe.run_code_check(probe.extract_python_block(GOOD))["passed"]


def test_wrong_code_fails_the_executed_asserts():
    wrong = "def is_palindrome(s):\n    return s == s[::-1]\n"  # ignora caixa/pontuação
    result = probe.run_code_check(wrong)
    assert not result["passed"] and result["returncode"] != 0


def test_missing_block_is_reported_as_failure():
    assert probe.run_code_check(None) == {"passed": False,
                                          "reason": "resposta sem exatamente um bloco ```python"}


def test_hanging_code_times_out_as_failure():
    result = probe.run_code_check("while True:\n    pass\n", timeout_s=1)
    assert not result["passed"] and "timeout" in result["reason"]


def test_gen_metrics_converts_ollama_fields():
    m = probe.gen_metrics({"total_duration": 3_000_000_000, "load_duration": 1_500_000,
                           "prompt_eval_count": 20, "prompt_eval_duration": 100_000_000,
                           "eval_count": 60, "eval_duration": 1_000_000_000, "done_reason": "stop"}, 3.2)
    assert m == {"wall_ms": 3200.0, "total_ms": 3000.0, "load_ms": 1.5, "prompt_tokens": 20,
                 "prompt_tps": 200.0, "eval_tokens": 60, "eval_tps": 60.0, "done_reason": "stop"}
