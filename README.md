# agent-runtime

Runtime durável de agentes do Plan2Space: EventStore, outbox transacional, workers,
adapters de CLI (Claude Code / Codex), supervisor, integração com GitHub,
agendamento, idempotência e recuperação.

## Fronteiras

- Não depende de `sketchup-mcp` (geometria, SketchUp, móveis) nem de `agent-bar` (UI).
- Os consumidores falam com o runtime por **contratos versionados** (`src/agent_runtime/contracts.py`).
- Um único armazenamento autoritativo por fila. Nada de duas fontes de verdade em paralelo.

## Origem

Extraído na reorganização FP-062:

- SPEC: https://github.com/Plan2Space/sketchup-mcp/issues/676
- Epic: https://github.com/Plan2Space/sketchup-mcp/issues/675
- Matriz PR → issue: [docs/migration/pr-issue-matrix.md](docs/migration/pr-issue-matrix.md)
- Decisão de estratégia: [docs/adr/0001-extraction-strategy.md](docs/adr/0001-extraction-strategy.md)

## Desenvolvimento

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

CI roda os testes em Python 3.11 e 3.12 (`.github/workflows/ci.yml`).
