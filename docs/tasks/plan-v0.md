# Plano OPC-Bridge v0 — Prova de Conceito ABB.AfwOpcDaSurrogate.1

## Objetivo
Servidor central Python + agente Windows instalável offline para leitura OPC DA Device sob comando remoto, validado no servidor ABB alvo.

## Contratos definidos
- `docs/contracts/protocol.md` — framing, mensagens, estruturas ItemRef/ItemResult.
- `docs/contracts/opc-adapter.md` — interface do adaptador COM, isolamento, regras Device.

## Tarefas

### T1 — Estrutura base do projeto ✅ CONCLUÍDA (simulado/Linux)
- **Responsável:** Claude (execução direta devido a falhas no despacho Codex).
- **Commit:** d571ef8 (dev/codex).
- **Evidência:** Layout criado, pyproject.toml, README atualizado. Imports funcionam em Linux; Windows não validado.

### T2 — Implementação do protocolo ✅ CONCLUÍDA (simulado/Linux)
- **Responsável:** Claude.
- **Commit:** 7d6c633 (dev/codex).
- **Evidência:** 39 testes de round-trip passando; todos os tipos de mensagem cobertos. Robustez adicional (fragmentação, concatenação, malformed) em 9ea4bbb.

### T3 — Servidor central ✅ CONCLUÍDA (simulado/Linux)
- **Responsável:** Claude.
- **Commit:** c619fbd (dev/codex).
- **Evidência:** BridgeServer com TLS, handshake, CONFIG_PUSH; 7 testes de integração passando.

### T4 — Adaptador OPC DA isolado (Antigravity / Windows)
- **Dependências:** T1.
- **Escopo:** implementação do contrato `opc-adapter.md` usando `win32com` ou equivalente; processo COM separado supervisionado pelo serviço; suporte a ABB.AfwOpcDaSurrogate.1.
- **Critério de aceite:** leitura Device real do item `Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN` retorna valor/tipo/qualidade/timestamp; log de duração; commit na branch `dev/antigravity`.
- **Ambiente:** Windows com ABB OPC DA instalado. Worktree Linux existente não valida esta tarefa.
- **Bloqueio atual:** Antigravity requer execução em Windows. Sem ambiente Windows remoto registrado no Orca, esta tarefa não pode ser despachada automaticamente.

### T5 — Comunicação Agente-Servidor ✅ CONCLUÍDA (simulado/Linux)
- **Responsável:** Claude.
- **Commit:** 0d18ae4 (dev/codex).
- **Evidência:** AgentClient com TLS/handshake/config/read; 5 testes de integração (normal, erro parcial, lenta, reconexão, no-cache). Supervisão adicionada em 86a3a0b.
- **Nota:** Implementação validada com adaptador simulado em Linux. Serviço Windows real e build offline pendentes de ambiente Windows.

### T6 — Agendamento Central ✅ CONCLUÍDA (simulado/Linux)
- **Responsável:** Codex Luna low (dispatch ctx_acf1b5c71c23).
- **Commit:** 7ab89ea (dev/codex).
- **Evidência:** ReadSchedulerMetrics, ciclos correlacionados por request_id, timeout com descarte de overrun, cleanup em disconnect. 89 testes passando.
- **Nota:** Lógica de agendamento validada em Linux. Integrador Windows pendente.

### T7 — Supervisão do Agente ✅ CONCLUÍDA (simulado/Linux)
- **Responsável:** Codex Luna low (dispatch ctx_79224ca91971).
- **Commit:** 86a3a0b (dev/codex).
- **Evidência:** Supervisor com heartbeat timeout, backoff exponencial, watchdog OPC, logging estruturado. 93 testes passando.
- **Nota:** Módulo de supervisão validado em Linux. Serviço Windows e recuperação de processo COM pendentes de ambiente real.

### T8 — Instalador offline ⏸️ PENDENTE (Windows real necessário)
- **Dependências:** T5 concluída em Windows.
- **Bloqueio:** Requer build Windows, criação de serviço Windows e empacotamento offline.

### T9 — Validação de capacidade ⏸️ PENDENTE (ABB real necessário)
- **Dependências:** T4 (adaptador OPC DA real) e T5 (agente Windows).
- **Bloqueio:** Requer servidor ABB.AfwOpcDaSurrogate.1 acessível e ambiente Windows industrial.

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