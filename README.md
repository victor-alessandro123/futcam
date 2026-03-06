# FUTCAM - Replay local de 15s

Projeto local para capturar replay de 15 segundos por evento de botao/tecla, com foco em ambiente esportivo.

No estado atual do codigo, a implementacao operacional esta em modo:

- webcam local (indice de camera)
- teclas do teclado como gatilho
- compartilhamento local por link/QR no mesmo Wi-Fi

## O que o sistema faz hoje

- Captura video continuo em buffer circular de 30s
- Ao pressionar tecla configurada (`1`, `2`, `3`...), salva os ultimos 15s em MP4
- Exibe preview em GUI desktop
- Registra metadados em SQLite
- Gera link temporario e QR Code para download local
- Aplica limpeza automatica por retencao (padrao: 30 dias)

## Estrutura atual da pasta principal

- `replay_gui.py` - aplicacao principal (GUI, buffer, gravacao de clipes, SQLite e QR)
- `requirements.txt` - dependencias Python do projeto
- `DIAGRAMA_PROJETO_REPLAY.md` - fluxograma e desenho logico do projeto
- `hikvison bullet.txt` - anotacoes tecnicas da camera Hikvision DS-2CD1023G2-LIU
- `README.md` - documentacao principal

## Requisitos

- Python 3.10+
- Webcam funcional
- Windows (o app usa `os.startfile` para abrir arquivos/pastas)

## Instalacao

```bash
pip install -r requirements.txt
```

## Execucao

```bash
python replay_gui.py --keys "1,2,3" --camera-index 0
```

## Parametros principais

- `--keys`: teclas habilitadas para trigger (ex: `1,2,3,4`)
- `--camera-index`: indice da camera local (normalmente `0`)
- `--clip-seconds`: duracao do replay salvo (padrao `15`)
- `--buffer-seconds`: janela de buffer circular (padrao `30`)
- `--retention-days`: dias de retencao de clipes (padrao `30`)
- `--data-dir`: diretorio para banco e clipes (padrao `./data`)
- `--share-port`: porta do servidor local para links/QR (padrao `8765`)
- `--link-ttl-hours`: validade do link do QR (padrao `12`)

## Uso rapido

1. Inicie o app.
2. Aguarde alguns segundos para preencher o buffer.
3. Clique na janela para garantir foco do teclado.
4. Pressione uma tecla configurada.
5. Gere QR ou abra o arquivo salvo na lista de clipes.

## Proxima etapa planejada

Migrar de webcam/teclado para:

- 2 cameras IP 1080p (RTSP)
- 3 botoes sem fio com bateria (Zigbee)
- check-in minimo por sessao (nome, celular, inicio, fim, quadra)
- distribuicao local dos clipes apenas para usuarios no Wi-Fi da arena
