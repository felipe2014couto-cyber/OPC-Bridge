# OPC-Bridge

Servidor central e agente Windows em Python para leitura OPC DA local
sob comando remoto.

A configuração operacional é administrada exclusivamente no servidor:
seleção do OPC, endereços dos itens e intervalos de coleta.

## Documentação

- [AGENTS.md](AGENTS.md) — instruções para agentes e organização do projeto.
- [docs/requirements.md](docs/requirements.md) — requisitos funcionais e não funcionais.
- [docs/contracts/protocol.md](docs/contracts/protocol.md) — contrato do protocolo de comunicação.
- [docs/contracts/opc-adapter.md](docs/contracts/opc-adapter.md) — contrato do adaptador OPC DA.
- [docs/tasks/plan-v0.md](docs/tasks/plan-v0.md) — plano de tarefas da prova de conceito.
- [docs/operations/autou3-linux-server.md](docs/operations/autou3-linux-server.md) — execução central no AUTOU3 Linux.
- [docs/operations/agent-credentials.md](docs/operations/agent-credentials.md) — provisionamento seguro de credenciais individuais.
- [docs/operations/tag-ui.md](docs/operations/tag-ui.md) — interface local para seleção de servidor OPC, validação isolada e aplicação confirmada de tags.

## Estrutura

```
src/opc_bridge/
  server/     # servidor central e agendamento
  agent/      # comunicação, supervisão e serviço Windows
  protocol/   # contratos compartilhados
  adapters/   # acesso OPC DA
packaging/windows/  # empacotamento e instalador offline
tests/              # testes unitários e de integração
```

## Desenvolvimento

```bash
pip install -e ".[dev]"
pytest
ruff check src/ tests/
```

Consulte AGENTS.md e docs/requirements.md antes de desenvolver.
