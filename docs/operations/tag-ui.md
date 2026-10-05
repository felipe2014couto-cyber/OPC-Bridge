# Administração de Equipamentos, Planos de Leitura OPC e Interface Web

A interface web administrativa do control plane do OPC-Bridge é servida pelo daemon interno do central em `http://127.0.0.1:8081/ui` e disponibilizada externamente de forma segura via **proxy reverso HTTPS com autenticação HTTP Basic no Nginx**.

Nenhum componente web externo, Node.js, npm ou dependência de CDN é utilizado. A interface é totalmente autocontida e estruturada em dois módulos operacionais principais: **Equipamentos** e **OPC**.

---

## 1. Arquitetura de Acesso e Proxy Reverso HTTPS

O servidor central mantém a API administrativa interna privada no loopback (`127.0.0.1:8081`), acessível apenas localmente ou através do proxy reverso configurado no Nginx.

* **URL externa oficial:** `https://10.247.168.43:8081/ui`
* **Porta exposta na rede:** `10.247.168.43:8081` (exclusivamente HTTPS/TLS)
* **Acesso de rede:** Acesso liberado para qualquer estação na rede corporativa roteável (sem ACL fixa por IP).
* **Autenticação obrigatória:** HTTP Basic Authentication gerenciada integralmente pelo Nginx para todas as rotas e caminhos, utilizando o arquivo de credenciais `/etc/opc-bridge/nginx-admin.htpasswd`. Qualquer tentativa de acesso sem autenticação válida é rejeitada pelo Nginx antes de atingir o backend.
* **Injeção de Bearer Token:** O Nginx sobrescreve o cabeçalho `Authorization` do cliente e injeta o cabeçalho técnico `Authorization: Bearer <ADMIN_API_TOKEN>` ao encaminhar a requisição para o backend WSGI interno (`127.0.0.1:8081`), garantindo que o token administrativo permaneça protegido no servidor.
* **Segurança no navegador:** O navegador **não recebe, não solicita, não armazena (sem uso de `localStorage` ou `sessionStorage`), não imprime e não envia** o token `ADMIN_API_TOKEN`. As requisições executadas pelo JavaScript da UI utilizam caminhos relativos same-origin (`/api/v1/...`) sem cabeçalho `Authorization` nem credenciais no código-fonte.
* **Proteção contra exposição direta:** A exposição direta da porta HTTP interna sem criptografia ou em bind amplo (`0.0.0.0` sem TLS) é estritamente proibida.

---

## 2. Configuração Operacional do Nginx e Gerenciamento de Usuários (AUTOU3)

