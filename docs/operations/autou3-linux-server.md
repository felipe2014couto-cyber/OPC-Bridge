# BridgeServer central no AUTOU3 Linux

O comando suportado para execução operacional é `opc-bridge-server`, instalado
com o pacote Python. Ele lê a configuração exclusivamente do ambiente, exige
PostgreSQL e um par TLS válido e envia os logs para stdout, capturados pelo
journal do systemd. A inicialização direta de `BridgeServer` é destinada a
testes e não substitui este launcher em produção.

## Configuração

Copie `deploy/systemd/opc-bridge-server.env.example` para
`/etc/opc-bridge/opc-bridge-server.env`. Preencha `DATABASE_URL` com a URL do
PostgreSQL e configure caminhos absolutos de certificado e chave privada. O
arquivo deve ser legível pelo usuário `opc-bridge`; restrinja permissões, por
exemplo `root:opc-bridge` e modo `0640`. Proteja o arquivo de ambiente com modo
`0600`, pois DATABASE_URL e, opcionalmente, ADMIN_API_TOKEN são segredos.

| Variável | Uso | Padrão |
| --- | --- | --- |
| `OPC_BRIDGE_HOST` | Endereço local de escuta | `0.0.0.0` |
| `OPC_BRIDGE_PORT` | Porta TLS do agente | `8443` |
| `DATABASE_URL` | URL `postgresql://` ou `postgres://`; obrigatória | nenhum |
| `OPC_BRIDGE_TLS_CERTFILE` | Certificado TLS do servidor | nenhum |
| `OPC_BRIDGE_TLS_KEYFILE` | Chave privada correspondente | nenhum |
| `OPC_BRIDGE_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` ou `CRITICAL` | `INFO` |
| `OPC_BRIDGE_LOG_FORMAT` | Formato Python logging em uma linha | formato do exemplo |
| `OPC_BRIDGE_RETENTION_DAYS` | Prazo de retenção operacional, de 1 a 7 dias | `7` |
| `ADMIN_API_TOKEN` | Habilita a API administrativa local | desabilitada |
| `ADMIN_API_PORT` | Porta da API administrativa em loopback | `8081` |

O launcher verifica a URL PostgreSQL, os parâmetros, o token opcional e carrega
o par de certificados antes de construir/iniciar o listener. Falha de
configuração encerra o processo sem abrir a porta 8443. Não há fallback para
SQLite nem para TCP sem TLS. O certificado precisa ser válido para o nome/IP
usado pelo agente (SAN); instale também a CA correspondente no agente, segundo
a configuração de TLS já existente.

`ADMIN_API_TOKEN` deve ser omitido para manter a API administrativa desligada.
Se habilitada, a API escuta apenas em `127.0.0.1`; exponha-a remotamente apenas
por um mecanismo de acesso controlado fora deste serviço.

## Migração e serviço

Instale o pacote no ambiente virtual em `/opt/opc-bridge/venv` com o extra
`persistence` e as dependências operacionais aprovadas. Aplique as migrações
usando um ambiente de execução que tenha `DATABASE_URL` exportada, antes de
iniciar o serviço:

```sh
python -m opc_bridge.server.persistence.migrate
```

Copie `deploy/systemd/opc-bridge-server.service` para
`/etc/systemd/system/opc-bridge-server.service`, crie a conta de sistema
`opc-bridge`, instale o arquivo de ambiente protegido e confirme leitura do
certificado/chave por essa conta. Depois da revisão dos valores do ambiente:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now opc-bridge-server.service
systemctl status opc-bridge-server.service
journalctl -u opc-bridge-server.service -f
```

O estado operacional e o histórico ficam no PostgreSQL informado por
`DATABASE_URL`; o processo não grava estado ou arquivos de log localmente.
O template não cria banco, credenciais, certificados, serviço ou regras de
firewall. Os nomes DNS/IP alcançáveis, CA, SAN, parâmetros reais de PostgreSQL,
usuário/grupo do serviço e disponibilidade de rede/porta devem ser definidos
pelo operador antes da implantação.
