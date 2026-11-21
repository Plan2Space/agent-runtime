# Runbook — merge gate (S00-14, fase 0)

O merge gate lê um PR no GitHub, classifica o risco, confere CI e aprovações e decide. Na fase 0 ele só faz merge
de PR **LOW** (só documentação não executável). MEDIUM e HIGH nunca são mergeados pelo runtime: ficam em
"aguardando humano" e um humano faz o merge.

## Rodar

Sempre de dentro do repositório, com o venv dele. O padrão é **dry-run**: decide e grava o fato, sem mergear.

```powershell
.venv\Scripts\python.exe scripts\merge_gate.py --repo Plan2Space/agent-runtime --pr <n> --store <events.sqlite> --approver <login> --log <gate.jsonl>
```

Para mergear de verdade (só LOW), acrescente `--execute`. `--approver` é configuração (repita para vários logins humanos); sem aprovador configurado nenhuma aprovação conta.

## O que cada estado quer dizer

| Estado | Significado | Quem age |
|---|---|---|
| `DRY_RUN_WOULD_MERGE` | LOW, CI verde, evidência válida; não mergeou porque faltou `--execute` | operador |
| `MERGED` | mergeado pelo runtime com `--match-head-commit` | ninguém |
| `WAITING_HUMAN` | MEDIUM/HIGH sem aprovação humana no HEAD atual | humano aprova |
| `APPROVED_HUMAN_MERGE_REQUIRED` | aprovado no HEAD, mas a fase 0 não faz merge de MEDIUM/HIGH | humano faz o merge |
| `NOT_ELIGIBLE` | CI não está verde, ou PR em rascunho, sem evidência no corpo, ou base errada | autor |
| `HEAD_CHANGED` | o HEAD mudou entre a decisão e o merge | rodar de novo |
| `BLOCKED` | o GitHub recusou o merge; o motivo do GitHub fica no fato | humano |
| `NOT_OPEN` | PR fechado ou já mergeado | ninguém |

## Garantias

- Nunca usa `--admin` nem `--auto` e nunca altera proteção de branch.
- Commit novo no PR invalida qualquer aprovação anterior.
- Cada execução grava um fato `MERGE_DECISION` no EventStore (e `PR_MERGED` quando mergeia). Nenhum fato contém
  texto que possa ser lido como aprovação.
- A conta GitHub hoje é compartilhada entre agentes e humano. Por isso o merge automático fica restrito a LOW até
  os agentes terem conta própria.
