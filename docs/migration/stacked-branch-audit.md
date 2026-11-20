# Auditoria da pilha de branches (FP-062 — prioridade 2)

SPEC: https://github.com/Plan2Space/sketchup-mcp/issues/676 · Epic: https://github.com/Plan2Space/sketchup-mcp/issues/675

Método: leitura do corpo de cada um dos 28 PRs fechados (texto completo, não truncado), extração das
frases com linguagem explícita de empilhamento ("Empilhado sobre/em #N"), com duas correções manuais
feitas depois de ler o PR inteiro (ver Ressalvas). **Isto não substitui ler o diff de cada PR antes de
portar** — é o grafo de dependência declarado nos textos, não uma prova de compatibilidade de código.

## DAG de dependências (declaradas pelo autor no corpo do PR)

| PR | Empilhado sobre | Observação |
|---|---|---|
| #674 | — | — |
| #672 | — | — |
| #671 | #665 | — |
| #670 | #630 | — |
| #669 | — | desbloqueia ambientalmente #643, #652, #640, #642 (não é dependência de pilha) |
| #668 | — | — |
| #665 | #648 | — |
| #663 | — | — |
| #660 | — | — |
| #657 | #649, #636, #641, #642, #646, #648 | direto só em #649; #636, #641, #642, #646, #648 chegam transitivamente por #649 |
| #652 | — | — |
| #649 | #646, #636, #641, #642, #648 | — |
| #648 | — | — |
| #646 | #636, #641 | — |
| #645 | #630 | — |
| #643 | — | — |
| #642 | #636 | — |
| #641 | — | — |
| #640 | — | — |
| #636 | #634 | — |
| #635 | — | — |
| #634 | — | — |
| #632 | #618, #619, #630 | também empilhado em #617 (já MERGED em develop) |
| #630 | #618, #619 | — |
| #619 | #618 | — |
| #618 | — | — |
| #614 | — | — |
| #605 | — | — |

## Ordem segura de migração (topológica, sem ciclos)

Migrar/portar nesta ordem — cada PR só depois dos que ele declara empilhar sobre:

```
#605 → #614 → #618 → #619 → #630 → #632 → #634 → #635 → #636 → #640 → #641 → #642 → #643 → #645 → #646 → #648 → #649 → #652 → #657 → #660 → #663 → #665 → #668 → #669 → #670 → #671 → #672 → #674
```

Não houve ciclo. #672 e #669 ficam deliberadamente no fim do grupo de dependentes diretos de pouca
coisa (ver ressalvas), mas tecnicamente não dependem de nada — a ordem exata entre eles e seus pares
sem dependência (#605, #614, #660, #663, #668, #674) pode ser ajustada por prioridade de produto.

## Candidatos a duplicação / sobreposição (por título e domínio, a confirmar no diff)

| Área | PRs | Risco |
|---|---|---|
| Matar árvore de processos / sandbox do executor | #643, #669 | Ambos tocam `event_executor.kill_tree`/lock de estado; portar #669 primeiro (ambiental) e reverificar #643 por cima, não em paralelo. |
| Fonte SQLite do EventStore / detector de divergência | #634, #636, #642 | Linha reta de evolução (#634 lê, #636 reivindica, #642 reconcilia); não paralelizar. |
| Outbox / publicação no GitHub | #618, #619, #630, #632 | Cadeia única #618→#619→#630→#632; #632 também cita #617 (já em `develop`) — confirmar se o diff de #632 ainda se aplica. |
| Merge automático / reconciliação do ciclo do executor | #648, #649, #657 | #657 só precisa de #649 diretamente; #649 já inclui #636/#641/#642/#646/#648 — portar #649 por último do grupo evita re-aplicar o mesmo código duas vezes. |
| Ciclo de vida do PR / limites de uso do revisor | #665, #671 | #671 é resiliência sobre o que #665 cria; mesma área de código (`pr_lifecycle`, `codex_reviewer`). |

## Ressalvas (encontradas só ao ler o corpo completo, não o resumo truncado da SPEC)

- **#672 não depende da pilha.** O corpo cita "#670 estava empilhado sobre #630 → #619 → #618" para
  explicar por que #672 foi aberto **separado** — é contexto sobre outro PR, não uma dependência própria.
  #672 é descrito como "o lado confiável, que **não depende** da pilha".
- **#669 não depende de #643/#652/#640/#642 — é o inverso.** #669 corrige o ambiente do sandbox
  (AppContainer) que fazia os testes desses 4 PRs falharem; portar #669 primeiro é o que os desbloqueia.
- **#646 não depende de #645.** A menção a #645 no corpo é uma referência cruzada ("testes no
  AppContainer (#645)" descrevendo o comportamento existente de `deliver`), não um empilhamento.
- **#674 teve a HEAD alterada** depois da SPEC listar `d54e9100` (HEAD real no fechamento: `18736dd2`).
