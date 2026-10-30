/* Rudo kričí — vanilla JS client */
const $ = (s) => document.querySelector(s);

let currentTts = null, currentSttMap = {}, lastQueueSig = "";

const esc = (s) => (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (n) => n == null ? "—" : (n + "s");
const voiceLabel = (voice) => voice === "melisko" ? "Melisko" : "Rudo";
const jobVoice = (j) => j.kind === "tts" ? " · " + voiceLabel(j.voice) : "";

function statusLabel(s) {
  return { queued: "Čaká vo fronte", processing: "Spracováva sa", completed: "Hotovo", failed: "Chyba", cancelled: "Zrušené" }[s] || s;
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

async function pollStatus() {
  try {
    const st = await api("/api/status");
    const m = st.manager || {};
    const prof = m.active_profile == null ? "Idle" : m.active_profile.toUpperCase();
    $("#mgr").innerHTML = `AI stav: <b>${m.status === "healthy" ? "Online" : "Degradované"}</b> · profil: <b>${prof}</b> · queue: <b>${m.queue_depth ?? 0}</b> · workers: <b>${esc(Object.entries(m.workers || {}).map(([k, v]) => k + "=" + v).join(" "))}</b>`;
    const wake = $("#wakeBtn");
    if (wake) { wake.disabled = wake.dataset.busy === "1"; }

    const q = st.queue || {};
    const cur = q.current;
    const wait = q.queued || [];
    $("#qcurrent").innerHTML = cur ? `<span class="qitem">#${cur.sequence} ${cur.kind.toUpperCase()}${jobVoice(cur)}${cur.filename ? " " + esc(cur.filename) : (cur.text_preview ? " „" + esc(cur.text_preview.slice(0, 48)) + "…“" : "")} <b class="${cur.status === 'processing' ? 'ok' : 'wait'}">${statusLabel(cur.status)}</b></span>` : "—";
    $("#qwaiting").innerHTML = wait.length ? wait.map(j => `<span class="qitem">#${j.sequence} ${j.kind.toUpperCase()}${jobVoice(j)} ${esc(j.filename || (j.text_preview ? "„" + esc(j.text_preview.slice(0, 44)) + "…“" : ""))}</span>`).join("") : "—";

    const busy = q.busy;
    $("#qstate").textContent = busy ? "pracuje" : (wait.length ? "čaká" : "idle");
    $("#qstate").className = "pill" + (busy ? " busy" : wait.length ? " busy" : "");
  } catch (e) {
    $("#mgr").innerHTML = `AI stav: <b class="bad"> nedostupný</b>`;
  }

  // TTS job state
  if (currentTts) {
    try {
      const j = await api("/api/job/" + currentTts);
      if (j.status === "completed" && j.result_path) {
        showTtsResult(j);
      } else if (j.status === "failed") {
        $("#ttsStatus").innerHTML = `Stav: <b class="bad">Chyba</b> — ${esc(j.error || "")}`;
        currentTts = null;
      } else {
        $("#ttsStatus").innerHTML = `Stav: <b class="wait">${statusLabel(j.status)}</b> · ${voiceLabel(j.voice)}`;
      }
    } catch (e) { currentTts = null; }
  }

  // STT job states
  const ids = Object.keys(currentSttMap);
  for (const id of ids) {
    try {
      const j = await api("/api/job/" + id);
      const meta = j.raw?.duration_s ?? j.duration_s;
      $("#sttRow_" + id).innerHTML = `<span class="name">${esc(j.filename)}</span><span>${statusLabel(j.status)}${j.kind === "stt" ? "" : ""}${meta ? " · " + fmt(meta) : ""}</span>`;
      if (j.status === "completed") {
        const tx = j.transcript ?? "";
        currentSttMap[id] = tx;
        $("#sttResults").innerHTML += `
          <div class="transcript" id="tr_${id}">
            <h3 style="margin:16px 0 6px;font-size:15px">${esc(j.filename)}</h3>
            <div class="meta">trvanie ${fmt(j.duration_s)} · stav dokončené</div>
            <textarea class="transcriptEdit" id="tedit_${id}">${esc(tx)}</textarea>
            <button class="outline-button" onclick="saveTx('${id}')">Uložiť úpravy</button>
            <a class="outline-button" href="/api/transcript/${id}.txt" style="text-decoration:none">Stiahnuť TXT</a>
          </div>`;
        $("#allTxt").classList.remove("hidden");
        delete currentSttMap[id];
      } else if (j.status === "failed" || j.status === "cancelled") {
        delete currentSttMap[id];
      }
    } catch (e) { }
  }

  pollHistory();
}

async function pollHistory() {
  try {
    const h = await api("/api/jobs?limit=20");
    const body = $("#histBody");
    if (!h.jobs) return;
    if (JSON.stringify(h.jobs.map(j => [j.id, j.status, j.voice])) === lastQueueSig) return;
    lastQueueSig = JSON.stringify(h.jobs.map(j => [j.id, j.status, j.voice]));
    body.innerHTML = h.jobs.map(j => `<tr>
      <td>${j.kind}${jobVoice(j)}</td>
      <td>${esc(j.filename || (j.text_preview ? "„" + esc(j.text_preview.slice(0, 50)) + "…“" : "—"))}</td>
      <td>${j.status === "completed" ? '<b class="ok">Hotovo</b>' : j.status === "failed" ? '<b class="bad">Chyba</b>' : '<b class="wait">' + statusLabel(j.status) + "</b>"}</td>
      <td>${fmt(j.duration_s)}</td>
      <td>${j.status === "completed" ? `<a class="outline-button" style="padding:6px 12px;font-size:12px" href="/api/job/${j.id}/download" target="_blank">Stiahnuť</a>` : ""}</td>
    </tr>`).join("") || "<tr><td colspan=5>žiadne úlohy</td></tr>";
  } catch (e) { }
}

function showTtsResult(j) {
  currentTts = null;
  $("#ttsStatus").innerHTML = `Stav: <b class="ok">Hotovo</b> · ${voiceLabel(j.voice)} · ${fmt(j.duration_s)}`;
  $("#ttsOut").innerHTML = `<audio controls src="/api/job/${j.id}/download"></audio>`;
  $("#dlBox").classList.remove("hidden");
  $("#dlBox").dataset.job = j.id;
  $("#dlBox").dataset.wav = "1";
}

$("#wakeBtn").addEventListener("click", async () => {
  const btn = $("#wakeBtn");
  if (btn.disabled) return;
  btn.dataset.busy = "1"; btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "Budia…";
  try {
    await api("/api/wake", { method: "POST" });
    btn.textContent = "Hotovo";
    setTimeout(() => { btn.textContent = orig; }, 1500);
    pollStatus();
  } catch (e) {
    btn.textContent = "Chyba: " + e.message.slice(0, 30);
    setTimeout(() => { btn.textContent = orig; }, 2000);
  } finally {
    delete btn.dataset.busy; btn.disabled = false;
  }
});

$("#ttsGo").addEventListener("click", async () => {
  const text = $("#ttsText").value.trim();
  if (!text) { alert("Zadajte text."); return; }
  try {
    // Static JS is served before an authorized restart refreshes INDEX_HTML.
    const voice = $("#ttsVoice")?.value || "rudo";
    const r = await api("/api/tts", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text, voice, normalize: $("#ttsNorm").checked, seed: $("#ttsSeed").value === "" ? null : parseInt($("#ttsSeed").value, 10) }) });
    currentTts = r.id;
    $("#ttsStatus").innerHTML = `Stav: <b class="wait">Čaká vo fronte (#${r.sequence})</b> · ${voiceLabel(r.voice || voice)}`;
  } catch (e) { $("#ttsStatus").innerHTML = `Stav: <b class="bad">Chyba — ${esc(e.message)}</b>`; }
});

