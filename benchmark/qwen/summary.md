# Benchmark S00-11 — qwen

Modelo: `qwen3:14b` · digest `bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8` · quant `Q4_K_M` · commit agent-runtime `14f64f43079b`

| run | outcome | tentativas | tool calls | erros de tool | testes depois | ocultos | tempo (s) | tokens (p/c) |
|---|---|---|---|---|---|---|---|---|
| qwen-s1_paginate-r1 | SUCCESS | 1 | 5 | 0 | 4/4 | PASS | 8.438 | 5453/223 |
| qwen-s1_paginate-r2 | SUCCESS | 1 | 5 | 0 | 4/4 | PASS | 5.125 | 5453/224 |
| qwen-s1_paginate-r3 | SUCCESS | 1 | 5 | 0 | 4/4 | PASS | 5.062 | 5453/225 |
| qwen-s2_duration-r1 | BLOCKED | 3 | 42 | 0 | 1/4 | — | 48.375 | 195488/2608 |
| qwen-s2_duration-r2 | SUCCESS | 2 | 34 | 0 | 4/4 | PASS | 36.61 | 187679/1960 |
| qwen-s2_duration-r3 | BLOCKED | 3 | 59 | 12 | 3/4 | — | 69.531 | 355710/4136 |
| qwen-s3_inventory-r1 | FUNCTIONAL_FAILURE | 1 | 10 | 0 | 4/4 | FAIL | 8.641 | 24581/428 |
| qwen-s3_inventory-r2 | FUNCTIONAL_FAILURE | 1 | 10 | 0 | 4/4 | FAIL | 8.969 | 24563/488 |
| qwen-s3_inventory-r3 | FUNCTIONAL_FAILURE | 1 | 18 | 2 | 4/4 | FAIL | 12.718 | 68767/630 |

Sucesso funcional: **4/9**. Intervenção humana: nenhuma.

Erros: qwen-s2_duration-r1: tests still failing after 3 attempts; qwen-s2_duration-r3: tests still failing after 3 attempts; qwen-s3_inventory-r1: visible tests pass but hidden evaluator rejects the change; qwen-s3_inventory-r2: visible tests pass but hidden evaluator rejects the change; qwen-s3_inventory-r3: visible tests pass but hidden evaluator rejects the change
