"""rudo-tts-stt-web backend — thin UI/queue layer over the RTX3050 AI GPU manager.

The manager remains the single source of truth for GPU arbitration, TTS
conditioning/retry/normalization/chunking, and STT model lifecycle. This
service only: UI, one global FIFO queue (concurrency=1), file handling,
FFmpeg format conversion, retention.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

BASE = Path(os.getenv("APP_HOME", str(Path(__file__).resolve().parent)))

# job journal goes to its own file (systemd stdout is journald; keep
# deterministic log-based testing independent of it)
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
jh = logging.FileHandler(LOG_DIR / "jobs.log", encoding="utf-8")
jh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logging.getLogger("rudo-jobs").addHandler(jh)
logging.getLogger("rudo-jobs").setLevel(logging.INFO)
logging.getLogger("rudo-jobs").propagate = False
DATA = BASE / "data"
JOBS_DIR = DATA / "jobs"
TTS_DIR = DATA / "tts"
STT_DIR = DATA / "stt"
CONV_DIR = DATA / "converted"
DB_PATH = DATA / "jobs.db"

MANAGER = os.getenv("AI_MANAGER_URL", "http://127.0.0.1:18080").rstrip("/")
AI_TIMEOUT = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "600"))
FILE_RETENTION_HOURS = float(os.getenv("FILE_RETENTION_HOURS", "24"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
PORT = int(os.getenv("PORT", "10075"))
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() in ("1", "true", "yes")

# Canonical production TTS values (tts-production.env) — the manager worker owns them.
TTS_CANONICAL = {
    "temperature": 0.6, "top_p": 0.92, "min_p": 0.05,
    "repetition_penalty": 1.35, "cfg_weight": 0.5, "exaggeration": 0.5,
}
AUDIO_EXTS = {"wav", "mp3", "aac", "m4a", "flac", "ogg", "opus", "webm", "mp4", "wma"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.\-\u0080-\uFFFF]+")


def safe_name(name: str) -> str:
    name = Path(name).name
    name = SAFE_NAME.sub("_", name)
    return name[:180] or "upload"


# ---------------------------------------------------------------- sqlite store
# NOTE: every db() call must be closed by the caller. Historically callers
# leaked connections, which filled the 1024 fd limit (EMFILE: Too many open
# files) and made static file serving / API return HTTP 500.
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id TEXT PRIMARY KEY, kind TEXT, status TEXT, filename TEXT, text_preview TEXT,
        created REAL, started REAL, finished REAL, error TEXT, result_path TEXT,
        params TEXT, sequence INTEGER)""")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v)")
    conn.commit()
    return conn


class Queue:
    """Global FIFO, single asyncio worker. Guarantees one AI job at a time
    across TTS+STT regardless of how many browsers submitted."""

    def __init__(self):
        self.sema = asyncio.Semaphore(1)
        self.seq = 0
        self.cond = asyncio.Condition()
        self.pending: list[str] = []
        self.current: str | None = None
        self.task: asyncio.Task | None = None

    async def enqueue(self, job_id: str) -> int:
        async with self.cond:
            self.seq += 1
            s = self.seq
            self.pending.append(job_id)
            conn = db()
            try:
                conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_seq',?)", (str(self.seq),))
                conn.execute("UPDATE jobs SET sequence=? WHERE id=?", (s, job_id))
                conn.commit()
            finally:
                conn.close()
            self.cond.notify()
        return s

    async def run(self):
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            async with self.cond:
                while not self.pending:
                    await self.cond.wait()
                job_id = self.pending.pop(0)
            async with self.sema:
                self.current = job_id
                try:
                    await process_job(job_id)
                except Exception as exc:  # never wedge the worker
                    try:
                        _mark_failed(job_id, str(exc))
                    except Exception:
                        pass
                finally:
                    self.current = None

    def snapshot(self):
        return {"current": self.current, "queued": list(self.pending), "worker_busy": self.current is not None}


QUEUE = Queue()


def _mark_failed(job_id: str, err: str):
    c = db()
    try:
        c.execute("UPDATE jobs SET status='failed', error=?, finished=? WHERE id=? AND status IN ('queued','processing')",
                  (err[:400], time.time(), job_id))
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------- job helpers
def job_get(job_id: str):
    c = db()
    try:
        return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        c.close()


def job_voice(row):
    """Legacy TTS rows predate voice selection; non-TTS jobs have no voice."""
    return json.loads(row["params"] or "{}").get("voice", "rudo") if row["kind"] == "tts" else None


