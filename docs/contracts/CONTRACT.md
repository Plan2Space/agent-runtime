# Contrato público do agent-runtime

Versão do documento: 0.1.0 (alinhada ao pacote). Este é o único contrato que consumidores podem usar. Qualquer outro módulo ou função é interno e pode mudar sem aviso.

## Política de versão

- Versionamento semântico. Tags imutáveis no formato `vMAJOR.MINOR.PATCH`. Uma tag nunca é movida ou reutilizada.
- **MAJOR:** quebra de contrato ou de schema do banco. Exige migração documentada.
- **MINOR:** função, campo ou método novo, sem quebrar o existente.
- **PATCH:** correção sem mudança de API.
- Consumidores fixam a versão por tag ou SHA. Nunca consomem `main`.

## Superfície pública (v0.1.x)

### `agent_runtime.contracts`

- `JobEnvelope.from_dict(data) -> JobEnvelope`: valida `schema_version`, `job_id`, `kind`, `idempotency_key` e `payload`. Levanta `ContractError`.
- `CONTRACT_VERSION = "1.0.0"`, `SUPPORTED_MAJOR = 1`, `ALLOWED_KINDS`.

### `agent_runtime.event_store`

- `EventStore(path, *, clock=time.time, busy_timeout_ms=15000)`.
- Métodos públicos: `publish`, `claim`, `claim_by_id`, `renew`, `complete`, `fail`, `supersede`, `run_started`, `run_finished`, `claim_outbox`, `renew_outbox`, `complete_outbox`, `fail_outbox`, `pending_outbox`, `outbox_entries`, `events`, `get`, `transitions`, `runs`, `job_terminal`, `stats`.
- `LeaseLost`: exceção pública quando o dono do lease perdeu a posse.
- Constantes: `SCHEMA_VERSION = 1`, `EVENT_TYPES`, `CONSUMABLE`.

**Schema do banco (versão 1):** tabelas `meta`, `events`, `transitions`, `subprocess_runs`, `outbox`. Leitores externos (ex.: agent-bar) dependem apenas de `events`, `transitions` e `outbox`, com as colunas usadas por eles. Mudança nessas colunas é MAJOR.

### `agent_runtime.outbox_publisher`

- `publish_one(store, targets, owner, *, lease_seconds=60.0, target=None, retry_delay=1.0, after_send=None) -> dict`.
- `drain(store, targets, owner, *, limit=20, **options) -> list[dict]`.
- `Target` (protocolo): `find(entry) -> str | None`, `send(entry) -> str`, `send_timeout_s` opcional.
- `LedgerTarget(path)`: alvo de arquivo local, usado em testes e laboratório.
- `TargetError`.

Garantias: reconciliação pela chave antes de todo envio; ACK só do dono do lease; sem reconciliação não há envio. **Não há exactly-once** sem alvo com idempotência atômica.

### `agent_runtime.durable_file`

- `write_durable(path, text)`: temporário, fsync e troca atômica.
- `fsync_dir(directory)`.
- `replace_retry(src, dst, tries=100, pause=0.02)`.

## Registros de agente (formato de arquivo, não API Python)

O harness de agentes (`agent_runtime.agents`, S00-05/S00-06) grava dois registros versionados que consumidores leem
como arquivo, sem importar o pacote:

- `agent_event` v1 — `docs/contracts/agent_event.v1.schema.json`, um por linha em `<run_dir>/events.jsonl`.
- `benchmark_result` v1 — `docs/contracts/benchmark_result.v1.schema.json`, `<run_dir>/result.json`.

Texto normativo: `Plan2Space/engineering-specs` `plan2space/S00-05_agent_records_and_adapter.md` (citar por SHA).
Leitor rejeita major diferente de 1 e ignora campo desconhecido. O módulo Python `agent_runtime.agents` e
`agent_runtime.approval` ainda são internos (fora da prova de rollback) até a próxima versão MINOR do pacote.

## Merge gate (S00-14) — fatos no EventStore

