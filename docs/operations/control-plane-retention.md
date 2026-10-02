# Retenção do control plane

O comando `opc-bridge-retention` aplica uma política de retenção ao PostgreSQL
do control plane. O padrão é `OPC_BRIDGE_RETENTION_DAYS=7`; valores explícitos
aceitos vão de 1 a 7 dias. O cutoff é calculado em UTC como o instante atual
menos o número de dias configurado. Sem `--apply`, o comando é dry-run e apenas
conta o que seria removido. A saída é uma linha resumida adequada ao journal e
não inclui `DATABASE_URL` nem outros segredos.

Execute os comandos em um ambiente controlado que tenha `DATABASE_URL`
PostgreSQL exportada a partir do armazenamento de segredos aprovado. SQLite é
recusado pelo comando.

O schema precisa estar atualizado antes de executar a retenção. Aplique as
migrações pelo procedimento operacional de banco existente, por exemplo com o
comando de migração do pacote em um ambiente que tenha a URL PostgreSQL
exportada:

```sh
python -m opc_bridge.server.persistence.migrate
```

Depois, para validar a política sem excluir dados:

```sh
opc-bridge-retention
```

Para aplicar manualmente depois de revisar as quantidades:

```sh
opc-bridge-retention --apply
```

Defina `OPC_BRIDGE_RETENTION_DAYS` no EnvironmentFile protegido do servidor
para usar outro prazo, por exemplo `3`. A mesma variável é usada pelo timer.
Não coloque `DATABASE_URL` na linha de comando ou no histórico do shell.

## Timer systemd

Os arquivos em `deploy/systemd/` são templates; esta documentação não cria nem
instala units. Revise-os e copie-os para `/etc/systemd/system/` quando a
implantação for aprovada. O serviço é oneshot, roda como usuário não-root
`opc-bridge`, lê o mesmo `/etc/opc-bridge/opc-bridge-server.env` protegido do
BridgeServer e passa `--apply`. O timer executa diariamente, com atraso
aleatório de até 15 minutos:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now opc-bridge-retention.timer
systemctl list-timers opc-bridge-retention.timer
journalctl -u opc-bridge-retention.service
```

Para uma simulação usando a mesma configuração, execute o binário diretamente
como `opc-bridge` dentro do ambiente que exporta o EnvironmentFile. O timer só
deve ser habilitado após conferir primeiro a saída dry-run e validar uma
restauração de backup.

## Dados que expiram e dados preservados

Depois do cutoff, o comando pode remover:

- sessões encerradas, incluindo o heartbeat e o estado observado guardados
  nessas sessões;
- operações de configuração terminais (`applied`, `rejected`, `failed` ou
  `expired`) e antigas;
- snapshots antigos sem referência a operação preservada, exceto o snapshot
  global mais recente necessário à recuperação do servidor;
- eventos de auditoria operacional antigos.

Sessões ativas, operações pendentes, operações recentes, o snapshot global mais
recente e a última operação aplicada de cada agente (que ancora sua configuração
válida) são preservados. Snapshots referenciados por qualquer operação
preservada também são mantidos. A identidade/cadastro dos agentes, todos os
registros de credencial ativa ou revogada, os eventos `agent.credential.*` de
auditoria de rotação e os planos de coleta não expiram por esta rotina. Assim,
a operação aplicada que ancora a configuração atual pode permanecer além do
prazo; ela é metadado de estado atual, não histórico operacional removível.

As exclusões são feitas por repositório, com consultas parametrizadas, numa
única transação. Reexecutar a rotina é seguro: dados já removidos não voltam a
ser selecionados. A retenção não substitui backups do PostgreSQL; mantenha
backups regulares, protegidos e com testes periódicos de restauração.
