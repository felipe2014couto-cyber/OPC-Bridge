# Inspeção OPC não destrutiva v1

Capability HELLO: `opc-inspection-v1`. O central só envia requisições a agentes
autenticados que anunciem essa capability. Framing, CRC, versão e mensagens
existentes não mudam. Novos tipos: `OPC_INSPECT_REQUEST=0x0B` e
`OPC_INSPECT_RESPONSE=0x0C`. Corpos são JSON UTF-8, limitados a 128 KiB, sem
campos desconhecidos ou duplicados. Cada requisição tem UUID canônico e há no
máximo uma pendente por sessão. Respostas só resolvem a requisição da mesma
sessão e UUID; respostas tardias são descartadas.

Requisição de tags:

```json
{"request_id":"00000000-0000-4000-8000-000000000001","action":"tags","opc_prog_id":"ABB.AfwOpcDaSurrogate.1","tags":["Good.Tag"]}
```

Requisição de servidores: mesmo esquema, `action: "servers"`, `opc_prog_id: ""`
e `tags: null`. Nenhuma operação permite escrita, browse ou leitura de valores.
ProgID tem até 256 bytes; cada caminho, até 1024 bytes UTF-8; tags únicas, 1–50.

Resposta:

```json
{"request_id":"00000000-0000-4000-8000-000000000001","results":[{"opc_item_path":"Good.Tag","status":"valid","hresult":null}],"servers":null,"error":null}
```

`results` preserva a ordem e os caminhos requisitados. Estados: `valid`,
`invalid` ou `error`. HRESULT é inteiro unsigned de 32 bits ou null. Falhas
OPC_E_UNKNOWNITEMID/OPC_E_INVALIDITEMID indicam `invalid`; demais falhas indicam
`error`. Nenhuma exceção/objeto COM ou mensagem de provider é serializada nessa
mensagem. Para descoberta, `results: null` e `servers` contém até 100 ProgIDs.
Erros globais permitidos: `unavailable`, `busy`, `timeout`, `inspection_failed`,
`invalid_request`, ou null.

O agente usa um novo adapter e worker isolado, com a arquitetura configurada.
Conecta, cria grupo temporário, tenta AddItem individualmente e libera a
conexão. Não chama read_device, não troca estado ativo e não envia CONFIG_ACK.
O central espera no máximo 45 segundos e nunca interpreta inspeção como
confirmação de CONFIG_PUSH. O contrato IPC de grupos continua composto apenas
por nome/intervalo; o envelope de erro IPC pode incluir `hresult` primitivo.
