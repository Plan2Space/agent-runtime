# agent-runtime — instruções para Codex CLI

## Conhecimento de engenharia Plan2Space

Consulte `Plan2Space/claude-engineering-skills` na release `v0.9.3`, commit `3f93ccb001d4609527587ddfa1a716ca5a09b38c`. O catálogo autoritativo é `catalog/skills.json`; use `skills/spec-author/SKILL.md` para escrever SPECs, `skills/spec-progress-keeper/SKILL.md` para acompanhar marcos e `skills/backlog-governance/SKILL.md` para triagem de Issues/Board. Registre a skill e esse SHA na evidência. A instalação automática por lockfile ainda é a microtarefa [hub #29](https://github.com/Plan2Space/claude-engineering-skills/issues/29); não declare uma skill carregada sem verificá-la.

O [contrato de ownership](https://github.com/Plan2Space/claude-engineering-skills/blob/v0.9.3/contracts/ownership.v1.md) mantém skills reutilizáveis e contratos compartilhados no hub. SPECs, código, testes e contratos internos de execução do `agent-runtime` ficam neste repositório. Cite SPECs externas por commit imutável; não copie decisões normativas. Issues e [Project #1](https://github.com/orgs/Plan2Space/projects/1) guardam o estado operacional.

Próximos passos: integrar descoberta/carregamento versionados em [agent-runtime#50](https://github.com/Plan2Space/agent-runtime/issues/50) depois do hub #29; então registrar rastreabilidade e reconciliar o Board em [agent-runtime#51](https://github.com/Plan2Space/agent-runtime/issues/51). Não altere permissões, proteção de branch ou políticas de aprovação sem autorização.
