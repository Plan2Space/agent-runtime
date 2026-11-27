# Local First routing — auditoria e desenho (Frente E, 2026-11-27)

SPEC de origem: P2S-LOCAL-FIRST-EXECUTION-001 §4–§8 (pedido literal do Felipe, 2026-11-27).

## O que já existia (auditoria)

| Peça | Onde | O que decide |
|---|---|---|
| `EventStore.claim(event_types, owner)` | `event_store.py` | **qual job** roda: o evento consumível mais antigo do tipo pedido, com lease cercado. Nenhuma noção de modelo. |
| `AgentAdapter` (C8) | `agents/adapter.py` | **como** um backend roda: `discover()` sonda disponibilidade real, `start()` recusa com `REFUSED` (sem fallback) quando indisponível, `result()` devolve resultado tipado. |
| `agents/bench.py`, `agents/events.py` | idem | registros `benchmark_result/1.0` e `agent_event/1.1` — a evidência de capacidade. |
| `agents/resources.py` | idem | amostra GPU/RAM/CPU; o que não mede vira `null`, nunca estimativa. |

Faltava a escolha do **executor**: hoje quem chama escolhe o backend na mão. Não há segundo
orquestrador aqui: `routing.py` fica entre o `claim` (job) e o `AgentAdapter` (execução).

## O que foi acrescentado

- `routing.py` — `route(task, executors, policy, resources) -> RoutingDecision`, puro.
  Prioridade: determinístico → local com prova → outro local compatível com prova → cloud.
  Local sem `Proof` para o tipo de tarefa é rejeitado (`NO_PROVEN_CAPABILITY`): instalado não basta.
  Estratégias `LOCAL_FIRST`, `LOCAL_ONLY`, `HYBRID`, `CLOUD_FIRST`, `MANUAL`; política global com
  override por projeto (`resolve_policy`), orçamento de tokens cloud, escalonamento automático
  desligável, executor manual. Todo executor excluído aparece em `rejected` com o motivo.
- `execution.py` — `execute_with_recovery(decision, run_attempt, store=...)`: tentativas limitadas
  por executor e no total, classificação PROTOCOL/SCHEMA/CONTEXT/TOOL/CAPABILITY, feedback
  estruturado para a próxima tentativa, escalonamento só para cloud que o roteador já admitiu,
  orçamento reaplicado com tokens medidos, sucesso atribuído a quem executou.
- `EventStore`: fatos RECORDED `ROUTING_DECIDED`, `ROUTING_REFUSED`, `EXECUTION_ATTEMPT_STARTED`,
  `EXECUTION_ATTEMPT_FAILED`, `EXECUTION_ESCALATED`, `EXECUTION_COMPLETED`, `EXECUTION_FAILED`,
  `TOKENS_CONSUMED` (`measured` e `estimated` em campos separados), `aggregate_type="task"`,
  `producer="router"`. A Frente D lê estes eventos.

## Em aberto

- Origem das `Proof`: matriz da Frente A + `benchmark_result` da PoC/C. O adaptador que monta
  `Executor.proofs` a partir desses registros entra quando o formato da matriz estiver fixado.
- `run_attempt` real: interface combinada com a PoC (executor local + fila de produção) —
  "executar a tarefa X com o executor Y" contra um motor pinado por SHA/tag, nunca uma worktree.
- Item 8 do §6 (falha recorrente → melhoria do motor) é leitura agregada dos eventos, não lógica aqui.
