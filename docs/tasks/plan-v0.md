# Plano OPC-Bridge v0 — Prova de Conceito ABB.AfwOpcDaSurrogate.1

## Objetivo
Servidor central Python + agente Windows instalável offline para leitura OPC DA Device sob comando remoto, validado no servidor ABB alvo.

## Contratos definidos
- `docs/contracts/protocol.md` — framing, mensagens, estruturas ItemRef/ItemResult.
- `docs/contracts/opc-adapter.md` — interface do adaptador COM, isolamento, regras Device.

## Tarefas

### T1 — Estrutura base do projeto (Codex / Luna / low)
- **Dependências:** nenhuma.
- **Escopo:** criar layout `src/opc_bridge/{server,agent,protocol,adapters}`, `packaging/windows`, `tests/`; `pyproject.toml` mínimo; README atualizado.
- **Critério de aceite:** imports funcionam em Linux e Windows; lint básico passa; commit na branch `dev/codex`.
- **Worktree:** `/home/felipe/dev/OPC-Bridge-worktrees/codex`.

### T2 — Implementação do protocolo (Codex / Luna / low)
- **Dependências:** T1.
- **Escopo:** módulo `protocol` com framing, serialização/deserialização, CRC32C, testes unitários de round-trip para todas as mensagens definidas em `protocol.md`.
- **Critério de aceite:** 100% dos tipos de mensagem cobertos por teste; nenhum aviso de tipo; commit na branch `dev/codex`.
- **Worktree:** `/home/felipe/dev/OPC-Bridge-worktrees/codex`.

### T3 — Servidor central: skeleton + config push (Codex / Luna / low)
- **Dependências:** T2.
- **Escopo:** servidor TCP/TLS que aceita HELLO/AUTH, envia CONFIG_PUSH, recebe READ_RESPONSE; logs estruturados; configuração versionada em memória.
- **Critério de aceite:** servidor inicia, aceita conexão TLS mock, troca handshake completo; teste de integração local; commit na branch `dev/codex`.
- **Worktree:** `/home/felipe/dev/OPC-Bridge-worktrees/codex`.

### T4 — Adaptador OPC DA isolado (Antigravity / Windows)
- **Dependências:** T1.
- **Escopo:** implementação do contrato `opc-adapter.md` usando `win32com` ou equivalente; processo COM separado supervisionado pelo serviço; suporte a ABB.AfwOpcDaSurrogate.1.
- **Critério de aceite:** leitura Device real do item `Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN` retorna valor/tipo/qualidade/timestamp; log de duração; commit na branch `dev/antigravity`.
- **Ambiente:** Windows com ABB OPC DA instalado. Worktree Linux existente não valida esta tarefa.
- **Bloqueio atual:** Antigravity requer execução em Windows. Sem ambiente Windows remoto registrado no Orca, esta tarefa não pode ser despachada automaticamente.

### T5 — Agente Windows: serviço + comunicação (Antigravity / Windows)
- **Dependências:** T2, T4.
- **Escopo:** serviço Windows que inicia conexão TLS ao servidor, aplica CONFIG_PUSH, executa READ_REQUEST via adaptador isolado, envia READ_RESPONSE; heartbeat; logs com rotação.
- **Critério de aceite:** serviço instala e inicia automaticamente; conecta ao servidor central; responde a um ciclo de leitura Device; commit na branch `dev/antigravity`.
- **Ambiente:** Windows. Mesmo bloqueio de T4.

### T6 — Instalador offline (Antigravity / Windows)
- **Dependências:** T5.
- **Escopo:** empacotar runtime Python, dependências, binário do serviço e dados de conexão inicial; instalador silencioso; compatível com Windows 7 SP1+.
- **Critério de aceite:** instalador executa em máquina limpa; serviço sobe após reboot; sem internet necessária.
- **Bloqueio:** depende de T5 e de ambiente Windows real.

### T7 — Validação de capacidade (Antigravity / Windows)
- **Dependências:** T5.
- **Escopo:** teste de carga com até 3.000 tags/s contra ABB real; relatório de throughput, latência e overruns.
- **Critério de aceite:** relatório com evidência; meta atingida ou limitação documentada com causa raiz.
- **Bloqueio:** depende de T5 e de ambiente Windows real.

## Estado da orquestração
- **Run:** `run_da9310dfe0e9` criado e vinculado a este terminal.
- **Worktrees registradas no Orca:** apenas `main`. As worktrees Git `claude`, `codex`, `antigravity` existem no disco mas não estão ativas como workspaces Orca independentes (o Orca as reconhece via `worktree show --worktree path:...`).
- **Ambientes remotos:** nenhum ambiente Windows registrado. Antigravity não pode ser despachado sem host Windows conectado ao Orca.
- **Skills indisponíveis:** `workload-router` e `antigravity-autou3` não existem no catálogo do Orca CLI.

## Próximos passos imediatos
1. Despachar T1 para Codex na worktree `dev/codex` (ambiente Linux disponível).
2. Resolver o placement do Antigravity: conectar host Windows ao Orca ou definir fluxo manual para T4/T5/T6/T7.
3. Após T1 concluída, despachar T2 e T3 sequencialmente para Codex.
4. T4 só inicia quando houver ambiente Windows validado.

## Regras de coordenação
- Não declarar T4-T7 completas sem evidência Windows real.
- Revisar cada commit antes de integrar à main.
- Preservar contratos; alterações exigem atualização da documentação e revalidação.
- Workers não fazem merge na main nem alteram arquivos de outro worker.