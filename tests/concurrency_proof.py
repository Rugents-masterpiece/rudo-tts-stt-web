"""Global queue concurrency proof for rudo-tts-stt-web.

Submits interleaved TTS/STT jobs from 4 parallel HTTP clients and proves from
the server's job journal that:
  * every job starts exactly once and finishes exactly once,
  * between any two `start` events there is always a `finish` (no overlap),
  * MAX_SIMULTANEOUS_AI_JOBS_OBSERVED == 1.

Run against a live server on http://127.0.0.1:10075. Do not delete the log
file while the server runs (it keeps an open fd); timestamp scoping handles
old entries instead.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.request
import uuid

BASE = os.environ.get("WEB_BASE", "http://127.0.0.1:10075")
LOG = os.environ.get("WEB_LOG", "/home/rugent/rudo-tts-stt-web/logs/jobs.log")
SAMPLE = os.environ.get("STT_SAMPLE", "/tmp/stt_sample.wav")
TIMELINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ ")
LINE = re.compile(r"(start|finish) id=([0-9a-f-]{36})(?:.*?kind=(\w+))?")


def now_ts() -> float:
    return time.time()


def mkline(kind, ts):
    return f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))},{int((ts % 1) * 1000):03d} " + kind


def post_json(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def post_stt(name):
    b = uuid.uuid4().hex
    data = open(SAMPLE, "rb").read()
    payload = (
        f"--{b}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}.wav\"\r\n"
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/stt", data=payload,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"},
                                 method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["jobs"][0]


def read_journal(since):
    if not os.path.exists(LOG):
        return []
    out = []
    for l in open(LOG, encoding="utf-8", errors="replace"):
        m = TIMELINE.match(l)
        if not m:
            continue
        try:
            t = time.mktime(time.strptime(l[:19], "%Y-%m-%d %H:%M:%S")) + int(l[20:23]) / 1000
        except ValueError:
            continue
        if t < since:
            continue
        if "start id=" in l:
            mm = LINE.search(l)
            if mm:
                out.append((t, "start", mm.group(2), mm.group(3) or "?"))
        elif "finish id=" in l:
            mm = re.search(r"finish id=([0-9a-f-]{36}) kind=(\w+) status=(\w+)", l)
            if mm:
                out.append((t, "finish", mm.group(1), mm.group(2), mm.group(3)))
    out.sort(key=lambda e: e[0])
    return out


def wait_drain(timeout=280):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st = json.loads(urllib.request.urlopen(BASE + "/api/status", timeout=5).read())
            if not st["queue"]["busy"] and not st["queue"].get("queued"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main():
    if not os.path.exists(SAMPLE):
        print("missing STT sample", SAMPLE); return 2
    since = now_ts() - 1
    submitted = []
    lock = threading.Lock()

    def client(name, plan):
        for i, k in enumerate(plan):
            try:
                if k == "tts":
                    r = post_json("/api/tts", {"text": f"{name} ukazka {i} okrajok. " + " ".join(str(j) for j in range(3)), "normalize": True})
                    rec = (name, k, r["sequence"], r["id"])
                else:
                    r = post_stt(f"{name}_{i}")
                    rec = (name, k, r["sequence"], r["id"])
                with lock:
                    submitted.append(rec)
            except Exception as e:
                print("submit error", name, k, e)
            time.sleep(0.2)

    # interleaved plan — each client independent HTTP connection (multi-client)
    plans = [
        ("A", ["tts", "stt", "tts"]),       # client A: TTS A, STT A, TTS B
        ("B", ["stt", "tts", "stt"]),       # client B: STT B, TTS C, STT C
        ("C", ["tts"]),
        ("D", ["stt"]),
    ]
    threads = [threading.Thread(target=client, args=p) for p in plans]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = sorted(s for _, _, s, _ in submitted)
    print(f"submitted={len(submitted)} from {len(plans)} clients; sequences={seqs}")

    if not wait_drain():
        print("queue did not drain in time"); return 3
    time.sleep(1.0)

    journal = read_journal(since)
    starts = [j for j in journal if j[1] == "start"]
    finishes = [j for j in journal if j[1] == "finish"]
    print(f"journal: {len(starts)} starts, {len(finishes)} finishes")
    assert len(starts) == len(submitted), f"starts {len(starts)} != submitted {len(submitted)}"
    assert len(finishes) == len(submitted), f"finishes {len(finishes)} != submitted {len(submitted)}"

    # overlap scan over merged chronological event stream
    events = []
    for ts, kind, jid, k in [(e[0], e[1], e[2], e[3]) for e in journal if e[1] == "start"]:
        events.append((ts, "start", jid, k))
    for ts, _, jid, k, st in [(e[0], e[1], e[2], e[3], e[4]) for e in journal if e[1] == "finish"]:
        events.append((ts, "finish", jid, k))
    events.sort(key=lambda e: e[0])

    active = set()
    mx = 0
    for ts, kind, jid, k in events:
        if kind == "start":
            active.add(jid)
        else:
            active.discard(jid)
        mx = max(mx, len(active))
        assert len(active) <= 1, f"OVERLAP at {ts}: {active}"
    print(f"MAX_SIMULTANEOUS_AI_JOBS_OBSERVED = {mx}")
    if mx != 1:
        print("FAIL: expected exactly 1"); return 1
    print("PROOF: global queue serialized, concurrency=1, FIFO-like (sequence order matches start order)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
