# Administração de planos de leitura OPC e Interface Web

A interface web administrativa do control plane do OPC-Bridge é servida pelo daemon interno do central em `http://127.0.0.1:8081/ui` e disponibilizada externamente de forma segura via **proxy reverso HTTPS com autenticação Basic no Nginx**.

Nenhum componente web externo, Node.js, npm ou dependência de CDN é utilizado.

---

## 1. Arquitetura de Acesso e Proxy Reverso HTTPS

O servidor central mantém a API administrativa interna privada no loopback (`127.0.0.1:8081`), acessível apenas localmente ou através do proxy reverso configurado no Nginx.

* **URL externa oficial:** `https://10.247.168.43:8081/ui`
* **Porta exposta na rede:** `10.247.168.43:8081` (exclusivamente HTTPS/TLS)
* **ACL de rede:** Acesso restrito via diretivas Nginx exclusivamente à estação de operação/engenharia (`10.247.87.39`), com descarte (`deny all;`) para quaisquer outros IPs.
* **Autenticação no navegador:** HTTP Basic Authentication gerenciada pelo Nginx, utilizando o arquivo de credenciais `/etc/opc-bridge/nginx-admin.htpasswd`.
* **Injeção de Bearer Token:** O Nginx injeta o cabeçalho técnico `Authorization: Bearer <ADMIN_API_TOKEN>` ao encaminhar a requisição para o backend WSGI interno (`127.0.0.1:8081`), sobrescrevendo qualquer cabeçalho de autorização enviado pelo cliente.
* **Segurança no navegador:** O navegador **não recebe, não solicita, não armazena (sem uso de `localStorage` ou `sessionStorage`), não imprime e não envia** o token `ADMIN_API_TOKEN`. As requisições executadas pelo JavaScript da UI utilizam caminhos relativos same-origin (`/api/v1/...`) sem cabeçalho `Authorization` nem credenciais no código-fonte.
* **Proteção contra exposição direta:** A exposição direta da porta HTTP interna sem criptografia ou em bind amplo (`0.0.0.0` sem TLS) é estritamente proibida.

---

## 2. Configuração Operacional do Nginx e Segredos no Host Central (AUTOU3)

O template de configuração do Nginx é versionado em [`deploy/nginx/opc-bridge-admin-ui.conf.example`](file:///C:/OPCBridge-build/deploy/nginx/opc-bridge-admin-ui.conf.example).

### 2.1. Arquivo de credenciais Basic Auth (`nginx-admin.htpasswd`)

No host AUTOU3, crie manualmente o arquivo de credenciais Basic Auth para o operador/administrador:

```sh
sudo htpasswd -c -B /etc/opc-bridge/nginx-admin.htpasswd admin
# Para fins de homologação e testes iniciais, defina a senha temporária combinada.
sudo chown root:root /etc/opc-bridge/nginx-admin.htpasswd
sudo chmod 600 /etc/opc-bridge/nginx-admin.htpasswd
```

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

## 3. Instalação da CA Interna no Windows Local

Para que a conexão HTTPS em `https://10.247.168.43:8081/ui` seja reconhecida como confiável pelo navegador no Windows sem alertas de segurança TLS:

1. Obtenha o certificado da autoridade certificadora interna do servidor central (`ca.crt` ou `ca.pem`).
2. No Windows local (estação `10.247.87.39`):
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

## 4. Seleção de Servidor e Validação de Tags

1. Abra `https://10.247.168.43:8081/ui`. O navegador solicitará o usuário e senha (Basic Auth).
2. A interface carrega automaticamente o workspace com a lista de agentes conectados.
3. Selecione um agente cadastrado. A tela mostra estado, sessão, heartbeat e a configuração aplicada sanitizada, separada do plano em edição.
4. Use **Consultar servidores** para listar os ProgIDs anunciados pelo agente via descoberta local, ou informe manualmente o ProgID (ex: `ABB.AfwOpcDaSurrogate.1`).
5. Informe o intervalo desejado (inteiro de 1000 a 60000 ms) e de 1 a 50 endereços únicos de tags OPC DA (ex: `Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN`).
6. **Validar** verifica uma tag pontual; **Validar lista** verifica o plano completo. O agente deve estar conectado e anunciar suporte à capability `opc-inspection-v1`.

### Propriedades da inspeção OPC:
* A validação instancia um processo worker secundário temporário, respeitando a arquitetura definida para o agente (x64 ou x86).
* O worker conecta ao servidor OPC DA via `OPC.Automation`, cria um grupo transitório e executa `AddItem` para cada tag em modo Device.
* **Isolamento rigoroso:** A inspeção não lê valores, não escreve valores, não gera `CONFIG_PUSH`, e não interfere nem interrompe o worker principal de leitura ativa.
* Cada tag retorna o status seguro `valid`, `invalid` ou `error`, acompanhado de HRESULT numérico caso ocorra erro. Nenhum segredo, valor de processo ou caminho interno do servidor OPC é exposto.

---

## 5. Aplicação e Histórico de Configurações

1. Após executar **Validar lista** com 100% das tags com status `valid`, o botão **Aplicar configuração** é habilitado.
2. Ao clicar em **Aplicar configuração**, uma caixa de confirmação explícita do navegador é exibida (`window.confirm`).
3. O ticket de validação possui validade de 5 minutos, é de uso único e é atrelado estritamente à sessão ativa do agente e ao fingerprint criptográfico do plano validado.
4. Qualquer alteração nas tags, ProgID, intervalo, reinício do central ou reconexão do agente invalida imediatamente a aprovação e exige nova validação completa.
5. A aplicação confirmada gera uma nova versão de configuração despachada via `CONFIG_PUSH` ao worker ativo do agente. O histórico de operações é atualizado a cada 5 segundos na tabela da interface.

---

## 6. Restrições e Garantias de Segurança

* **Escrita OPC:** Totalmente indisponível. Não existem botões, campos, rotas de API nem suporte de protocolo para escrita de tags.
* **Integração PI Point:** A interface não realiza criação, edição ou vinculação a PI Points ou PI Web API.
* **Limites de Capacidade:** Máximo de 50 tags por plano e intervalos entre 1.000 ms e 60.000 ms.
* **Acesso Direto à API:** Chamadas diretas à API em `127.0.0.1:8081` (como scripts de automação interna ou curl) continuam exigindo obrigatoriamente `Authorization: Bearer <ADMIN_API_TOKEN>`.
