# B8 — Benchmark geométrico (desenho, NÃO executado)

Estado: preparado. Execução bloqueada até a Frente A publicar o motor (`Plan2Space/sketchup-mcp-next`) e a interface
fixture/executor/validador no `engineering-specs`. Nada aqui foi rodado.

Interface publicada pela Frente A: `Plan2Space/engineering-specs@5ea82f612f33b8f1bd26d54062d9573c6af939b0`
`plan2space/P2S-CONTRACTS-v1.md` (servidor MCP `p2s-engine`, tool `build_room`, sucesso só com
`validation_report.verdict == "PASS"`). Falta o motor rodando para executar.

## Entrada que falta (dona: Frente A)

- Fixture `room_spec` (S00-02 §C1, valores da S00-08) com `schema_version` e `sha256`.
- Tool MCP `build_room` (S00-02 §C2): `fixture_path`, `out_dir`, `run_id` → `status`, `manifest_path`, `error`.
- `validation_report.json` (§C5): `verdict` PASS/FAIL/BLOCKED, `checks[]`, `fixture_sha256`, `geometry_report_sha256`.

## Desenho

Mesma regra do S00-11: mesmo commit base do `-next`, mesma fixture (mesmo `sha256`), mesmas instruções, mesmo
contrato de ferramentas, mesmos limites, worktree independente por modelo e por repetição.

1. Tarefa por cenário: um defeito geométrico controlado no planejador do `-next` (ex.: offset de abertura medido do
   bordo errado; espessura de parede aplicada duas vezes; peitoril somado à altura da porta). Cada cenário tem um teste
   unitário falhando no núcleo **e** um alvo geométrico no `validation_report`.
2. Ferramentas do harness = as do S00-11 + `build_room` (servida pelo MCP do `-next`, chamada pelo harness com a
   mesma allowlist). Nenhum agente recebe acesso ao SketchUp fora dessa tool.
3. Avaliação determinística, nunca o próprio agente:
   - testes do núcleo (visíveis + ocultos);
   - `validation_report.verdict == PASS` com `reopened = true`, gerado **por execução** (cada modelo dispara o seu
     build; um mesmo build nunca conta para três modelos);
   - `fixture_sha256` do relatório == `sha256` da fixture do cenário (amarra o veredito à entrada);
   - tolerâncias vêm da fixture; mudar tolerância = `CONTRACT_VIOLATION` (arquivo protegido).
4. `visual_verdict` continua `PENDING_HUMAN`; o benchmark não declara aparência.
5. GPU compartilhada: SketchUp (Frente A) e modelo local não rodam ao mesmo tempo; o runner serializa e registra
   `nvidia-smi` por execução (já feito pelo `ResourceSampler`).

## Saída

Igual ao S00-11 (`benchmark_result` v1) com dois campos opcionais (MINOR 1.1): `geometry.verdict` e
`geometry.validation_report_sha256`.
