# Guia de Configuração e Operação da Integração PI (OSIsoft / AVEVA PI Web API)

Este documento descreve a configuração externa, diretrizes de segurança industrial e operação do canal de publicação **OPC → PI** no central plane do OPC-Bridge.

---

## 1. Princípios e Diretrizes de Segurança Industrial

1. **Sentido Unidirecional Obrigatório:**
   - O fluxo de dados é estritamente unidirecional:
     $$\text{Tag OPC ativa em cache} \longrightarrow \text{Publicação no PI Point configurado}$$
   - **Zero escrita em OPC:** O módulo jamais realiza operações de escrita OPC DA, chamadas COM de escrita ou disparo de `CONFIG_PUSH` para agentes. A coleta OPC permanece estritamente em leitura.
   - **Zero sobrecarga de polling:** Leituras são consumidas exclusivamente do cache volátil em memória mantido pelo servidor central (`get_live_values`).

2. **Chave Geral de Segurança (*Kill Switch*):**
   - A variável `OPC_BRIDGE_PI_OUTPUT_ENABLED` possui valor padrão obrigatório `false`.
   - Se o valor não for estritamente `"true"`, toda e qualquer tentativa de chamada HTTP/HTTPS para o PI é sumariamente bloqueada antes da abertura de qualquer socket.
   - Sob bloqueio do kill switch, registra-se apenas a mensagem segura: `Saída PI desabilitada`.

3. **Isolamento de Segredos e Credenciais:**
   - Nenhuma URL do PI, usuário, senha, token ou certificado é versionado no Git ou exibido na interface Web administrativa (`/ui`).
   - Todas as mensagens de erro, logs e eventos de auditoria passam por sanitização regex automática para remoção de tokens Bearer, senhas e credenciais embutidas em URLs.

4. **Padrões Abertos e Zero Dependências Proprietárias:**
   - Toda a comunicação é baseada no padrão REST da **PI Web API** via HTTPS, utilizando apenas a biblioteca padrão Python (`urllib.request`, `ssl`, `json`). Não são utilizados SDKs proprietários (ex: AF SDK, PI SDK).

5. **Isolamento de Falhas e *Backoff*:**
   - Falhas transitórias no PI Web API (ex: timeout, 503, erro de rede) não afetam a coleta OPC, a interface de usuário ou outros mapeamentos.
   - Aplica-se *backoff* exponencial por mapeamento (limitado a 5 minutos) com incremento do contador `failure_count`.

---

## 2. Parâmetros de Configuração Externa (Ambiente)

As seguintes variáveis de ambiente devem ser configuradas externamente (fora do repositório Git), por exemplo em `/etc/opc-bridge/pi.env` no host de produção:

| Variável | Tipo | Padrão | Descrição |
| :--- | :--- | :--- | :--- |
| `OPC_BRIDGE_PI_OUTPUT_ENABLED` | Booleano | `false` | **Kill switch mandatório.** Apenas `"true"` habilita chamadas de rede reais ao PI. |
| `OPC_BRIDGE_PI_OUTPUT_MODE` | String | `simulated` | Modo de operação: `simulated` (seguro/testes) ou `web_api` (PI Web API real). |
| `OPC_BRIDGE_PI_WEB_API_URL` | String | `""` | URL base do PI Web API (ex: `https://piserver.corp.local/piwebapi`). |
| `OPC_BRIDGE_PI_WEB_API_AUTH_TYPE` | String | `basic` | Tipo de autenticação: `basic`, `bearer` ou `anonymous`. |
| `OPC_BRIDGE_PI_WEB_API_USERNAME` | String | `""` | Nome de usuário de serviço para Basic Auth. |
| `OPC_BRIDGE_PI_WEB_API_PASSWORD` | String | `""` | Senha de serviço para Basic Auth. |
| `OPC_BRIDGE_PI_WEB_API_BEARER_TOKEN` | String | `""` | Token para autenticação Bearer. |
| `OPC_BRIDGE_PI_WEB_API_TIMEOUT_SECONDS` | Float | `10.0` | Timeout máximo em segundos por chamada HTTP (mínimo 1.0s). |
| `OPC_BRIDGE_PI_WEB_API_CA_BUNDLE` | String | `""` | Caminho do arquivo de certificados CA customizados (PEM/CRT). |
| `OPC_BRIDGE_PI_WEB_API_VERIFY_SSL` | Booleano | `true` | Se `true`, valida certificados TLS. Usar `false` apenas em homologação restrita. |

