# Runbook — merge gate (S00-14)

O merge gate lê um PR no GitHub, classifica o risco, confere CI e aprovações e decide.

| Risco | O runtime mergeia? |
|---|---|
| LOW (só documentação não executável) | sim, com CI verde e evidência no corpo do PR |
| MEDIUM | sim, com CI verde, evidência **e** review do GitHub `APPROVED` no HEAD exato por um login configurado como aprovador MEDIUM, que não seja o autor do PR |
| HIGH | **nunca**, mesmo com review aprovado. O mantenedor aprova e o coordenador faz o merge travado no HEAD |

## Rodar

Sempre de dentro do repositório, com o venv dele. O padrão é **dry-run**: decide e grava o fato, sem mergear.

```powershell
.venv\Scripts\python.exe scripts\merge_gate.py --repo Plan2Space/agent-runtime --pr <n> --store <events.sqlite> --approver MEDIUM=<reviewer-login> --log <gate.jsonl>
```

Para mergear de verdade, acrescente `--execute`. `--approver NIVEL=LOGIN` é configuração (repita para vários
logins); sem aprovador MEDIUM configurado, nenhum PR MEDIUM é mergeado. Aprovadores HIGH são ignorados.

## O que cada estado quer dizer

| Estado | Significado | Quem age |
|---|---|---|
| `DRY_RUN_WOULD_MERGE` | elegível; não mergeou porque faltou `--execute` | operador |
| `MERGED` | mergeado pelo runtime com `--match-head-commit` | ninguém |
| `WAITING_HUMAN` | MEDIUM sem review `APPROVED` válido no HEAD atual | revisor aprova |
| `HIGH_HUMAN_ONLY` | HIGH: nunca mergeado pelo runtime | mantenedor aprova, coordenador mergeia |
| `NOT_ELIGIBLE` | CI não está verde, ou PR em rascunho, sem evidência no corpo, ou base errada | autor |
| `HEAD_CHANGED` | o HEAD mudou entre a decisão e o merge | rodar de novo |
| `BLOCKED` | o GitHub recusou o merge; o motivo do GitHub fica no fato | humano |
| `NOT_OPEN` | PR fechado ou já mergeado | ninguém |

## Garantias

- Nunca usa `--admin` nem `--auto` e nunca altera proteção de branch.
- Commit novo invalida o review; um `CHANGES_REQUESTED` posterior do mesmo revisor retira a aprovação.
- Review do próprio autor do PR é ignorado. Comentários nunca disparam merge.
- Cada execução grava um fato `MERGE_DECISION` no EventStore (e `PR_MERGED` quando mergeia). Nenhum fato contém
  texto que possa ser lido como aprovação.
