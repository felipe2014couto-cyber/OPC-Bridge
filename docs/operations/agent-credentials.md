# Provisionar credenciais de agentes

O comando `opc-bridge-agent-credential` cria o agente e sua credencial
individual, ou rotaciona a credencial ativa de um agente existente. Ele exige
`DATABASE_URL` exportada com uma URL PostgreSQL e lê o token exclusivamente de
stdin. SQLite é recusado. O token é processado em memória e somente o hash
SHA-256 compatível com o BridgeServer é persistido; nem o token, nem o hash,
nem a URL são impressos.

Instale o pacote com o extra de persistência e execute o comando em um shell ou
ambiente de segredo controlado que já tenha `DATABASE_URL` exportada. Não
coloque a URL em argumentos do comando ou em histórico de shell. O valor do
token deve ser fornecido por stdin sem eco, por exemplo em Bash:

```sh
IFS= read -r -s -p 'Token do agente: ' agent_token
printf '\n' >&2
printf '%s\n' "$agent_token" | opc-bridge-agent-credential PIMS_TD1_PB2
result=$?
unset agent_token
test "$result" -eq 0
```

O identificador é o único argumento posicional. O comando não aceita token por
argumento; argumentos desconhecidos são rejeitados sem serem repetidos na
mensagem. O token pode conter qualquer conteúdo de uma linha não vazia.

Por padrão, uma segunda execução para um `agent_id` existente é recusada. Para
rotacionar explicitamente, repita o fornecimento seguro do token e use:

```sh
IFS= read -r -s -p 'Novo token do agente: ' agent_token
printf '\n' >&2
printf '%s\n' "$agent_token" | opc-bridge-agent-credential --rotate PIMS_TD1_PB2
result=$?
unset agent_token
test "$result" -eq 0
```

Na rotação, credenciais anteriores são revogadas e a nova credencial é gravada
na mesma transação; um evento de auditoria registra somente a ação e o
identificador da credencial. O programa não conecta a um agente nem altera a
configuração no Windows. O operador deve atualizar o segredo provisionado no
agente pelo processo de implantação aprovado.

Erros de configuração e banco não incluem URL, token ou hash. Códigos de saída:
`0` sucesso, `1` falha genérica de persistência, `2` configuração/entrada
inválida, `3` agente existente sem `--rotate` e `4` agente inexistente com
`--rotate`.
