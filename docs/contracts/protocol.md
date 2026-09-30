# Contrato do Protocolo OPC-Bridge (v0)

## Escopo
Protocolo binário/framing sobre TLS entre servidor central e agente Windows.
Versão inicial para prova de conceito com ABB.AfwOpcDaSurrogate.1.

## Framing
- Little-endian.
- Header fixo: magic(2B) | version(2B) | msg_type(2B) | flags(2B) | seq(4B) | payload_len(4B).
- Payload segue imediatamente; checksum CRC32C no trailer (4B).
- Magic: 0x4F50 ("OP"). Version: 0x0001.

## Mensagens (msg_type)
| ID   | Nome              | Direção       | Descrição                                            |
|------|-------------------|---------------|------------------------------------------------------|
| 0x01 | HELLO             | Agent->Server | Handshake: agent_id, hostname, os_version, caps.     |
| 0x02 | HELLO_ACK         | Server->Agent | Aceite: session_id, heartbeat_interval_ms.           |
| 0x03 | AUTH              | Agent->Server | Credenciais (token/cert hash).                       |
| 0x04 | AUTH_ACK          | Server->Agent | Resultado auth + políticas.                          |
| 0x05 | CONFIG_PUSH       | Server->Agent | Configuração de tags/grupos/intervalos (versionada). |
| 0x06 | CONFIG_ACK        | Agent->Server | Confirmação com config_version aplicada.             |
| 0x07 | READ_REQUEST      | Server->Agent | Lote de leituras: request_id, items[].               |
| 0x08 | READ_RESPONSE     | Agent->Server | Resultados por item + duração total.                 |
| 0x09 | HEARTBEAT         | Bidirecional  | Keepalive com timestamp local.                       |
| 0x0A | ERROR             | Bidirecional  | Erro de protocolo/sessão.                            |

## Estruturas comuns
### ItemRef (READ_REQUEST)
- item_id (u32): identificador estável atribuído pelo server.
- opc_item_path (utf8, len-prefixed u16).
- requested_source (u8): 0=Device, 1=Cache (reservado; v0 exige Device).

### ItemResult (READ_RESPONSE)
- item_id (u32)
- status (u8): 0=OK, 1=BAD_QUALITY, 2=NOT_FOUND, 3=TIMEOUT, 4=ERROR
- value_type (u8): 0=i16, 1=i32, 2=f32, 3=f64, 4=bool, 5=string, 6=blob
- quality (u16): qualidade original OPC
- timestamp_us (u64): microsegundos desde epoch UTC
- value_len (u32) + value_bytes
- error_code (u32): código específico quando status != 0

## Regras
- Cada READ_REQUEST exige nova leitura Device; cache é proibido em v0.
- Reutilizar conexões/grupos/itens no agente; IDs são estáveis por sessão.
- Se leitura ultrapassar intervalo, não acumular ciclos: descartar pendentes
  e reportar overrun em HEARTBEAT ou próximo READ_RESPONSE.
- CONFIG_PUSH carrega versão; CONFIG_ACK confirma versão exata aplicada.
- CONFIG rejeitada não pode ser confirmada como aplicada.
- TLS obrigatório; credenciais nunca em claro.
- Sequências (seq) incrementais por direção; detecção de perda/duplicação.

## Extensões futuras
- Compressão de payload (flag).
- Multiplexação de streams.
- OPC UA transport mapping.