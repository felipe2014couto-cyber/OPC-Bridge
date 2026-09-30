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

## Decisão de design: binário vs JSON
A proposta inicial previa JSON com tamanho prefixado sobre TLS. A implementação
adotou framing binário little-endian pelos seguintes motivos:
1. **Desempenho**: a meta de 3.000 tags/s exige serialização/deserialização de
   alta velocidade; `struct.pack/unpack` é ordens de magnitude mais rápido que
   `json.dumps/loads` para payloads numéricos repetitivos.
2. **Tamanho**: payloads binários são estimados em 3-5x menores que JSON
   equivalente (medição reproduzível pendente), reduzindo largura de banda e
   latência em redes industriais.
3. **Tipos nativos**: valores OPC (i16, i32, f32, f64, bool) mapeiam diretamente
   para formatos struct, evitando conversões de string e perda de precisão.
4. **Determinismo**: sem ambiguidade de encoding JSON (whitespace, ordem de chaves),
   facilitando testes de round-trip e depuração.
5. **Compatibilidade**: Python 3.8+ suporta `struct` nativamente; sem dependências
   externas adicionais.

JSON permanece como opção futura para mensagens de configuração humana ou
diagnóstico, mas não para o caminho crítico de leitura Device.

## Requisitos preservados na implementação binária
| Requisito | Status | Evidência |
|-----------|--------|-----------|
| Descoberta de OPC | Pendente | Requer adaptador real (T4) |
| Browse de itens | Pendente | Requer adaptador real (T4) |
| Validação de endereços | Pendente | Requer adaptador real (T4) |
| Configuração versionada | ✅ Implementado | CONFIG_PUSH + CONFIG_ACK com config_version |
| Leitura Device (sem cache) | ✅ Definido | requested_source=0 obrigatório em ItemRef; validado com adaptador simulado |
| Qualidade original OPC | ✅ Implementado | quality (u16) em ItemResult |
| Timestamp por item | ✅ Implementado | timestamp_us (u64) em ItemResult |
| Tipos preservados | ✅ Implementado | ValueType enum + value_bytes em ItemResult |
| Erros individuais por item | ✅ Implementado | status + error_code em ItemResult |
| Lote de leituras | ✅ Implementado | ReadRequestPayload com items[] |
| Duração da leitura | ✅ Implementado | duration_us em ReadResponsePayload |
| Não acumular ciclos | ✅ Implementado | Regra de overrun + scheduler com descarte |
| TLS obrigatório | ✅ Implementado | BridgeServer com ssl.SSLContext |
| Sequências incrementais | ✅ Implementado | seq no Header, rastreado em AgentSession |

## Testes de robustez implementados
- [x] Mensagens fragmentadas (TCP split no meio do header/payload/trailer)
- [x] Mensagens concatenadas (dois/três frames em um único buffer)
- [x] Payload máximo (64KB string, 1000 itens em lote)
- [x] Entrada malformada (CRC corrompido, length inconsistente, magic inválido)
- [x] Sequências incrementais e limite u32
- [ ] Timeout de leitura durante handshake (pendente)
- [ ] Reconexão após desconexão abrupta (coberto parcialmente em test_agent_integration)

## Extensões futuras
- Compressão de payload (flag).
- Multiplexação de streams.
- OPC UA transport mapping.
- JSON alternativo para mensagens de diagnóstico/configuração humana.