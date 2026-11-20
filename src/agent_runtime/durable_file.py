"""durable_file.py — escrita duravel de arquivos de estado (temporario -> fsync -> troca atomica).

Portado de `tools/validation/execution_state.py` (sketchup-mcp, branch do PR #635, HEAD 2aab2ffd), so as tres funcoes
de escrita duravel: `write_durable`, `fsync_dir` e `replace_retry`. Logica inalterada.

Nao inclui o manifesto do executor, as travas por O_EXCL nem os claims: isso e outro componente e fica fora desta migracao.
"""
from __future__ import annotations

import os
from pathlib import Path

def write_durable(p: Path, text: str) -> None:
    """Grava `text` em `p` de forma duravel: temporario -> flush -> fsync -> troca atomica. Sem o `fsync`, uma parada abrupta (queda de
    energia, travamento do SO) pode deixar no NTFS o arquivo com o tamanho certo e o conteudo ZERADO: foi o que se mediu em
    `event_bridge.json` (20.888 bytes, todos zero; #633). O `os.replace` sozinho garante atomicidade, nao durabilidade."""
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    replace_retry(tmp, p)                                                # a TROCA e uma alteracao do DIRETORIO: `replace_retry` sincroniza o diretorio (POSIX) depois dela


def fsync_dir(directory: Path) -> None:
    """Sincroniza o diretorio para que renames/criacoes ja feitos nele sobrevivam a uma queda de energia. Necessario em POSIX; o NTFS persiste a metadata no proprio
    `os.replace` e nao permite abrir um diretorio com `os.open`, entao la e no-op. Falha de sync de diretorio nunca derruba a gravacao ja feita."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def replace_retry(src: Path, dst: Path, tries: int = 100, pause: float = 0.02) -> None:
    """`os.replace` atomico, repetido enquanto o Windows recusa por instantes (PermissionError/WinError 32 e 5): um leitor sem trava que tem o arquivo aberto
    (ex.: `status`) impede a troca ate fechar. Passado o prazo (~2 s) o erro sobe: nunca se grava por cima de outro jeito."""
    import time
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            fsync_dir(Path(dst).parent)                                  # durabilidade da troca (quarentena, promocao do pendente, gravacao do estado)
            return
        except PermissionError:
            if attempt + 1 == tries:
                raise
            time.sleep(pause)