def job_set(job_id: str, **kw):
    c = db()
    sets, vals = [], []
    for k, v in kw.items():
        sets.append(f"{k}=?" if k != "params" else "params=?")
        vals.append(json.dumps(v) if k == "params" else v)
    vals.append(job_id)
    c.execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", vals)
    c.commit()
    c.close()


# ---------------------------------------------------------------- processing
async def process_job(job_id: str):
    c = db()
    try:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "queued":
            return
    finally:
        c.close()
    job_set(job_id, status="processing", started=time.time())
    row = job_get(job_id)
    log = logging.getLogger("rudo-jobs")
    preview = f"{(row['filename'] or 'text')}"[:80] if row["kind"] != "tts" else f"{(json.loads(row['params'] or '{}').get('text') or '')[:60]!r}"
    log.info("start id=%s seq=%s kind=%s %s", job_id, row["sequence"], row["kind"], preview)
    try:
        async with httpx.AsyncClient(timeout=AI_TIMEOUT) as client:
            if row["kind"] == "tts":
                await run_tts(client, row)
            elif row["kind"] == "stt":
                await run_stt(client, row)
            elif row["kind"] == "convert":
                await asyncio.to_thread(run_convert, row)
            else:
                raise RuntimeError(f"unknown kind {row['kind']}")
        job_set(job_id, status="completed", finished=time.time())
        started = row["started"] if row["started"] else time.time()
        logging.getLogger("rudo-jobs").info("finish id=%s kind=%s status=completed dur=%.2fs", job_id, row["kind"], time.time() - started)
    except asyncio.CancelledError:
        job_set(job_id, status="cancelled", finished=time.time())
        logging.getLogger("rudo-jobs").info("finish id=%s kind=%s status=cancelled", job_id, row["kind"])
        raise
    except Exception as exc:
        job_set(job_id, status="failed", finished=time.time(), error=str(exc)[:400])
        logging.getLogger("rudo-jobs").info("finish id=%s kind=%s status=failed", job_id, row["kind"])


def _upload_path(job_id: str) -> Path | None:
    p = JOBS_DIR / job_id
    return p if p.is_file() else None


async def run_tts(client: httpx.AsyncClient, row):
    params = json.loads(row["params"] or "{}")
    text = params.get("text", "")
    voice = params.get("voice", "rudo")
    payload = {"input": text, "normalize": bool(params.get("normalize", True)), "voice": voice}
    if params.get("seed") is not None:
        payload["seed"] = int(params["seed"])
    r = await client.post(f"{MANAGER}/v1/audio/speech", json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"manager TTS HTTP {r.status_code}: {r.text[:200]}")
    audio = r.content
    if audio[:4] != b"RIFF":
        raise RuntimeError("manager returned non-WAV audio")
    name = f"{voice}-tts-{time.strftime('%Y-%m-%d-%H%M%S')}-{row['id'][:8]}.wav"
    out = TTS_DIR / name
    out.write_bytes(audio)
    job_set(row["id"], result_path=str(out))


