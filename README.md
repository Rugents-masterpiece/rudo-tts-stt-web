# rudo-tts-stt-web

Local web GUI for **TTS (Text-to-Speech)** and **STT (Speech-to-Text)** powered by **RTX 3050 AI GPU Manager**. Features Chatterbox TTS with voice cloning, WhisperX STT with diarization, and a voice selection UI.

## Features

- **Chatterbox TTS**: Voice cloning from reference audio, multi-speaker support
- **WhisperX STT**: Transcription with word-level timestamps, speaker diarization
- **Voice Selection UI**: Visual voice picker with Fico voice (seed 0, denoised)
- **RTX 3050 Optimized**: Managed by AI GPU Manager for dynamic model loading/offloading
- **REST API**: `/tts`, `/stt`, `/voices`, `/health` endpoints
- **WebSocket**: Real-time STT streaming

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  rudo-tts-stt-web (FastAPI/Uvicorn on :10075)              │
│  ├── app.py              → Main FastAPI application         │
│  ├── static/             → Web UI (HTML/CSS/JS)            │
│  └── tests/              → Test suite                       │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
   ┌───────────────────────┐
   │ AI GPU Manager        │
   │ (manager_app on       │
   │  :18080)              │
   │ ├── Chatterbox TTS    │
   │ ├── WhisperX STT      │
   │ └── Model offloading  │
   └───────────────────────┘
```

## Prerequisites

- Python 3.10+
- RTX 3050 (or compatible GPU)
- AI GPU Manager running on `:18080`
- CUDA 12.x

## Installation

```bash
git clone https://github.com/Rugents-masterpiece/rudo-tts-stt-web.git
cd rudo-tts-stt-web

# Create venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure (copy example)
cp .env.example .env
# Edit .env with your settings

# Run
uvicorn app:app --host 0.0.0.0 --port 10075

# Or with systemd
sudo cp systemd/rudo-tts-stt-web.service /etc/systemd/system/
sudo systemctl enable --now rudo-tts-stt-web
```

## Configuration (`.env`)

```env
AI_GPU_MANAGER_URL=http://127.0.0.1:18080
TTS_MODEL=chatterbox
STT_MODEL=whisperx
VOICE_SELECTOR_UI=true
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/voices` | GET | List available TTS voices |
| `/tts` | POST | Text-to-Speech synthesis |
| `/stt` | POST | Speech-to-Text transcription |
| `/stt/stream` | WS | Real-time STT streaming |

## Voice Selection UI

Access at `http://<host>:10075/` — visual voice picker with:
- Voice cards with play preview
- Fico voice (ref sha 010a6b21, denoised, seed 0)
- Voice cloning from uploaded reference audio

## Development

```bash
# Run tests
pytest tests/

# Lint
ruff check .
black --check .
```

## Systemd Service

```ini
# /etc/systemd/system/rudo-tts-stt-web.service
[Unit]
Description=Rudo krici TTS/STT web (frontend over RTX3050 AI GPU Manager)
After=network-online.target ai-gpu-manager.service
Wants=ai-gpu-manager.service

[Service]
Type=simple
User=rugent
WorkingDirectory=/home/rugent/rudo-tts-stt-web
EnvironmentFile=/home/rugent/rudo-tts-stt-web/.env
ExecStart=/home/rugent/rudo-tts-stt-web/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 10075
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Security

- No credentials in repository
- `.env` is gitignored (copy from `.env.example`)
- AI GPU Manager handles model authentication
- Runs on localhost by default

## License

MIT
