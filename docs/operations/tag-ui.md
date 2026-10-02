# Administração de planos de leitura OPC

A interface é servida pelo control plane existente em `http://127.0.0.1:8081/ui`.
Use o launcher `opc-bridge-server` com PostgreSQL, TLS do listener de agentes e
`ADMIN_API_TOKEN` configurados conforme a documentação operacional do central.
O listener HTTP administrativo continua fixo em `127.0.0.1`; `ADMIN_API_PORT`
tem padrão 8081. Nenhum componente web externo, Node, npm ou CDN é necessário.

Abra `/ui` e informe o token administrativo. A página inicial é uma estrutura
estática de login, retornada com HTTP 401 enquanto não há Bearer válido. Os
arquivos CSS/JS estáticos não contêm dados operacionais. Os dados e operações
exigem `Authorization: Bearer <ADMIN_API_TOKEN>`. O token fica apenas na memória
da página, é apagado ao sair e não é colocado em URL, cookies ou storage do
navegador. Feche a aba ao terminar. Não use equipamento compartilhado para
guardar o token. Respostas usam `no-store`; a UI usa CSP restrita e renderiza os
campos operacionais como texto.

Para acesso de outra máquina, crie um túnel autorizado até o AUTOU3:

```sh
ssh -N -L 8081:127.0.0.1:8081 usuario@AUTOU3
```

Abra `http://127.0.0.1:8081/ui` na máquina que mantém esse túnel. Não publique a
porta HTTP administrativa na rede. Esta entrega não altera SSH, firewall,
certificados ou serviços.

## Seleção de servidor e validação

1. Selecione um agente cadastrado. A tela mostra estado, sessão, heartbeat e a
   configuração aplicada sanitizada, separada do plano ainda em edição.
2. Use **Consultar servidores** para selecionar um ProgID anunciado pelo agente,
   ou informe manualmente o endereço/ProgID, por exemplo
   `ABB.AfwOpcDaSurrogate.1`. A descoberta consulta o registro existente; não
   registra COM nem garante que cada ProgID possa conectar.
3. Informe intervalo inteiro de 1000 a 60000 ms e de 1 a 50 endereços únicos,
   por exemplo `Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN`. IDs internos são
   atribuídos pelo backend: caminhos ordenados lexicograficamente recebem IDs
   1..N. O mesmo conjunto tem os mesmos IDs independentemente da ordem digitada.
4. **Validar** verifica uma tag; **Validar lista** verifica o plano completo.
   O agente precisa estar conectado e anunciar `opc-inspection-v1`.

A inspeção usa outro adapter/worker temporário, com a mesma seleção de
arquitetura aprovada para o agente. O wrapper continua `OPC.Automation`, seguido
de `Connect(prog_id)`. O worker conecta, cria um grupo próprio e tenta AddItem
para cada tag. Não lê valores, não escreve OPC, não envia CONFIG_PUSH, não
interrompe nem reconfigura o worker ativo. Objetos COM ficam nesse worker; o IPC
transporta identidades e resultados primitivos. Há uma inspeção por agente,
limite de 50 tags, prazo do central de 45 segundos, orçamento de 30 segundos no
inspector e deadlines próprios do worker temporário. Um timeout de AddItem
interrompe novas tentativas desse lote. A limpeza libera a conexão temporária.
Conectar um segundo cliente depende da capacidade/licenciamento do servidor OPC;
isso precisa ser validado no Windows em uma etapa autorizada.

Cada resultado é `valid`, `invalid` ou `error`, com HRESULT numérico quando
disponível; mensagens internas de provider, valores de processo e segredos não
são retornados. Um ProgID que não conecta produz erros individuais sem aprovar
o plano. A descoberta e a validação são capacidades novas: agentes anteriores
retornam `inspection_unsupported`, sem tentativa de usar CONFIG_PUSH como teste.
Será necessário atualizar o pacote do agente em etapa separada antes de usar
essas funções no PIMS; esta entrega não gera nem instala pacote Windows.

## Aplicação e histórico