async def run_stt(client: httpx.AsyncClient, row):
    params = json.loads(row["params"] or "{}")
    src = _upload_path(row["id"])
    if not src:
        raise RuntimeError("upload file missing")
    wav = src
    if src.suffix.lower().lstrip(".") not in ("wav", "flac"):
        # canonicalize to 16k mono PCM WAV via ffmpeg before STT
        conv = STT_DIR / f"{row['id']}_canonical.wav"
        await asyncio.to_thread(_ffmpeg, ["ffmpeg", "-y", "-v", "error", "-i", str(src),
                                          "-ac", "1", "-ar", "16000", str(conv)], 120)
        wav = conv
    data = wav.read_bytes()
    r = await client.post(f"{MANAGER}/v1/audio/transcriptions",
                          files={"file": (wav.name, data, "audio/wav")})
    if r.status_code != 200:
        raise RuntimeError(f"manager STT HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    text = j.get("text", "")
    job_set(row["id"], params={**params, "transcript": text, "raw": {k: v for k, v in j.items() if k != "text"}})


def _ffmpeg(argv, timeout):
    p = subprocess.run(argv, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed rc={p.returncode}: {p.stderr.decode(errors='replace')[:200]}")


def run_convert(row):
    params = json.loads(row["params"] or "{}")
    src = Path(params["wav_path"])
    if not src.is_file():
        raise RuntimeError("source WAV missing")
    fmt = params.get("format", "mp3")
    bitrate = params.get("bitrate", "192k")
    volume = params.get("volume")  # e.g. "-16" LUFS target or None
    sr = params.get("sample_rate")  # None => keep original
    ch = params.get("channels")   # None => keep original
    name = f"{src.stem.replace('.wav','')}-{fmt}.{fmt}"
    if fmt in ("mp3", "aac"):
        name = name if "." in name else name
    out = CONV_DIR / f"{row['id'][:8]}-{name}"
    argv = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if volume:
        argv += ["-af", f"loudnorm=I={volume}:TP=-1.5:LRA=11"]
    if sr:
        argv += ["-ar", str(sr)]
    if ch:
        argv += ["-ac", "1" if ch == "mono" else "2"]
    if fmt == "mp3":
        argv += ["-c:a", "libmp3lame", "-b:a", bitrate]
    elif fmt == "aac":
        argv += ["-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart"]
        out = out.with_suffix(".m4a")
    elif fmt == "wav24":
        argv += ["-c:a", "pcm_s24le"]
    else:
        argv += ["-c:a", "pcm_s16le"]
        out = out.with_suffix(".wav")
    argv.append(str(out))
    _ffmpeg(argv, 600)
    job_set(row["id"], result_path=str(out))


# ---------------------------------------------------------------- retention
async def retention_loop():
    while True:
        try:
            cutoff = time.time() - FILE_RETENTION_HOURS * 3600
            c = db()
            for row in c.execute("SELECT id, result_path FROM jobs WHERE finished IS NOT NULL AND finished < ? AND status IN ('completed','failed','cancelled')", (cutoff,)).fetchall():
                if row["result_path"]:
                    p = Path(row["result_path"])
                    if p.is_file() and (row["id"] != QUEUE.current):
                        p.unlink(missing_ok=True)
            # prune job records older than 7 days
            c.execute("DELETE FROM jobs WHERE created < ?", (time.time() - 7 * 86400,))
            c.commit()
            c.close()
            # drop orphan upload files
            for f in JOBS_DIR.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff and f.stem != QUEUE.current:
                    f.unlink(missing_ok=True)
        except Exception:
            pass
        await asyncio.sleep(3600)


# ---------------------------------------------------------------- API models
class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=100000)
    normalize: bool = True
    seed: int | None = None
    voice: Literal["rudo", "melisko", "fico"] = "rudo"


class ConvertIn(BaseModel):
    wav_path: str
    format: str = "mp3"
    bitrate: str = "192k"
    volume: float | None = None
    sample_rate: int | None = None
    channels: str | None = None


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    for d in (DATA, JOBS_DIR, TTS_DIR, STT_DIR, CONV_DIR):
        d.mkdir(parents=True, exist_ok=True)
    c = db()
    c.execute("UPDATE jobs SET status='failed', error='interrupted by restart', finished=? WHERE status IN ('queued','processing')", (time.time(),))
    row = c.execute("SELECT v FROM meta WHERE k='last_seq'").fetchone()
    if row:
        QUEUE.seq = int(row[0])
    c.commit()
    c.close()
    await QUEUE.run()
    app.state.retention = asyncio.create_task(retention_loop())
    yield


app = FastAPI(title="Rudo kričí – TTS / STT", lifespan=lifespan)


@app.get("/health")
async def health():
    mgr = "unknown"
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            r = await cl.get(f"{MANAGER}/health")
            mgr = "healthy" if r.status_code == 200 else "degraded"
    except Exception:
        mgr = "down"
    snap = QUEUE.snapshot()
    return {"status": "healthy", "queue_depth": len(snap["queued"]), "worker_busy": snap["worker_busy"],
            "ai_manager": mgr}


@app.get("/api/status")
async def api_status():
    mgr = {}
    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            mgr = (await cl.get(f"{MANAGER}/health")).json()
    except Exception as exc:
        mgr = {"status": "down", "error": str(exc)[:120]}
    snap = QUEUE.snapshot()
    cur = None
    if snap["current"]:
        row = job_get(snap["current"])
        if row:
            cur = {"id": row["id"], "kind": row["kind"], "filename": row["filename"], "text_preview": row["text_preview"],
                   "status": row["status"], "sequence": row["sequence"], "voice": job_voice(row)}
    queued = []
    for jid in snap["queued"]:
        row = job_get(jid)
        if row:
            queued.append({"id": row["id"], "kind": row["kind"], "filename": row["filename"], "text_preview": row["text_preview"], "sequence": row["sequence"], "voice": job_voice(row)})
    workers = {k: v.get("state") for k, v in (mgr.get("workers") or {}).items()}
    return {"manager": {"status": mgr.get("status"), "active_profile": mgr.get("active_profile"),
                        "state": mgr.get("state"), "queue_depth": mgr.get("queue_depth"), "workers": workers},
            "queue": {"current": cur, "queued": queued, "busy": snap["worker_busy"]}}


@app.post("/api/tts")
async def api_tts(body: TTSIn):
    job_id = str(uuid.uuid4())
    c = db()
    try:
        c.execute("INSERT INTO jobs(id,kind,status,created,params,text_preview) VALUES(?,?,?,?,?,?)",
                  (job_id, "tts", "queued", time.time(), json.dumps(body.model_dump()),
                   body.text[:120]))
        c.commit()
    finally:
        c.close()
    seq = await QUEUE.enqueue(job_id)
    return {"id": job_id, "sequence": seq, "status": "queued", "voice": body.voice}


@app.post("/api/stt")
async def api_stt(files: list[UploadFile] = File(...)):
    if len(files) == 0:
        raise HTTPException(400, "no files")
    created = []
    for f in files:
        name = safe_name(f.filename or "upload")
        if name.rsplit(".", 1)[-1].lower() not in AUDIO_EXTS:
            raise HTTPException(400, f"unsupported extension: {name}")
        job_id = str(uuid.uuid4())
        dst = JOBS_DIR / job_id
        size = 0
        try:
            with dst.open("wb") as fh:
                while chunk := await f.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_UPLOAD_MB * 1024 * 1024:
                        fh.close(); dst.unlink(missing_ok=True)
                        raise HTTPException(413, f"file too large (max {MAX_UPLOAD_MB} MB): {name}")
                    fh.write(chunk)
        except HTTPException:
            raise
        c = db()
        c.execute("INSERT INTO jobs(id,kind,status,created,filename,params,text_preview) VALUES(?,?,?,?,?,?,?)",
                  (job_id, "stt", "queued", time.time(), name, json.dumps({"orig_name": name}), None))
        c.commit()
        c.close()
        seq = await QUEUE.enqueue(job_id)
        created.append({"id": job_id, "sequence": seq, "filename": name, "status": "queued"})
    return {"jobs": created}


@app.get("/api/jobs")
async def api_jobs(limit: int = 20):
    c = db()
    try:
        rows = c.execute("SELECT id,kind,status,filename,text_preview,created,started,finished,error,params FROM jobs ORDER BY created DESC LIMIT ?", (min(limit, 100),)).fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        dur = (r["finished"] - r["started"]) if (r["finished"] and r["started"]) else None
        out.append({k: r[k] for k in ("id", "kind", "status", "filename", "text_preview", "created", "finished")} | {"duration_s": dur, "voice": job_voice(r)})
    return {"jobs": out}


@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    row = job_get(job_id)
    if not row:
        raise HTTPException(404, "job not found")
    params = json.loads(row["params"] or "{}")
    dur = (row["finished"] - row["started"]) if (row["finished"] and row["started"]) else None
    return {"id": row["id"], "kind": row["kind"], "status": row["status"], "filename": row["filename"],
            "text_preview": row["text_preview"], "transcript": params.get("transcript"), "raw": params.get("raw"),
            "result_path": row["result_path"], "duration_s": dur, "error": row["error"], "created": row["created"],
            "voice": job_voice(row), "seed": params.get("seed") if row["kind"] == "tts" else None}


@app.post("/api/job/{job_id}/cancel")
async def api_cancel(job_id: str):
    row = job_get(job_id)
    if not row:
        raise HTTPException(404, "job not found")
    if row["status"] != "queued":
        raise HTTPException(409, "only queued jobs can be cancelled")
    async with QUEUE.cond:
        if job_id in QUEUE.pending:
            QUEUE.pending.remove(job_id)
    job_set(job_id, status="cancelled", finished=time.time())
    return {"id": job_id, "status": "cancelled"}


@app.post("/api/jobs/{job_id}/convert")
async def api_convert(job_id: str, body: ConvertIn):
    row = job_get(job_id)
    if not row or row["kind"] != "tts" or not row["result_path"]:
        raise HTTPException(400, "job has no WAV result yet")
    wav_path = row["result_path"]
    if not Path(wav_path).is_file():
        raise HTTPException(404, "WAV file expired — regenerate the audio")
    if body.format not in ("mp3", "aac", "wav24"):
        raise HTTPException(400, "format must be mp3, aac or wav24")
    if body.bitrate not in ("96k", "128k", "192k", "256k", "320k"):
        raise HTTPException(400, "unsupported bitrate")
    if body.sample_rate not in (None, 44100, 48000):
        raise HTTPException(400, "unsupported sample rate")
    if body.channels not in (None, "mono", "stereo"):
        raise HTTPException(400, "unsupported channels")
    cid = str(uuid.uuid4())
    c = db()
    c.execute("INSERT INTO jobs(id,kind,status,created,params,filename) VALUES(?,?,?,?,?,?)",
              (cid, "convert", "queued", time.time(), json.dumps({"wav_path": wav_path, **body.model_dump()}),
               Path(wav_path).name))
    c.commit()
    c.close()
    seq = await QUEUE.enqueue(cid)
    return {"id": cid, "sequence": seq, "status": "queued"}


@app.get("/api/job/{job_id}/download")
async def api_download(job_id: str):
    row = job_get(job_id)
    if not row or not row["result_path"]:
        raise HTTPException(404, "no file")
    p = Path(row["result_path"])
    if not p.is_file():
        raise HTTPException(410, "file expired")
    media = {"wav": "audio/wav", "m4a": "audio/mp4", "mp3": "audio/mpeg"}.get(p.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(p, media_type=media, filename=p.name)


@app.post("/api/transcript/{job_id}")
async def api_save_transcript(job_id: str, body: dict):
    """Editable transcript saved back to the job."""
    row = job_get(job_id)
    if not row:
        raise HTTPException(404, "job not found")
    params = json.loads(row["params"] or "{}")
    params["transcript"] = str(body.get("text", ""))[:2_000_000]
    job_set(job_id, params=params)
    return {"id": job_id, "ok": True}


@app.get("/api/transcripts.txt")
async def api_transcripts_all():
    return await api_transcript_txt("__all__", all=True)


@app.get("/api/transcript/{job_id}.txt")
async def api_transcript_txt(job_id: str, all: bool = False):
    if job_id == "__all__":
        all = True
    c = db()
    if all:
        rows = c.execute("SELECT params FROM jobs WHERE kind='stt' AND status='completed' ORDER BY created DESC LIMIT 50").fetchall()
        c.close()
        if not rows:
            raise HTTPException(404, "no transcripts")
        buf = io.StringIO()
        for r in rows:
            p = json.loads(r["params"] or "{}")
            buf.write(f"=== {p.get('orig_name', 'audio')} ===\n\n{p.get('transcript', '')}\n\n")
        name = f"rudo-transcripts-{time.strftime('%Y-%m-%d-%H%M')}.txt"
        path = CONV_DIR / name
        path.write_text(buf.getvalue(), encoding="utf-8")
        return FileResponse(path, media_type="text/plain", filename=name)
    row = job_get(job_id)
    if not row:
        raise HTTPException(404, "job not found")
    params = json.loads(row["params"] or "{}")
    name = safe_name(row["filename"] or job_id[:8]) + ".txt"
    path = CONV_DIR / f"{uuid.uuid4().hex}.txt"
    path.write_text(params.get("transcript", ""), encoding="utf-8")
    return FileResponse(path, media_type="text/plain", filename=name)


# ---------------------------------------------------------------- UI
@app.get("/api/audio/{job_id}", name="audio_file")
async def audio_file(job_id: str):
    row = job_get(job_id)
    if not row or row["kind"] not in ("tts", "convert") or not row["result_path"]:
        raise HTTPException(404, "no audio")
    p = Path(row["result_path"])
    if not p.is_file():
        raise HTTPException(410, "file expired")
    media = {"wav": "audio/wav", "m4a": "audio/mp4", "mp3": "audio/mpeg"}.get(p.suffix.lstrip("."), "audio/wav")
    return FileResponse(p, media_type=media, filename=p.name)


@app.get("/api/stt/{job_id}", name="stt_json")
async def stt_json(job_id: str):
    row = job_get(job_id)
    if not row:
        raise HTTPException(404, "job not found")
    params = json.loads(row["params"] or "{}")
    dur = (row["finished"] - row["started"]) if (row["finished"] and row["started"]) else None
    return {"text": params.get("transcript", ""), "duration_s": dur, "raw": params.get("raw"),
            "filename": row["filename"], "status": row["status"]}


@app.post("/api/wake")
async def wake():
    """Zobuditie produkčného AI managera — pošle minimálny TTS request, čo aktivuje GPU worker."""
    try:
        async with httpx.AsyncClient(timeout=120) as cl:
            r = await cl.post(f"{MANAGER}/v1/audio/speech", json={"input": "Ahoj.", "normalize": False})
        if r.status_code != 200:
            raise HTTPException(503, f"manager wake HTTP {r.status_code}: {r.text[:200]}")
        return {"ok": True, "detail": "tts worker activated"}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"manager unavailable: {exc}")


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="sk"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rudo kričí – TTS / STT</title>
<link rel="stylesheet" href="/static/styles.css"></head>
<body>
<header class="topbar"><div class="brand"><span class="brand-mark">R</span><span>Rudo kričí</span></div><div class="ai-cluster">
<button id="wakeBtn" class="btn-wake" type="button" title="Zobuditie AI manager (aktivuje GPU worker)">Zobudiť AI</button>
<div id="mgr" class="mgr">AI stav: …</div></div></header>
<main class="shell">
<section class="queue-card" id="queuecard">
  <h2>GPU fronta <span class="pill" id="qstate">idle</span></h2>
  <div class="qgrid">
    <div><strong>Aktuálne</strong><div id="qcurrent">—</div></div>
    <div><strong>Poradie / čaká</strong><div id="qwaiting">—</div></div>
  </div>
</section>

<section class="card">
<h2>Text → hlas <span class="sub">TTS</span></h2>
<div class="voice-field"><label for="ttsVoice">Hlas</label>
<select id="ttsVoice" aria-describedby="ttsVoiceNote"><option value="rudo" selected>Rudo</option><option value="melisko">Melisko</option><option value="fico">Fico</option></select>
<span id="ttsVoiceNote" class="advNote">Predvolený hlas: Rudo. Predvolený seed: Rudo 42 · Melisko 0 · Fico 0. Prázdny seed použije predvolenú hodnotu hlasu na serveri.</span></div>
<textarea id="ttsText" rows="12" placeholder="Sem napíšte text, ktorý chcete previesť na hlas..."></textarea>
<label class="switch"><input type="checkbox" id="ttsNorm" checked> Normalizovať slovenský text</label>
<details class="adv"><summary>Pokročilé nastavenia</summary>
<div class="advNote">Hlas a voliteľný <code>seed</code> sa uložia pri odoslaní úlohy. Ostatné parametre generovania riadi manager; nepoužívame falošné ovládače.</div>
<label class="switch">Seed <input type="number" id="ttsSeed" placeholder="predvolený seed hlasu" min="0" max="2147483647" style="max-width:220px"></label>
</details>
<button class="primary-button" id="ttsGo">Generovať hlas</button>
<div id="ttsStatus" class="statusline">Stav: pripravené</div>
<div id="ttsOut"></div>
<div id="dlBox" class="dlbox hidden">
  <label>Formát <select id="dlFmt"><option value="wav">WAV (pôvodný)</option><option value="mp3">MP3</option><option value="aac">AAC</option></select></label>
  <label>Bitrate <select id="dlBit"><option>128k</option><option selected>192k</option><option>256k</option><option>320k</option></select></label>
  <label><input type="checkbox" id="dlVol"> Normalizovať hlasitosť (loudness)</label>
  <button class="primary-button" id="dlGo">Stiahnuť</button>
</div>
</section>

<section class="card">
<h2>Hlas → text <span class="sub">STT</span></h2>
<div id="drop" class="drop">Sem myši alebo vyber audio súbory (WAV, MP3, M4A, FLAC, OGG, …)
<input type="file" id="sttFiles" multiple accept=".wav,.mp3,.aac,.m4a,.flac,.ogg,.opus,.webm,.mp4,.wma" hidden></div>
<div id="sttList" class="sttlist"></div>
<button class="primary-button" id="sttGo">Spracovať</button>
<div id="sttStatus" class="statusline">Stav: pripravené</div>
<details class="adv"><summary>Pokročilé nastavenia</summary><div class="advNote">Prepisuje produkčný Parakeet wrapper — momentálne žiadne kvalitovo nastaviteľné parametre nie sú vystavené (bez timestampov). Nepoužívame falošné ovládače.</div></details>
<div id="sttResults"></div>
<button class="outline-button hidden" id="allTxt">Stiahnuť všetky TXT</button>
</section>

<section class="card">
<h2>Posledné úlohy</h2>
<table class="hist"><thead><tr><th>typ</th><th>súbor / text</th><th>stav</th><th>trvanie</th><th></th></tr></thead><tbody id="histBody"></tbody></table>
</section>
</main>
<script src="/static/app.js"></script>
</body></html>
"""

# (static files are served via mount below)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