O template de configuração do Nginx é versionado em [`deploy/nginx/opc-bridge-admin-ui.conf.example`](file:///C:/OPCBridge-build/deploy/nginx/opc-bridge-admin-ui.conf.example).

### 2.1. Arquivo de credenciais Basic Auth (`nginx-admin.htpasswd`)

As credenciais de acesso de administradores e operadores são gerenciadas via Nginx com hash bcrypt (`-B`). Não há tela nem endpoint na interface para cadastro de usuários, evitando superfícies de ataque adicionais.

#### Criação inicial do arquivo htpasswd:
```sh
sudo htpasswd -c -B /etc/opc-bridge/nginx-admin.htpasswd admin
sudo chown root:root /etc/opc-bridge/nginx-admin.htpasswd
sudo chmod 600 /etc/opc-bridge/nginx-admin.htpasswd
```

#### Adicionar novos usuários individualmente (sem sobrescrever existentes):
Para adicionar um novo operador ou engenheiro preservando os usuários já existentes, execute o utilitário `htpasswd` **sem a flag `-c`**:
```sh
sudo htpasswd -B /etc/opc-bridge/nginx-admin.htpasswd NOME_DO_USUARIO
```
O utilitário solicitará e confirmará a senha interativamente, gravando a entrada com hash bcrypt seguro.

### 2.2. Arquivo de injeção de token técnico (`nginx-api-token.conf`)

O `ADMIN_API_TOKEN` técnico nunca deve ser versionado no Git nem inserido no template público. No AUTOU3, crie manualmente o arquivo protegido lido exclusivamente pelo processo do Nginx:

```sh
sudo bash -c 'cat << "EOF" > /etc/opc-bridge/nginx-api-token.conf
proxy_set_header Authorization "Bearer <VALOR_DO_ADMIN_API_TOKEN_REAL>";
EOF'
sudo chown root:root /etc/opc-bridge/nginx-api-token.conf
sudo chmod 600 /etc/opc-bridge/nginx-api-token.conf
```

### 2.3. Instalação e validação do Nginx

```sh
# Copiar o template para a pasta de sites disponíveis do Nginx
sudo cp deploy/nginx/opc-bridge-admin-ui.conf.example /etc/nginx/sites-available/opc-bridge-admin-ui.conf
sudo ln -sf /etc/nginx/sites-available/opc-bridge-admin-ui.conf /etc/nginx/sites-enabled/

# Validar a sintaxe da configuração antes de aplicar
sudo nginx -t

# Recarregar o serviço Nginx
sudo systemctl reload nginx
```

---

## 3. Módulo "Equipamentos"

A aba **Equipamentos** centraliza a gestão cadastral das máquinas industriais monitoradas pelo sistema:

* **Listagem de equipamentos:** Exibe nome, IP da máquina, agente OPC-Bridge associado, situação da conexão do agente e quantidade de configurações OPC vinculadas.
* **Situação do agente:**
  * **Conectado (verde):** Agente vinculado e com sessão TLS ativa e heartbeats regulares.
  * **Desconectado:** Agente vinculado, mas sem sessão ativa no momento.
  * **Não associado:** Equipamento cadastrado sem agente vinculado.
* **Validação de endereço IP:** Aceita exclusivamente endereços IPv4 ou IPv6 válidos (ex: `10.247.168.43` ou `2001:db8::1`), validados pelo backend via biblioteca padrão `ipaddress`.
* **Natureza estritamente administrativa:** O cadastro de equipamento registra apenas metadados no banco PostgreSQL/SQLite. **Em nenhuma hipótese** o servidor central tenta abrir DCOM, OPC, SSH, Telnet ou qualquer socket direto ao endereço IP informado.
* **Proteção contra exclusão acidental:** A exclusão de qualquer equipamento exige confirmação. Caso o equipamento possua configurações OPC vinculadas, o sistema bloqueia a exclusão simples e exige confirmação detalhada listando expressamente os nomes de todas as configurações afetadas.

---

## 4. Módulo "OPC"

A aba **OPC** gerencia a definição de planos de leitura, descoberta de servidores e acompanhamento de dados ao vivo:

### 4.1. Seleção e Vínculo de Equipamento
* O usuário seleciona o equipamento cadastrado desejado.
* Para realizar descoberta de servidores, validação de tags ou aplicação ativa, o equipamento selecionado precisa estar associado a um agente com sessão conectada.

### 4.2. Descoberta de Servidores OPC
* O botão **Buscar servidores OPC** utiliza estritamente a capability remota `opc-inspection-v1` já existente no agente conectado.
* A inspeção instancia um worker isolado no agente para consultar os ProgIDs disponíveis localmente.
* **Não altera a configuração ativa** e **não dispara `CONFIG_PUSH`**.
* O operador pode selecionar um ProgID descoberto ou optar por "Informar ProgID manualmente".

### 4.3. Tabela Dinâmica de Endereços OPC (Tags)
* Suporte a até 50 tags únicas por configuração.
* Intervalo de leitura configurável entre 1.000 ms e 60.000 ms.
* Colunas da listagem:
  1. **Endereço OPC (Tag)**
  2. **Último valor**
  3. **Tipo** (ex: `I32`, `F64`, `BOOL`, `STRING`)
  4. **Qualidade** (ex: `Good (192)` em verde, `Bad (0)` em vermelho)
  5. **Último timestamp** (data/hora proveniente do servidor OPC em formato `dd/MM/yyyy HH:mm:ss.SSS`)
  6. **Idade** (tempo decorrido desde o recebimento no servidor central, diferenciando o timestamp de leitura na origem da chegada no central)
  7. **Ação Excluir** (remoção individual da tag da lista em edição)

### 4.4. Acompanhamento ao Vivo (Live Values)
* Os botões **Iniciar acompanhamento ao vivo** e **Parar acompanhamento ao vivo** consultam o endpoint `/api/v1/agents/{agent_id}/live-values` a cada 1 segundo (1000 ms).
* **Valores efêmeros em memória volátil:** Os dados exibidos provêm exclusivamente do cache em memória dos ciclos periódicos normais já configurados. **Nenhuma leitura OPC adicional é gerada**.
* **Destaques visuais:**
  * Verde para qualidade Good (`>= 192`) e operação normal.
  * Âmbar para valores desatualizados (*stale*, quando a idade supera o TTL de `max(3 * update_rate_ms, 15000)` ms).
  * Vermelho para erro de leitura ou qualidade ruim.
  * Traço (`—`) e indicador neutro para itens aguardando primeira leitura.
* **Interrupção automática de polling:** O acompanhamento ao vivo é interrompido imediatamente ao trocar de equipamento, carregar outra configuração, editar/adicionar/remover tags, trocar de aba, ocultar/fechar a página (`visibilitychange`, `pagehide`, `beforeunload`) ou caso o agente se desconecte.

### 4.5. Configurações Salvas vs. Aplicação no Agente
* **Salvar configuração:**
  * Persiste a definição administrativa nomeada no PostgreSQL (`named_opc_configs`).
  * Contém: nome da configuração, equipamento, agente, ProgID OPC, intervalo e lista de tags.
  * **Não aplica e não envia `CONFIG_PUSH` ao agente**.
  * Permite listar, carregar, editar e excluir configurações por equipamento de forma segura.
* **Aplicar no agente:**
  * Ação separada, explícita e protegida.
  * Exige que o equipamento possua agente conectado e plano validado.
  * Exibe diálogo de confirmação detalhado (`window.confirm`) alertando que a configuração ativa de leitura do agente será imediatamente substituída.
  * Dispara `CONFIG_PUSH` versionado e registra operação no histórico e trilha de auditoria.

---

## 5. Instalação da CA Interna no Windows Local

Para que a conexão HTTPS em `https://10.247.168.43:8081/ui` seja reconhecida como confiável pelo navegador no Windows sem alertas de segurança TLS:

1. Obtenha o certificado da autoridade certificadora interna do servidor central (`ca.crt` ou `ca.pem`).
2. No Windows local:
   * Pressione `Win + R`, digite `certmgr.msc` e tecle Enter;
   * Navegue até **Autoridades de Certificação Raiz Confiáveis** -> **Certificados**;
   * Clique com o botão direito -> **Todas as tarefas** -> **Importar...**;
   * Selecione o arquivo `ca.crt` / `ca.pem` e confirme a importação no repositório de Raízes Confiáveis;
   * Alternativamente, via PowerShell administrativo:
     ```powershell
     Import-Certificate -FilePath "C:\caminho\para\ca.pem" -CertStoreLocation "Cert:\LocalMachine\Root"
     ```
3. Reinicie o navegador e acesse `https://10.247.168.43:8081/ui`.

---

## 6. Restrições e Garantias de Segurança

* **OPC Estritamente Somente Leitura:** Não existem campos, rotas, funções de protocolo nem botões para escrita OPC DA.
* **Ausência de COM Advise / Assinatura Real:** Todas as leituras são síncronas periódicas Device no worker do agente.
* **Valores Efêmeros Não Persistidos:** Leituras OPC ao vivo nunca são salvas no banco de dados, em arquivos de log, em eventos de auditoria ou em snapshots. Apenas métricas de configuração e estados administrativos são persistidos.
* **Sem Integração com PI Points:** Nenhuma chamada ou criação de tags no OSIsoft PI / AVEVA PI ocorre nesta interface.
* **Proteção de Segredos:** Tokens, hashes de senha e certificados nunca são expostos na interface web ou em storage local do navegador.
* **Acesso Direto à API:** Chamadas internas diretas à API em `127.0.0.1:8081` (como curl ou rotinas de manutenção) continuam exigindo obrigatoriamente `Authorization: Bearer <ADMIN_API_TOKEN>`.