const drop = $("#drop");
drop.addEventListener("click", () => $("#sttFiles").click());
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("hover"); });
drop.addEventListener("dragleave", () => drop.classList.remove("hover"));
drop.addEventListener("drop", (e) => {
  e.preventDefault(); drop.classList.remove("hover");
  onFiles(e.dataTransfer.files);
});
$("#sttFiles").addEventListener("change", (e) => onFiles(e.target.files));

let pendingFiles = [];
function onFiles(list) {
  for (const f of list) pendingFiles.push(f);
  $("#sttList").innerHTML = pendingFiles.map((f, i) => `<div class="sttrow" id="pf_${i}"><span class="name">${esc(f.name)}</span><span>na radu na odoslanie</span></div>`).join("");
}

$("#sttGo").addEventListener("click", async () => {
  if (!pendingFiles.length) { alert("Pridajte aspoň jeden súbor."); return; }
  const fd = new FormData();
  for (const f of pendingFiles) fd.append("files", f);
  pendingFiles = [];
  $("#sttList").innerHTML = "";
  try {
    const r = await api("/api/stt", { method: "POST", body: fd });
    $("#sttResults").innerHTML = "";
    r.jobs.forEach(j => {
      currentSttMap[j.id] = "";
      $("#sttList").innerHTML += `<div class="sttrow" id="sttRow_${j.id}"><span class="name">#${j.sequence} ${esc(j.filename)}</span><span>čaká</span></div>`;
    });
    $("#sttStatus").innerHTML = `Stav: <b class="wait">${r.jobs.length} úloh vo fronte</b>`;
  } catch (e) { $("#sttStatus").innerHTML = `Stav: <b class="bad">Chyba — ${esc(e.message)}</b>`; }
});

