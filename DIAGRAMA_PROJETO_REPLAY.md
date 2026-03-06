# Diagrama do Projeto - Replay 15s (Local)

## Fluxograma principal (versao atual)

```mermaid
flowchart TD
    A[Inicio do sistema] --> B[Infra local pronta<br/>Wi-Fi da arena + LAN cameras]
    B --> C[2 cameras IP 1080p PoE<br/>Time A e Time B]
    C --> D[Servidor local Python inicia<br/>FastAPI + FFmpeg + SQLite]
    D --> E[Conectar RTSP das 2 cameras]
    E --> F[Buffer circular por camera<br/>janela 30s]
    F --> G[Gateway Zigbee inicia<br/>Zigbee2MQTT/Home Assistant]
    G --> H[Botoes sem fio com bateria<br/>Time A e Time B + Global]

    H --> I{Botao pressionado?}
    I -- Nao --> F
    I -- Sim --> J[Mapear botao -> camera alvo]
    J --> K[Congelar ultimos 15s do buffer]
    K --> L[Gerar clipe MP4 local]
    L --> M[Salvar metadados no SQLite]

    M --> N{Existe sessao ativa?}
    N -- Sim --> O[Associar clipe ao cliente da sessao]
    N -- Nao --> P[Associar ao perfil padrao do dono]

    O --> Q[Gerar link local temporario + QR]
    P --> Q
    Q --> R[Cliente acessa via Wi-Fi da arena]
    R --> S{Token valido + rede autorizada?}
    S -- Sim --> T[Reproduzir/Baixar clipe]
    S -- Nao --> U[Acesso negado]

    T --> V[Retencao automatica: 30 dias]
    U --> V
    V --> W[Limpeza diaria de arquivos e banco]
    W --> F
```

## Desenho logico da central dos botoes (3 botoes)

```mermaid
flowchart LR
    B1[Botao Zigbee 1<br/>Time A] --> ZC[Coordenador Zigbee<br/>SONOFF ZBDongle-E]
    B2[Botao Zigbee 2<br/>Time B] --> ZC
    B3[Botao Zigbee 3<br/>Global] --> ZC

    ZC --> Z2M[Zigbee2MQTT]
    Z2M --> MQ[Broker MQTT local]
    MQ --> API[Replay API Python]
    API --> BUF[Buffers RTSP 15s+]
    BUF --> CLIP[Gerar clipe MP4]
    API --> DB[(SQLite<br/>sessoes/eventos)]
    API --> WEB[Portal local + QR]
```

## Mapeamento dos 3 botoes

- Botao 1 (`time_a`): salva ultimos 15s da `cam_time_a`
- Botao 2 (`time_b`): salva ultimos 15s da `cam_time_b`
- Botao 3 (`global`): salva ultimos 15s de `cam_time_a` e `cam_time_b`

## Politicas definidas

- Sem camera central nesta fase
- Botoes sem fio com bateria (Zigbee)
- Retencao de clips por 30 dias
- Check-in com campos minimos (nome, celular, inicio, fim, quadra)
