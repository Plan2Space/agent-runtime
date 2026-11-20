# ADR 0001: Estratégia de extração do runtime (FP-062)

**Status:** aceita (2026-11-20). **SPEC:** https://github.com/Plan2Space/sketchup-mcp/issues/676

## Contexto

O FP-060 misturou orquestração genérica de agentes com o produto SketchUp. A fila de PRs
foi encerrada sem merge para reconstruir por fronteira de domínio. Os PRs originais
continuam sendo a fonte canônica do diff, das revisões e das evidências.

## Decisão

1. **Contratos primeiro.** `agent_runtime.contracts` define o envelope de job versionado.
   Consumidores dependem só dele.
2. **Strangler.** Cada peça é portada de um PR fechado, com teste e rollback próprios.
   O código antigo em `sketchup-mcp` continua no ar até a peça nova ter paridade provada.
3. **Uma fonte autoritativa por fila.** Não rodar dois armazenamentos autoritativos em
   paralelo. A troca de fonte passa por detector de divergência antes do corte.
4. **Sem merge cego de PRs empilhados.** Cada PR portado é revisado no diff completo.
5. **Sem dependência de produto.** `agent-runtime` não importa `sketchup-mcp` nem `agent-bar`.
   `sketchup-mcp` não depende de agentes de IA.
6. **Branches não são apagadas.** Os PRs fechados e suas branches ficam como evidência.

## Ordem de extração sugerida

1. Contratos e validação (este bootstrap).
2. EventStore com leases, fencing e DLQ.
3. Outbox transacional e publicador com reconciliação.
4. Workers de implementação e revisão.
5. Adapters de CLI (Claude Code, Codex) atrás de interface.
6. Supervisor no Windows.

## Consequências

- Cada etapa tem rollback por flag, desligado por padrão quando tocar produção.
- A matriz em `docs/migration/pr-issue-matrix.md` rastreia PR antigo → issue → repositório → estado.
- Provas de concorrência, recuperação de crash e E2E real ainda são pendentes e ficam
  nas issues de implementação.
