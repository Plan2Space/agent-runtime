# Benchmark S00-11 — comparação Claude × Qwen × Llama

Mesmo commit base por cenário: `s1_paginate` sim, `s2_duration` sim, `s3_inventory` sim

| Backend | Modelo | Sucesso | Testes visíveis | Ocultos (qualidade) | Tentativas (mediana) | Tempo s (mediana) | Tool calls (mediana) | Taxa erro tool | Tokens p/c (mediana) | GPU máx MiB (sistema) | Intervenção humana | Recomendação |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude | `claude-sonnet-5-5`  | 9/9 | 9/9 | 9/9 | 1 | 11.73 | 6 | 0.0 | 14818/746 | 12902.0 | 0 | **autonomo_controlado** |
| qwen | `qwen3:14b` Q4_K_M | 4/9 | 7/9 | 4/9 | 1 | 8.97 | 10 | 0.074 | 24581/488 | 12911.0 | 0 | **so_tarefas_simples** |
| llama | `llama3.1:8b` Q4_K_M | 0/9 | 0/9 | 0/9 | 3 | 10.03 | 5 | 0.0 | 7742/528 | 12906.0 | 0 | **inadequado** |

## Por cenário (sucesso / execuções, desfechos)

| Backend | s1_paginate | s2_duration | s3_inventory |
|---|---|---|---|
| claude | 3/3 (SUCCESS, SUCCESS, SUCCESS) | 3/3 (SUCCESS, SUCCESS, SUCCESS) | 3/3 (SUCCESS, SUCCESS, SUCCESS) |
| qwen | 3/3 (SUCCESS, SUCCESS, SUCCESS) | 1/3 (BLOCKED, SUCCESS, BLOCKED) | 0/3 (FUNCTIONAL_FAILURE, FUNCTIONAL_FAILURE, FUNCTIONAL_FAILURE) |
| llama | 0/3 (BLOCKED, BLOCKED, BLOCKED) | 0/3 (BLOCKED, BLOCKED, BLOCKED) | 0/3 (BLOCKED, BLOCKED, BLOCKED) |

## Regra de recomendação (determinística)

- Recommendation per backend is a deterministic rule over the measured success rates (no subjective score):
- autonomo_controlado        : SUCCESS in >= 8/9 runs, every scenario >= 2/3, zero contract violations
- assistente_supervisionado  : SUCCESS in >= 2/3 of s1 and s2 runs, zero contract violations
- so_tarefas_simples         : SUCCESS in >= 2/3 of s1 runs
- inadequado                 : otherwise

## Leitura, limitações e o que NÃO concluir

- **Mesmo código para os três:** os `run_manifest.json` registram o mesmo commit do agent-runtime e o mesmo `core_sha256` (todo `src/agent_runtime/agents/*.py` + `benchmark/scenarios/**`). Sujeira registrada em `agent_runtime_dirty` é só saída de outros backends e docs.
- **Amostra pequena:** 3 cenários × 3 repetições. Serve para separar "nunca/às vezes/sempre" neste conjunto, não para estimar taxa de sucesso geral nem declarar equivalência entre modelos.
- **Não é a mesma classe de modelo:** Claude (`claude-sonnet-5-5`, nuvem) × Qwen 14.8B Q4_K_M × Llama 8.0B Q4_K_M locais. O resultado compara o que roda nesta máquina hoje, não famílias de modelos.
- **Sampling:** Ollama com temperatura 0.3 e seed 1001/1002/1003 por repetição, `num_ctx` 16384, Qwen com `think=false`. O `claude -p` não expõe temperatura/seed: as repetições do Claude não são semeadas.
- **Laço:** nos modelos locais o harness conduz o laço; no Claude o CLI conduz, só com as ferramentas do harness via MCP (`--tools ""`). Mesmo prompt de sistema, mesmas ferramentas, mesma política, mesmos limites.
- **Tokens:** Claude soma entrada + cache lido + cache criado (o cache barateia, mas conta como contexto); Ollama reporta `prompt_eval_count`/`eval_count`. Custos em USD do CLI são o número informado pela assinatura, não cobrança.
- **Recursos:** GPU/RAM são do sistema inteiro. O run do Claude rodou em paralelo ao do Llama, então o máximo de GPU na linha do Claude é o do Llama + desktop, não do Claude. Os locais rodaram um por vez.
- **s3 tem um requisito só na docstring** (commit de reserva expirada), cobrado só pelo teste oculto: mede se o agente lê a especificação além dos testes. Os 3 FUNCTIONAL_FAILURE do Qwen corrigiram os outros 3 defeitos e falharam exatamente nesse (reavaliado aplicando cada `changes.patch` na fixture: só `test_commit_expired_raises_and_keeps_stock` falha, 7/8 ocultos passam).
- **Patches:** gravados com CRLF pelo `write_text` do Windows na execução; normalizados para LF depois. O `diff.sha256` registrado foi calculado sobre o texto LF e bate com os 27 arquivos normalizados.
- **Falha do Llama 3.1 8B:** começa com tool calls nativas e passa a escrever JSON em texto chamando ferramenta inexistente (`edit_file`) em caminho inventado; o contrato aceita só tool calling nativo, igual para todos. Um parser de JSON em texto mudaria o contrato e não foi aplicado.
- **Execução inválida descartada (não entra na tabela):** a 1ª rodada do Claude mostrou o servidor MCP do harness em `pending` em parte das tentativas (o modelo ficou sem ferramentas). Corrigido (conexão bloqueante + checagem do init + falha de infra em vez de tentativa do agente) e os três backends foram re-rodados no mesmo commit.
- **Sem intervenção humana** em nenhuma execução; nenhuma solução compartilhada (worktree e fixture próprios por backend e repetição).
