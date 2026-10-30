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


## 🤝 Contributing

Contributions are welcome! Whether it's bug fixes, new features, documentation improvements, or testing — every contribution helps.

### Ways to Contribute

| Type | Description |
|------|-------------|
| 🐛 **Bug Reports** | Found an issue? Open a GitHub Issue with steps to reproduce, expected vs actual behavior, and environment details |
| ✨ **Feature Requests** | Have an idea? Open a GitHub Issue with the "enhancement" label describing the use case |
| 📝 **Documentation** | Fix typos, clarify setup steps, add examples — PRs to `README.md` or `/docs` are always appreciated |
| 🔧 **Code** | Bug fixes, new features, refactoring, tests |
| 🧪 **Testing** | Test on different hardware/OS, report compatibility |
| 💡 **Ideas & Feedback** | Discussions, RFCs, architecture suggestions |

### Quick Start for Contributors

```bash
# 1. Fork the repository
# 2. Clone your fork
git clone https://github.com/YOUR-USERNAME/REPO-NAME.git
cd REPO-NAME

# 3. Create a feature branch
git checkout -b feature/your-feature-name

# 4. Make changes, test locally
# (see README for local development setup)

# 5. Commit with conventional commits
git commit -m "feat: add amazing feature"

# 6. Push and open a Pull Request
git push origin feature/your-feature-name
```

### Commit Convention

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat:     new feature
fix:      bug fix
docs:     documentation only
style:    formatting, no code change
refactor: code restructuring
test:     adding tests
chore:    maintenance, deps, build
```

Examples:
- `feat: add voice selection UI for TTS`
- `fix: handle GPU OOM in model offloading`
- `docs: clarify Docker setup for ARM64`

### Pull Request Guidelines

- ✅ **One feature/fix per PR** — keeps reviews focused
- ✅ **Tests pass** — run local tests before pushing
- ✅ **Update docs** — if behavior changes, update README/comments
- ✅ **No credentials** — never commit `.env`, keys, tokens
- ✅ **Small, focused commits** — easier to review

### Development Setup

See the **Installation** section above for local setup. Most projects use:

```bash
# Python projects
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# or pip install -e ".[dev]" for dev dependencies

# .NET projects
dotnet restore
dotnet build

# Docker projects
docker compose up -d --build
```

### Code Style

| Language | Formatter | Linter |
|----------|-----------|--------|
| Python | `black` | `ruff` |
| C# | `dotnet format` | Built-in |
| JavaScript/TypeScript | `prettier` | `eslint` |
| Blazor | `dotnet format` | Built-in |

Run formatters before committing:
```bash
# Python
black . && ruff check .

# .NET
dotnet format

# JS/TS (if applicable)
prettier --write .
```

### Reporting Security Issues

**Do not open public issues for security vulnerabilities.** Email directly or use GitHub's private vulnerability reporting.

### Community

- 💬 **Discussions**: Use GitHub Discussions for questions, ideas, RFCs
- 🐛 **Issues**: Bug reports, feature requests
- 📧 **Contact**: Open an issue for maintainer contact

### Recognition

Contributors are recognized in:
- GitHub Contributors graph
- Release notes (for significant contributions)
- `AUTHORS.md` or `CONTRIBUTORS.md` (if maintained)

---

**Thank you for contributing!** 🎉

---

## 📄 License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.

MIT License allows:
- ✅ Commercial use
- ✅ Modification
- ✅ Distribution
- ✅ Private use
- ❌ No liability
- ❌ No warranty

---

## ⭐ Star History

If you find this project useful, please consider giving it a star! It helps with visibility and motivation.

## License

MIT