`EVENT_TYPES` ganha `MERGE_DECISION` (adição MINOR; não consumível, status `RECORDED`). Uma linha por execução de
`scripts/merge_gate.py` / `agent_runtime.merge_gate.run_gate`, `job_id` = `owner/repo#n`. Payload:
`pr`, `url`, `author`, `head_sha`, `base`, `status` (`DRY_RUN_WOULD_MERGE`, `MERGED`, `WAITING_HUMAN`,
`HIGH_HUMAN_ONLY`, `NOT_ELIGIBLE`, `HEAD_CHANGED`, `BLOCKED`, `NOT_OPEN`), `risk`, `risk_reasons`, `ci_state`,
`checks`, `reasons`, `reason` (motivo do GitHub quando `BLOCKED`), `needs` (o que falta, ou null), `dry_run`,
`approvers` (configuração por nível), `approval` (`approver`, `kind`, `risk`, `head_sha`, `source`, ou null),
`decided_at`, `policy`. Um merge real grava também `PR_MERGED` (`pr`, `head_sha`, `merge_commit`, `risk`,
`merged_by`, `approval_source`, `merged_at`).

Política (decisão do mantenedor, 2026-11-21): LOW + CI verde + evidência → merge pelo runtime; MEDIUM idem **e** review do GitHub
`APPROVED` no HEAD exato por um login configurado como aprovador MEDIUM, que não seja o autor do PR e cujo review mais
recente seja essa aprovação; HIGH → nunca pelo runtime (`HIGH_HUMAN_ONLY`), mesmo com review. Comentários não disparam
merge. Sempre `--match-head-commit`, nunca `--admin`; dry-run por padrão. Nenhum fato contém texto de aprovação (R16).

## Fora do contrato

- Módulos `_`-prefixados e qualquer caminho não listado acima.
- Caminhos de banco por variável de ambiente: a configuração ainda não tem contrato (ver issue de configuração).
- Nenhum módulo de UI, de GitHub ou de SketchUp. O runtime não importa `sketchup-mcp` nem `agent-bar`.

## Consumidores

| Consumidor | O que usa | Versão |
|---|---|---|
| sketchup-mcp | (a integrar) `agent_runtime.event_store`, `durable_file` | a fixar por tag |
| agent-bar | leitura do schema v1 por SQL, sem import do pacote | schema 1 |

## Como verificar

Ambiente de verificação: dependências **fixas** em `scripts/verify-requirements.txt` (pytest 8.4.2 e dependências exatas). Nenhuma faixa de versão.

```
python -m venv venv
venv/bin/python -m pip install -r scripts/verify-requirements.txt   # Windows: use venv\Scripts\python.exe
python -m pip wheel . --no-deps -w dist
python scripts/verify_install.py --wheel dist/agent_runtime-0.1.0-py3-none-any.whl --expect-version 0.1.0
```

O que a verificação prova:
- o wheel é instalado num ambiente novo, sem o código-fonte;
- as chamadas usam modo isolado (`-I`): PYTHONPATH herdado, site do usuário e a pasta atual não entram;
- o módulo importado vem do ambiente instalado, não do checkout (falha se vier do repositório);
- a versão instalada é exatamente a esperada, e a major do contrato é a suportada;
- a suíte de contrato, em `tests/contract/`, roda contra o pacote instalado, fora do repositório.

Os testes da própria verificação (`tests/install/`) cobrem os casos negativos: versão divergente, wheel corrompido, wheel ausente, rollback com a mesma versão e PYTHONPATH vazado. Rodam no job `install-check`.

## Rollback: estado

**Pendente.** A prova de rollback (`--rollback-wheel`) exige duas versões publicadas de verdade. Hoje existe uma (0.1.0, ainda não tagueada). Até a segunda existir, a opção só aceita versões diferentes e não há resultado de rollback registrado.

Regra de testes: `tests/contract/` usa somente a superfície listada acima. Ele é o único conjunto que a prova de rollback roda. Recursos de uma versão específica ficam em `tests/` e nunca entram na prova de rollback.