---

## 3. Exemplo de Arquivo de Ambiente Seguro (`/etc/opc-bridge/pi.env`)

No servidor de produção (fora do diretório versionado):

```bash
# Permissões recomendadas: chmod 600 /etc/opc-bridge/pi.env
# Pertencente ao usuário de execução do serviço central

OPC_BRIDGE_PI_OUTPUT_ENABLED=true
OPC_BRIDGE_PI_OUTPUT_MODE=web_api
OPC_BRIDGE_PI_WEB_API_URL=https://piwebapi.empresa.local/piwebapi
OPC_BRIDGE_PI_WEB_API_AUTH_TYPE=basic
OPC_BRIDGE_PI_WEB_API_USERNAME=svc_opcbridge_pi
OPC_BRIDGE_PI_WEB_API_PASSWORD=SegredoForteAqui123!
OPC_BRIDGE_PI_WEB_API_TIMEOUT_SECONDS=10.0
OPC_BRIDGE_PI_WEB_API_CA_BUNDLE=/etc/ssl/certs/ca-corporativa.crt
OPC_BRIDGE_PI_WEB_API_VERIFY_SSL=true
```

No systemd service (`opc-bridge-server.service`):

```ini
[Service]
EnvironmentFile=-/etc/opc-bridge/pi.env
```

---

## 4. Regras de Publicação de Mapeamentos

Para que uma leitura seja elegível para envio ao PI:
1. O mapeamento individual deve estar **Ativo** (`enabled = true`).
2. A tag OPC deve possuir leitura recente no cache em memória.
3. O valor coletado não pode ser nulo (`None`).
4. A qualidade OPC deve ser **Boa** (`quality >= 192`). Leituras com qualidade ruim (*Bad*) são bloqueadas.
5. O dado não pode estar desatualizado (*stale*).
6. O intervalo de publicação configurado (`publish_interval_ms`) deve ter decorrido desde o último envio (exceto na ação manual pontual *Publicar uma vez*).
7. O intervalo de publicação deve ser maior ou igual à velocidade de coleta OPC (`update_rate_ms`), garantindo que não sejam enviadas replicações sem dados novos.

---

## 5. Operação na Interface Administrativa (`/ui`)

Na aba **Integração PI**:

- **Banner de Modo de Saída:**
  - `Saída PI: Simulação — nenhuma escrita real habilitada.` quando o kill switch estiver desabilitado (`false`).
  - `Saída PI habilitada` quando `OPC_BRIDGE_PI_OUTPUT_ENABLED=true`.

- **Testar Conexão PI (`btn-test-pi-connection`):**
  - Executa uma chamada segura e de leitura exclusiva ao endpoint de status do PI Web API (`/system/landing`).
  - Jamais grava valores em streams ou tags PI.
  - Informa o status de comunicação e versão do serviço sem vazar credenciais.

- **Publicar uma vez (`btn-publish-once`):**
  - Abre modal de confirmação exibindo:
    - Nome do PI Point de destino
    - Tag OPC de origem
    - Valor atual em cache
    - Qualidade da leitura
    - Timestamp da leitura
  - Se a leitura estiver ausente, com qualidade ruim ou desatualizada (*stale*), a interface exibe um aviso em destaque e bloqueia o botão de confirmação.
  - Ao confirmar, dispara a publicação imediata e registra evento de auditoria rastreável (`pi_mapping.publish_once`).
