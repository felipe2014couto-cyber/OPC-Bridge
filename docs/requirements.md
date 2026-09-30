# Requisitos iniciais

## Produto
- Desenvolvimento em Python.
- Servidor central administra os agentes e todas as configurações.
- Agente instalável como serviço Windows com início automático.
- Na máquina do agente, o usuário somente executa a instalação.
- Instalação e operação sem internet, com acesso ao servidor central
  pela rede interna.
- Instalador contém runtime, dependências e dados de conexão inicial.
- Credenciais reais não devem ser versionadas.

## OPC
- Primeira versão: OPC DA.
- Primeiro servidor a validar: ABB.AfwOpcDaSurrogate.1.
- Item de referência: Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN.
- Permitir selecionar diferentes servidores OPC DA locais.
- Descobrir servidores, navegar itens e validar endereços.
- Não anunciar compatibilidade universal sem testes.
- OPC UA é uma extensão futura, não um requisito inicial confirmado.

## Coleta
- Servidor central envia os pedidos de leitura.
- Intervalo inicial: 1000 ms.
- Meta de dimensionamento: até 3000 tags por segundo.
- Cada pedido executa uma nova leitura OPC, solicitando origem Device.
- Não substituir leitura solicitada por último valor armazenado.
- Reutilizar conexões, grupos e identificadores dos itens.
- Suportar pedidos em lote e erros individuais.
- Retornar valor, tipo, qualidade, timestamp e erro por item.
- Registrar duração da leitura.
- Não acumular ciclos indefinidamente quando o OPC estiver ocupado.
- Não repetir automaticamente pedidos de leitura após desconexão.
- Capacidade e semântica Device dependem de validação no OPC real.

## Plataforma
- Agente: Windows 7 SP1 em diante.
- Definir e validar dependências para pacote legado Python 3.8.
- Avaliar pacote moderno separado, mantendo o mesmo protocolo.
- Testar arquiteturas x86/x64 conforme componentes OPC.
- Build e testes do instalador precisam ocorrer no Windows.

## Arquitetura
- Agente inicia conexão persistente autenticada com o servidor.
- Comunicação com TLS e protocolo versionado.
- Acesso OPC em processo separado do serviço principal.
- Configurações possuem versão e confirmação de aplicação.
- Configuração rejeitada não pode ser anunciada como aplicada.
- Logs locais com rotação.
- Histórico de valores não faz parte da primeira prova de conceito.
