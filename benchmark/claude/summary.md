# Benchmark S00-11 — claude

Modelo: `claude-sonnet-5-5` · digest `None` · quant `None` · commit agent-runtime `14f64f43079b`

| run | outcome | tentativas | tool calls | erros de tool | testes depois | ocultos | tempo (s) | tokens (p/c) |
|---|---|---|---|---|---|---|---|---|
| claude-s1_paginate-r1 | SUCCESS | 1 | 5 | 0 | 4/4 | PASS | 8.703 | 8077/409 |
| claude-s1_paginate-r2 | SUCCESS | 1 | 5 | 0 | 4/4 | PASS | 8.391 | 8075/400 |
| claude-s1_paginate-r3 | SUCCESS | 1 | 5 | 0 | 4/4 | PASS | 8.531 | 8077/395 |
| claude-s2_duration-r1 | SUCCESS | 1 | 6 | 0 | 4/4 | PASS | 11.844 | 14818/746 |
| claude-s2_duration-r2 | SUCCESS | 1 | 6 | 0 | 4/4 | PASS | 11.063 | 14818/740 |
| claude-s2_duration-r3 | SUCCESS | 1 | 6 | 0 | 4/4 | PASS | 11.734 | 14968/817 |
| claude-s3_inventory-r1 | SUCCESS | 1 | 10 | 0 | 4/4 | PASS | 14.578 | 23100/1202 |
| claude-s3_inventory-r2 | SUCCESS | 1 | 9 | 0 | 4/4 | PASS | 13.297 | 17111/1123 |
| claude-s3_inventory-r3 | SUCCESS | 1 | 9 | 0 | 4/4 | PASS | 13.297 | 17091/1147 |

Sucesso funcional: **9/9**. Intervenção humana: nenhuma.

Erros: nenhum
