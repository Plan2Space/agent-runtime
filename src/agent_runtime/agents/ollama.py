"""Ollama chat adapter (Qwen, Llama) with native tool calling, stdlib only."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from .contract import BackendError, BackendInfo, BackendUnavailable, ChatReply, ToolCall, ToolSpec, Usage

DEFAULT_URL = "http://127.0.0.1:11434"


def _http(url: str, payload: Mapping[str, Any] | None, timeout_s: float) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        if exc.code == 404:
            raise BackendUnavailable(f"ollama 404: {body}") from exc
        raise BackendError(f"ollama HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise BackendUnavailable(f"ollama unreachable at {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BackendError(f"ollama returned non-JSON: {exc}") from exc


def describe_model(model: str, *, backend: str, url: str = DEFAULT_URL, sampling: Mapping[str, Any] | None = None,
                   timeout_s: float = 30) -> BackendInfo:
    """Build BackendInfo from what the server really reports (digest, quantization, context)."""
    tags = _http(f"{url}/api/tags", None, timeout_s)
    entry = next((m for m in tags.get("models", []) if m.get("name") == model), None)
    if entry is None:
        raise BackendUnavailable(f"model {model!r} is not listed by {url}")
    show = _http(f"{url}/api/show", {"model": model}, timeout_s)
    ctx = next((v for k, v in show.get("model_info", {}).items() if k.endswith(".context_length")), None)
    if "tools" not in show.get("capabilities", []):
        raise BackendUnavailable(f"model {model!r} does not declare tool support")
    return BackendInfo(backend=backend, provider="ollama", model=model, model_digest=entry.get("digest"),
                       quantization=entry.get("details", {}).get("quantization_level"),
                       context_window=ctx, sampling=dict(sampling or {}))


class OllamaChatBackend:
    def __init__(self, info: BackendInfo, *, url: str = DEFAULT_URL, keep_alive: str = "10m"):
        self.info = info
        self.url = url
        self.keep_alive = keep_alive

    def chat(self, messages: list[dict], tools: Sequence[ToolSpec], *, timeout_s: float) -> ChatReply:
        sampling = dict(self.info.sampling)
        think = sampling.pop("think", None)
        payload: dict[str, Any] = {"model": self.info.model, "messages": messages, "stream": False,
                                   "tools": [t.as_openai() for t in tools], "options": sampling,
                                   "keep_alive": self.keep_alive}
        if think is not None:
            payload["think"] = think
        data = _http(f"{self.url}/api/chat", payload, timeout_s)
        return parse_chat_response(data)


def parse_chat_response(data: Mapping[str, Any]) -> ChatReply:
    msg = data.get("message")
    if not isinstance(msg, Mapping):
        raise BackendError(f"ollama response has no message: {str(data)[:200]}")
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function", {}) if isinstance(tc, Mapping) else {}
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"__unparseable__": args}
        calls.append(ToolCall(str(fn.get("name", "")), args if isinstance(args, Mapping) else {"__invalid__": args},
                              call_id=str(tc.get("id", i))))
    return ChatReply(content=str(msg.get("content") or ""), tool_calls=calls,
                     usage=Usage(data.get("prompt_eval_count"), data.get("eval_count")),
                     raw_model=str(data.get("model", "")))