Depois de **Validar lista** com todas as tags válidas, **Aplicar configuração**
pede confirmação explícita. A aprovação vale por cinco minutos, uma única vez,
e é vinculada ao agente, sessão, versão aplicada e conteúdo exato do plano.
Alterar qualquer campo, reconectar, reiniciar o central ou mudar a configuração
aplicada exige nova validação. Validar apenas uma tag não habilita a aplicação
da lista. A confirmação também é exigida pelo backend (`confirmed: true`).

A aplicação reutiliza snapshots, operações, auditoria e o despacho CONFIG_PUSH
existentes. O histórico é atualizado a cada cinco segundos e mostra
`pending`, `applied`, `rejected`, `expired` (ou `failed` em falha de transporte).
Falha no estágio de conexão/grupo/itens mantém a configuração anterior ativa.
Uma validação bem-sucedida não garante que o servidor OPC continue disponível
até a aplicação. O modo HTTP standalone sem BridgeServer é somente leitura;
a UI indica essa condição e as rotas de inspeção/aplicação retornam 405.

Escrita OPC está indisponível. Não há botão, campo, endpoint ou mensagem de
escrita OPC. PI Web API e PI Point não fazem parte desta entrega.

## Endpoints e auditoria

Todos os endpoints abaixo exigem Bearer e usam os repositórios do control plane:

| Método / rota | Conteúdo |
| --- | --- |
| GET `/api/v1/ui-capabilities` | Disponibilidade do runtime com despacho |
| GET `/api/v1/agents/{agent_id}/active-config` | Versão, ProgID, intervalo e caminhos, sem snapshot bruto |
| GET `/api/v1/agents/{agent_id}/opc-servers` | ProgIDs descobertos por inspeção isolada |
| POST `/api/v1/agents/{agent_id}/tag-validations` | Validação de um plano e aprovação opcional |
| POST `/api/v1/agents/{agent_id}/tag-config-operations` | Aplicação confirmada do plano validado |
| GET `/api/v1/config-operations?agent_id=...` | Histórico e status do fluxo existente |
| GET `/api/v1/config-operations/{operation_id}` | Status individual sanitizado |

Payload de validação (o corpo JSON é limitado a 64 KiB):

```json
{
  "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
  "update_rate_ms": 5000,
  "tags": ["Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN"]
}
```

Na aplicação, envie o mesmo plano e acrescente `validation_id` recebido na
validação completa e `confirmed: true`. Campos extras, IDs/versões do cliente,
duplicatas e tipos inválidos são recusados. Sem aprovação atual: 409; sem
confirmação: 400; agente inexistente: 404; timeout: 504; falta de Bearer: 401.
O despacho verifica novamente sessão e versão aplicada no event loop do
BridgeServer, para recusar uma mudança de estado ocorrida durante a solicitação.
O endpoint legado `POST .../config-operations` mantém seu contrato de API para
clientes administrativos existentes; a UI usa exclusivamente a rota confirmada
`tag-config-operations`.

Os eventos `inspection.tags` e `inspection.servers` registram contagem e
resultado seguro. Aplicações reutilizam `config.requested` e seus resultados
`config.applied/rejected/expired`. Nenhum evento registra token ou valor OPC.
Solicitações originadas na UI registram `ui_confirmed: true` na auditoria.
O histórico continua sujeito à retenção operacional existente; backups do
PostgreSQL continuam necessários.

## Verificação simulada

Os testes usam SQLite isolado e objetos OPC/COM falsos, inclusive objetos que
recusam pickle. Não representam validação COM/OPC em Windows industrial.

```sh
PYTHONPATH=src python -m pytest -q tests/test_tag_ui.py tests/test_admin_api.py tests/test_worker_ipc.py
```

Há também um smoke test opcional com Firefox/geckodriver já instalados. Ele usa
somente listeners locais temporários, SQLite e OPC simulado, não instala
dependências e confirma login, seleção de ProgID, validação, cancelamento e
aceitação da confirmação, histórico e logout:

```sh
OPC_BRIDGE_TEST_BROWSER=1 PYTHONPATH=src python -m pytest -q tests/test_tag_ui_browser.py
```