async function saveTx(id) {
  const tx = $("#tedit_" + id).value;
  try { await api("/api/transcript/" + id, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: tx }) });
    $("#tr_" + id + " .meta").textContent += " · upravené"; } catch (e) { }
}

// canonical production defaults (manager-owned, shown as reset values)
$("#advReset") && $("#advReset").addEventListener("click", () => {});


function wireSliders() {
  document.querySelectorAll(".advGrid label").forEach(label => {
    const num = label.querySelector('input[type=number]');
    const rng = label.querySelector('input[type=range]');
    num.addEventListener("input", () => { rng.value = num.value; });
    rng.addEventListener("input", () => { num.value = rng.value; });
  });
}
wireSliders();

// download flow: convert via queue then download (conversion is also a job)
$("#dlGo").addEventListener("click", async () => {
  const box = $("#dlBox");
  const jobId = box.dataset.job;
  if (!jobId) return;
  const fmtSel = $("#dlFmt").value;
  if (fmtSel === "wav") {
    window.location = "/api/job/" + jobId + "/download";
    return;
  }
  const body = { wav_path: "", format: fmtSel, bitrate: $("#dlBit").value, volume: $("#dlVol").checked ? -16 : null, sample_rate: null, channels: null };
  try {
    const j = await api("/api/job/" + jobId);
    body.wav_path = j.result_path;
    const r = await api("/api/jobs/" + jobId + "/convert", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    $("#ttsStatus").innerHTML = `Stav: <b class="wait">konverzia vo fronte (#${r.sequence})</b>`;
    pollConversion(r.id);
  } catch (e) {
    alert("Konverzia zlyhala: " + e.message);
  }
});

async function pollConversion(id) {
  const iv = setInterval(async () => {
    try {
      const j = await api("/api/job/" + id);
      if (j.status === "completed") {
        $("#ttsOut").innerHTML += ` <a class="outline-button" href="/api/job/${id}/download" style="margin-left:10px;text-decoration:none">Konvertovaný súbor ↓</a>`;
        clearInterval(iv);
      } else if (j.status === "failed") {
        alert("Konverzia zlyhala: " + (j.error || ""));
        clearInterval(iv);
      }
    } catch (e) { clearInterval(iv); }
  }, 1500);
}

$("#allTxt").addEventListener("click", () => { window.location = "/api/transcripts.txt"; });

pollStatus();
setInterval(pollStatus, 1800);
