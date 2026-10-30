# Rudo kričí — rudo-tts-stt-web

Jednoduché lokálne GUI nad produkčnými AI službami (RTX 3050 AI stack).
Text → speech aj speech → text na jednej stránke, so zdieľanou frontou.

## Účel

* UI pre produkčné **TTS** (Chatterbox Slovak, hlasy **Rudo** / **Melisko**, predvolený Rudo) a **STT** (Parakeet TDT 0.6B v3)
* **nikdy** nespúšťa vlastný model — všetky AI operácie idú výhradne cez
  `AI GPU Manager` (`http://127.0.0.1:18080`), ktorý drží GPU arbitráž
* server-side globálna FIFO fronta s `concurrency = 1`: nikdy
  TTS+STT, 2×TTS ani 2×STT naraz (platí aj keď web otvoria dvaja naraz)

## Architektúra

```
Browser → rudo-tts-stt-web (:10075) → AI GPU Manager (:18080)
                                        └─ jeden GPU profil naraz
                                           (retrieval | STT | TTS)
```

* `app.py` — FastAPI: routes, SQLite job store, asyncio fronta (Semaphore(1)),
  FFmpeg konverzie (subprocess, argument list, `shell=False`), retention loop
* `static/` — vanilla JS + CSS, žiadne CDN, funguje offline
  (dizajnová rodina podľa interného webu *Peppol features*)
* `data/jobs.db` — front + história; `data/{tts,stt,converted,jobs}/` — súbory
* `logs/jobs.log` — journal `start/finish` (dôkaz sekvenčnosti, pozri nižšie)

Web **nepreberá** normalizáciu, chunking, retry, conditioning ani lifecycle —
to všetko vlastní a riadi manager.

## Install

```bash
cd /home/rugent/rudo-tts-stt-web
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # voliteľné, defaulty sú v kóde
```

Vyžaduje: `ffmpeg` v PATH (over `ffmpeg -version`).

## Run (manuálne)

```bash
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 10075
```

Open: `http://<server-ip>:10075/` (LAN). `AUTH_ENABLED=false` — len dôveryhodná sieť.

## Run (systemd)

```bash
sudo cp systemd/rudo-tts-stt-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rudo-tts-stt-web.service
systemctl status rudo-tts-stt-web.service
```

Beží ako `rugent`, `Restart=on-failure`, port 10075. Nezakročí do AI infraštruktúry.

## API

| Route | Účel |
|---|---|
| `GET /health` | zdravie webu + queue depth + stav managera |
| `GET /api/status` | aktuálny job, čakajúci, manager profil/workers |
| `POST /api/tts` | `{text, normalize, voice?, seed?}` → enqueue TTS job |
| `POST /api/wake` | zavolá minimálny TTS na manager, čím zobudí GPU worker z idle (switch profilu na TTS) |
| `POST /api/stt` | multipart `files=...` (N súborov → N jobov) |
| `GET /api/jobs?limit=N` | história (default 20) |
| `GET /api/job/{id}` | detail jobu (transcript/result_path) |
| `GET /api/job/{id}/download` | WAV / konvertovaný súbor |
| `POST /api/job/{id}/cancel` | len queued |
| `POST /api/jobs/{id}/convert` | WAV→MP3/AAC (`format`,`bitrate`,`volume?`) |
| `GET /api/transcript/{id}.txt` | TXT prepisu (UTF-8) |
| `GET /api/transcripts.txt` | spojené všetky prepisy |
| `GET /api/audio/{id}` | audio priamo do playera |

TTS request na manageri: `POST /v1/audio/speech {input, normalize, voice, seed?}`.
`voice` je iba `rudo` alebo `melisko`; vynechaná hodnota znamená `rudo`,
neplatná hodnota vráti HTTP 422 bez vloženia úlohy. Hlas aj seed sa uložia
pri odoslaní; neskoršia zmena výberu nemení už zaradené úlohy. Prázdny / null
seed neposielame manageru: použije predvolený seed hlasu (**Rudo 42**, **Melisko 0**).
Explicitný seed (vrátane 0) sa odošle bez straty. Ostatné parametre vlastní manager;
UI nemá falošné sliders. Detail, história aj aktuálna/čakajúca fronta vracajú
`voice`; staré TTS záznamy bez tohto poľa znamenajú Rudo. WAV názov obsahuje hlas.

### Offline regresné testy

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
node --check static/app.js
node tests/voice_ui.test.cjs
```

Testy používajú dočasný `APP_HOME`, kópiu statických súborov a mocked HTTP
managera; nespúšťajú lifespan worker, nemenia živú DB a nesyntetizujú na GPU.
Pôvodný `tests/concurrency_proof.py` je živý integračný test, nie offline suite.
Zmeny HTML/backendu sa aktivujú až po **schválenom reštarte** webu; statické
JS/CSS sa servujú okamžite. JS preto toleruje staré HTML bez výberu hlasu
(fallback Rudo). Táto implementácia sama žiadnu službu nereštartuje.

STT request: `POST /v1/audio/transcriptions` (multipart file).
Endpoint nepodporuje t timestampy ani ďalšie kvalitatívne parametre — UI preto
žiadne falošné sliders neobsahuje (advanced panel STT: žiadne dostupné).

## Fronta (concurrency=1)

* jeden asyncio worker, FIFO, `asyncio.Semaphore(1)`
* joby: `queued → processing → completed|failed|cancelled`
* dôkaz zo journalu (`logs/jobs.log`, řadky `start id=…/finish id=…`):
  medzi dvoma `start` je vždy `finish` — `MAX_SIMULTANEOUS_AI_JOBS = 1`
* test: `tests/concurrency_proof.py` (4 klienti, TTS aj STT súčasne)
* počas restartu service: nedokončené joby označia `failed: interrupted by restart`
  (bez trvalého persistence fronty naprieč rebootmi — v1)

## Uploady / downloads

* povolené prípony STT vstupu: wav, mp3, aac, m4a, flac, ogg, opus, webm, mp4, wma
  (ne-WAV vstup sa pred STT prevádza na 16 kHz mono WAV cez ffmpeg)
* limit `MAX_UPLOAD_MB=500`, filename sa sanitizuje a **nikdy nie je filesystem cesta**
* originál z managera = canonical **WAV** (24 kHz mono) — konverzie
  (MP3 128/192/256/320k, AAC 128/192/256k, prípadná loudness norma)
  sa tvoria do `data/converted/`, originál zostáva nezmenený

## Retention

* hodiny: `FILE_RETENTION_HOURS=24` (default); raz za hodinu smaže hotové
  audio + staré uploady a job-row starsie 7 dní. Aktívny job sa nemaže.

## Troubleshooting

* `connection refused :10075` → `systemctl status rudo-tts-stt-web`, prípadne `.env` port
* `manager unavailable` v UI → manager nie je `active`, over `ai-mode status` a `systemctl status ai-gpu-manager.service`
* job stuck `processing` → pozri `logs/jobs.log` + `journalctl -u rudo-tts-stt-web`; worker nesedia, chyba jobu nedrví frontu
* chýbajúce MP3/AAC → over `ffmpeg -version`; konverzia je also job (cez queue UI, ale FFmpeg nejde na GPU)
* UI netuistne → stačí refresh; frontový stav je serverový (`/api/status`)
* **NEMAŽ `logs/jobs.log` počas behu serveru** (drží odvorené fd); ak treba čistý list, reštartuj service.
