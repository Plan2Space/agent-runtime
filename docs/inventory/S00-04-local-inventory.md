# S00-04 — Inventário de hardware e modelos locais

Issue: [agent-runtime#53](https://github.com/Plan2Space/agent-runtime/issues/53) · Medido em 2026-11-20 (19:59–20:10 -03:00)
na máquina de desenvolvimento do Felipe. Tudo abaixo vem de comando executado nesta data. Os JSONs brutos estão em
[`evidence/`](evidence/), e a sonda é [`scripts/local_model_probe.py`](../../scripts/local_model_probe.py).

## Hardware e sistema

| Item | Valor medido | Fonte |
|---|---|---|
| SO | Windows 11 Pro 10.0.26200 (build 26200) | `Win32_OperatingSystem` |
| CPU | AMD Ryzen 7 9800X3D, 8 núcleos / 16 threads | `Win32_Processor` |
| RAM | 61,6 GB visíveis (2× 32 GB Corsair CMH64GX5M2B6000C38, **rodando a 4800 MT/s**, não aos 6000 do kit) | `Win32_PhysicalMemory` |
| GPU dedicada | NVIDIA GeForce RTX 5080, 16 303 MiB, compute 12.0, driver 595.97 (CUDA 13.2), limite 360 W | `nvidia-smi` |
| GPU integrada | AMD Radeon (Vulkan). O Ollama a descarta (`OLLAMA_IGPU_ENABLE` não definido) | `server.log` |
| VRAM ocupada sem modelo | ~2,2 GiB (desktop: Chrome, Discord, Steam, Teams, iCUE, apps Claude/ChatGPT/Codex) | `nvidia-smi` |
| Disco do store | `I:` com 871 GB livres. `C:` tem só 36 GB livres | `Get-PSDrive` |

## Servidores e ferramentas locais

| Ferramenta | Estado |
|---|---|
| Ollama | 0.40.16, `ollama serve` em `127.0.0.1:11434` (PID do app tray), `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=0` (auto), `OLLAMA_FLASH_ATTENTION=false`, `OLLAMA_KEEP_ALIVE=5m`, contexto padrão escolhido pela VRAM = 4096 |
| Claude Code CLI | 2.1.158 |
| Codex CLI | 0.164.0 |
| Docker Desktop | instalado, serviço **parado** |
| llama.cpp / LM Studio / vLLM | ausentes. Nenhum outro servidor ouvindo em 1234/5000/7860/8000/8080/8899 |
| Python | a instalação `Python312` do usuário está **vazia** (o `python` do PATH é o stub da Store e falha). Python 3.12.13 disponível via `uv` |

## Achado principal: os modelos não foram perdidos, o servidor olha para o lugar errado

- A variável de usuário `OLLAMA_MODELS=I:\ollama\models` está definida, mas **todos** os `server*.log` (16–20/11)
  mostram o `serve` do app com `OLLAMA_MODELS:C:\Users\felip_local\.ollama\models` e `total blobs: 0`. A configuração
  do app Ollama se sobrepõe à variável: em `%LOCALAPPDATA%\Ollama\db.sqlite`, tabela `settings`, coluna `models`
  vale `C:\Users\felip_local\.ollama\models` (lido numa cópia do arquivo, sem alterar o original).
- Resultado: `ollama list` em `:11434` retorna vazio, embora `I:\ollama\models` contenha 3 modelos de 2026-09-27.
- Os 14 blobs em `I:\ollama\models\blobs` tiveram o SHA-256 recalculado, e **14/14 batem com o nome**: nada
  corrompido.
- Para medir sem alterar a configuração do app, a sonda rodou contra um `ollama serve` temporário em `127.0.0.1:11435`
  com `OLLAMA_MODELS=I:\ollama\models` e `OLLAMA_NOPRUNE=1`. A correção definitiva (apontar o app para `I:`) é uma
  mudança de configuração persistente e espera autorização do Felipe.

## Modelos disponíveis (nenhum presumido)

| Modelo | Digest | Tamanho | Parâmetros / quant. | Capacidades |
|---|---|---|---|---|
| `qwen3:14b` | `bdbd181c33f2` | 9,3 GB | 14.8B Q4_K_M | completion, tools, thinking |
| `qwen2.5-coder:14b` | `9ec8897f747e` | 9,0 GB | 14.8B Q4_K_M | completion, tools, insert |
| `nomic-embed-text:latest` | `0a109f422b47` | 274 MB | 137M F16 | embedding |

**Nenhum Llama instalado no momento do inventário.** Qwen estava presente. O `llama3.1:8b` entrou depois, com autorização (ver adendo).

## Inferência e latência (temperatura 0, seed 42, `num_ctx` 4096, `qwen3` com `think=false`)

Duas execuções completas da sonda ([run1](evidence/s00-04-probe-run1.json), [run2](evidence/s00-04-probe-run2.json)).

| Modelo | Carga a frio (wall) | VRAM do modelo | Na GPU | Geração quente (tok/s, mediana de 3) | Prompt (tok/s) | Aritmética | Código executado |
|---|---|---|---|---|---|---|---|
| qwen3:14b | run1 **48,2 s** (1ª leitura do disco) · run2 2,2 s | 9 209 MiB | 100 % | 80,9 · 79,7 | 2 825 · 2 690 | PASS · PASS | PASS · PASS |
| qwen2.5-coder:14b | 2,1 s · 2,1 s | 9 041 MiB | 100 % | 77,7 · 78,7 | 3 505 · 3 579 | PASS · PASS | PASS · PASS |
| nomic-embed-text | 8,7 s · 1,0 s | 320 MiB | 100 % | — (768 dims, determinístico, quente 24–27 ms) | — | — | — |

- **Aritmética**: `17*23`, resposta aceita só se for exatamente `391`.
- **Código**: a função `is_palindrome` gerada é executada contra 5 asserts num subprocesso `python -I` com timeout de
  10 s. Os testes da própria sonda provam que código errado, ausente ou travado é marcado como FAIL.
- O tempo de 48 s da primeira carga do `qwen3` inclui leitura fria do disco e inicialização CUDA. Nas cargas
  seguintes, ficou em torno de 2 s.

### VRAM em função do contexto ([evidence](evidence/s00-04-context-vram.json), 1 amostra por ponto)

| Modelo | ctx 8 192 | ctx 16 384 | ctx 32 768 |
|---|---|---|---|
| qwen2.5-coder:14b | 9 942 MiB, 100 % GPU, 82 tok/s | 11 357 MiB, 100 % GPU, 84 tok/s | 14 992 MiB, **92,8 % GPU** (transborda para a CPU), 63 tok/s |
| qwen3:14b | 10 003 MiB, 100 % GPU, 57 tok/s | 11 141 MiB, 100 % GPU, 58 tok/s | 13 717 MiB, 100 % GPU, 58 tok/s |

A queda do `qwen3` para cerca de 57 tok/s com ctx ≥ 8k, contra 80 tok/s com 4k, aparece em uma amostra por ponto,
com a GPU compartilhada com o desktop. É uma observação, não uma conclusão. O benchmark do S00-11 precisa repetir
isso.

## Limitações para execução simultânea

1. **Só um modelo 14B por vez.** Com `qwen3:14b` carregado, um pedido ao `qwen2.5-coder:14b` **descarrega o
   primeiro** (`first_evicted: true` nas duas execuções). A troca custa de 2,7 a 2,9 s de carga. Os dois juntos
   precisam de cerca de 18,3 GiB, contra uns 14 GiB livres.
2. **Teto de contexto na GPU**: `qwen2.5-coder:14b` transborda acima de ~16k (com 32k ficam 92,8 % na GPU e a geração
   cai para 63 tok/s). Para um agente com 32k, o `qwen3:14b` cabe; o coder não cabe inteiro.
3. **`OLLAMA_NUM_PARALLEL=1`**: pedidos ao mesmo modelo são serializados. O Harness precisa de fila e timeout, não de
   paralelismo ingênuo.
4. **GPU compartilhada**: o desktop ocupa cerca de 2,2 GiB. O SketchUp/V-Ray da Frente A disputa a mesma placa. Nesta
   medição o SketchUp não estava aberto. Benchmarks pesados (S00-11) não devem rodar junto com render.
5. **Store no `I:`**: a carga a frio depende do disco (48 s na primeira leitura).

## Autorização e adendo Llama (2026-11-20, 22:09)

O Felipe autorizou as duas ações em 2026-11-20. O estado encontrado ao executá-las:

1. **Local dos modelos no app**: `settings.models` já estava `I:\ollama\models` quando o app foi fechado para a
   alteração (o app tinha sido reiniciado nesse meio-tempo). A escrita não mudou nada. Backup do `db.sqlite` antes da
   escrita: `E:\Claude\data\backups\ollama-app-db-2026-11-20\`. Depois de reabrir o app, o `server.log` mostra
   `OLLAMA_MODELS:I:\ollama\models`, e o `:11434` lista os 4 modelos.
2. **`llama3.1:8b`**: já estava no store, puxado via `POST /api/pull` às 20:22 (`server.log`), antes desta sessão
   executar o download. Por isso não foi baixado de novo. O SHA-256 do blob foi recalculado,
   `667b0c1932bc6ffc593ed1d03f895bf2dc8dc6df21db3042284a6f4416b06a29` (4 920 738 944 bytes), e é **igual** à camada
   `model` do manifest do registry.

Sonda contra o `:11434` real ([run3](evidence/s00-04-probe-run3-llama.json), mesmos parâmetros):

| Modelo | Carga (wall) | VRAM do modelo | Na GPU | Geração quente (tok/s) | Prompt (tok/s) | Aritmética | Código executado |
|---|---|---|---|---|---|---|---|
| llama3.1:8b (8.0B Q4_K_M; completion, tools) | 2,3 s | 5 035 MiB | 100 % | 161,3 | 4 229 | PASS | PASS |
| qwen3:14b | 2,4 s | 9 209 MiB | 100 % | 89,7 | 3 227 | PASS | PASS |
| qwen2.5-coder:14b | 4,1 s | 9 041 MiB | 100 % | 90,3 | 4 382 | PASS | PASS |

- **Llama + Qwen 14B também não coexistem**: com o `llama3.1:8b` carregado, pedir o `qwen3:14b` o descarrega
  (`first_evicted: true`). Juntos dariam cerca de 14,2 GiB, no limite da VRAM livre.
- Os Qwen deram cerca de 90 tok/s aqui, contra 78–81 nas execuções 1 e 2. A variação entre execuções, com a GPU
  compartilhada com o desktop, chega a cerca de 15 %. O S00-11 precisa de várias repetições antes de comparar
  modelos.
- **Escopo**: isto prova só a inferência. Ferramentas do Harness, testes e correção de código são critério do
  S00-05/06/11, não deste inventário.

## Ambiente da medição

- Sonda: `scripts/local_model_probe.py` (stdlib), Python 3.12.13 (uv).
- Concorrência durante a medição: apps de desktop abertos (lista acima), SketchUp fechado e GPU sem outro modelo.
