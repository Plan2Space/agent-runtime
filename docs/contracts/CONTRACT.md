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

`python scripts/verify_install.py --wheel dist/agent_runtime-0.1.0-py3-none-any.whl` instala o wheel num ambiente novo, importa a superfície pública e roda a suíte de contrato contra o pacote instalado (não contra o código-fonte).
