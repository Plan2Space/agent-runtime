# Benchmark S00-11 — llama

Modelo: `llama3.1:8b` · digest `46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e` · quant `Q4_K_M` · commit agent-runtime `14f64f43079b`

| run | outcome | tentativas | tool calls | erros de tool | testes depois | ocultos | tempo (s) | tokens (p/c) |
|---|---|---|---|---|---|---|---|---|
| llama-s1_paginate-r1 | BLOCKED | 3 | 5 | 0 | 2/4 | — | 11.531 | 4698/528 |
| llama-s1_paginate-r2 | BLOCKED | 3 | 5 | 0 | 2/4 | — | 10.656 | 4700/282 |
| llama-s1_paginate-r3 | BLOCKED | 3 | 5 | 0 | 2/4 | — | 6.282 | 4697/297 |
| llama-s2_duration-r1 | BLOCKED | 3 | 4 | 0 | 0/4 | — | 8.25 | 7870/427 |
| llama-s2_duration-r2 | BLOCKED | 3 | 6 | 0 | 0/4 | — | 13.906 | 4094/1354 |
| llama-s2_duration-r3 | BLOCKED | 3 | 6 | 0 | 0/4 | — | 8.11 | 12659/372 |
| llama-s3_inventory-r1 | BLOCKED | 3 | 4 | 0 | 1/4 | — | 24.797 | 7742/2937 |
| llama-s3_inventory-r2 | BLOCKED | 3 | 6 | 0 | 1/4 | — | 10.031 | 13822/676 |
| llama-s3_inventory-r3 | BLOCKED | 3 | 4 | 0 | 1/4 | — | 8.563 | 7801/561 |

Sucesso funcional: **0/9**. Intervenção humana: nenhuma.

Erros: llama-s1_paginate-r1: tests still failing after 3 attempts; llama-s1_paginate-r2: tests still failing after 3 attempts; llama-s1_paginate-r3: tests still failing after 3 attempts; llama-s2_duration-r1: tests still failing after 3 attempts; llama-s2_duration-r2: tests still failing after 3 attempts; llama-s2_duration-r3: tests still failing after 3 attempts; llama-s3_inventory-r1: tests still failing after 3 attempts; llama-s3_inventory-r2: tests still failing after 3 attempts; llama-s3_inventory-r3: tests still failing after 3 attempts
