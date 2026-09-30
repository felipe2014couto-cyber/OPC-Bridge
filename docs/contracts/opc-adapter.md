# Contrato do Adaptador OPC DA (v0)

## Escopo
Interface entre o serviço Windows do agente e os servidores OPC DA locais.
Primeiro alvo: ABB.AfwOpcDaSurrogate.1.

## Responsabilidades
- Descobrir servidores OPC DA registrados na máquina.
- Conectar a um servidor específico por ProgID ou CLSID.
- Criar e reutilizar grupos OPC com taxa de atualização configurável.
- Adicionar, remover e listar itens dentro de um grupo.
- Executar leituras síncronas com origem Device (sem cache).
- Retornar valor, tipo, qualidade original, timestamp e erro por item.
- Isolar chamadas COM em processo supervisionado separado do serviço principal.
- Desconectar e liberar recursos COM de forma determinística.

## Interface mínima (Python / COM)
```python
class OpcAdapter:
    def connect(self, prog_id: str) -> None: ...
    def disconnect(self) -> None: ...
    def create_group(self, name: str, update_rate_ms: int) -> GroupHandle: ...
    def remove_group(self, handle: GroupHandle) -> None: ...
    def add_items(self, group: GroupHandle, item_paths: list[str]) -> dict[str, ItemId]: ...
    def remove_items(self, group: GroupHandle, item_ids: list[ItemId]) -> None: ...
    def read_device(self, group: GroupHandle, item_ids: list[ItemId]) -> list[ItemResult]: ...
    def browse_items(self, parent_path: str = "") -> list[BrowseEntry]: ...
    def get_server_status(self) -> ServerStatus: ...
```

## Estruturas de retorno
### ItemResult
- item_id: identificador estável atribuído pelo adaptador.
- status: OK | BAD_QUALITY | NOT_FOUND | TIMEOUT | ERROR.
- value: dado lido (tipo preservado).
- value_type: i16 | i32 | f32 | f64 | bool | string | blob.
- quality: qualidade original OPC (u16).
- timestamp_us: microsegundos desde epoch UTC.
- error_code: código específico quando status != 0.

### BrowseEntry
- path: caminho completo do item.
- name: nome exibido.
- data_type: tipo OPC reportado.
- access_rights: leitura/escrita.
- is_leaf: True se não possui filhos.

### ServerStatus
- state: RUNNING | FAILED | NO_CONFIG | SUSPENDED | TEST.
- vendor_info: string do fabricante.
- version: versão do servidor OPC.
- start_time: timestamp de início do servidor.

## Regras
- Cada `read_device` deve solicitar leitura Device ao servidor OPC; cache proibido.
- Reutilizar conexões, grupos e identificadores de itens entre ciclos.
- Se uma leitura ultrapassar o intervalo, não acumular ciclos pendentes.
- Isolamento COM obrigatório: falhas no adaptador não podem derrubar o serviço.
- Não anunciar compatibilidade universal sem teste real contra o servidor alvo.
- Suportar x86 e x64 conforme arquitetura do componente OPC instalado.
- Logs estruturados com duração da leitura e erros individuais por item.

## Validação
- Prova real de leitura Device no ABB.AfwOpcDaSurrogate.1 é pré-requisito.
- Testes com OPC simulado são úteis para desenvolvimento, mas não comprovam
  compatibilidade com o ABB.
- Capacidade de 3.000 tags/s depende de validação no ambiente real.