# Instruções do projeto OPC-Bridge

## Escopo
Leia docs/requirements.md antes de trabalhar.
Altere somente os arquivos necessários à tarefa recebida.
Não altere projetos vizinhos nem configurações globais.
Não execute testes contra OPC de produção sem autorização específica.
Não armazene credenciais ou certificados privados no Git.
Não inclua emojis em código.

## Organização
- src/opc_bridge/server: servidor central e agendamento.
- src/opc_bridge/agent: comunicação, supervisão e serviço Windows.
- src/opc_bridge/protocol: contratos compartilhados.
- src/opc_bridge/adapters: acesso OPC.
- packaging/windows: empacotamento e instalador offline.
- docs/tasks: tarefas, evidências e entregas.

## Agentes
- Claude Code coordena, resolve decisões complexas e revisa integrações.
- Codex usa exclusivamente o modelo Luna com esforço Low.
- Codex recebe tarefas pequenas com arquivos e critérios delimitados.
- Antigravity executa no Windows.
- No AUTOU3, não iniciar Antigravity como processo Linux.
- Cada worker trabalha na sua worktree e branch.
- Workers não fazem merge na main nem alteram arquivos de outro worker.
- Contratos compartilhados precisam ser definidos antes das implementações.
- Ler as skills workload-router e antigravity-autou3 existentes,
  quando disponíveis.
- Preservar placement e política de cota dessas skills.
- Não modificar as skills globais para configurar este projeto.
- Se uma regra existente conflitar com os papéis deste projeto,
  registrar o conflito antes de despachar a tarefa.

## Evidência
Distinguir testes simulados, testes Windows e testes OPC reais.
Não apresentar teste Linux ou simulado como validação COM/OPC.
Não anunciar instalador pronto sem instalar e testar no Windows.

## Entrega de cada tarefa
Informar:
1. Resumo.
2. Arquivos alterados.
3. Verificações executadas e resultados.
4. Limitações ou bloqueios.
5. Branch e commit.
