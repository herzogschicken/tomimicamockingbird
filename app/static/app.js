"use strict";

const app = document.getElementById("app");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtTime = (sec) => {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
};

const fmtDur = (sec) => {
  sec = Math.round(sec || 0);
  if (sec < 60) return sec + " s";
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
};

const fmtSize = (b) => {
  if (b > 1e9) return (b / 1e9).toFixed(2) + " GB";
  if (b > 1e6) return (b / 1e6).toFixed(1) + " MB";
  return Math.round(b / 1e3) + " KB";
};

const STATUS = {
  not_transcribed: "Not transcribed",
  queued: "Queued",
  transcribing: "Transcribing",
  paused: "Paused",
  transcribed: "Transcribed",
  error: "Error",
  aligning: "Aligning",
  ready: "Ready",
  needs_review: "Needs review",
};

const PHASE = {
  queued: "Waiting in queue",
  checking_gpu: "Checking GPU",
  starting: "Starting",
  loading_model: "Loading model (first run downloads it)",
  copying_from_phone: "Copying audio from the phone",
  transcribing: "Transcribing",
  aligning: "Matching transcript to the EPUB",
};

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts = { ...opts, body: JSON.stringify(opts.json), headers: { "Content-Type": "application/json" } };
  }
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}

// One poll timer for whichever page is open.
let pollTimer = null;
function poll(fn, ms = 1000) {
  stopPoll();
  pollTimer = setInterval(fn, ms);
}
function stopPoll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

const isActive = (status) => status === "queued" || status === "transcribing" || status === "aligning";
const isDone = (status) => status === "transcribed" || status === "ready" || status === "needs_review";

// Overall fraction done for a job snapshot.
const jobFraction = (j) => j && j.total_audio ? (j.done_audio + (j.current_t || 0)) / j.total_audio : 0;

function jobLine(j) {
  if (!j) return "";
  const parts = [];
  if (j.phase === "copying_from_phone" && j.pull_total) {
    return `${PHASE[j.phase]} · ${Math.floor((j.pull_done / j.pull_total) * 100)}% of ${fmtSize(j.pull_total)}`;
  }
  if (j.phase !== "transcribing") parts.push(PHASE[j.phase] || j.phase);
  else parts.push(`Chapter ${j.current_idx + 1} of ${j.chapter_count}`);
  parts.push(`${Math.floor(jobFraction(j) * 100)}% done`);
  if (j.rate) parts.push(`${j.rate.toFixed(1)}x realtime`);
  if (j.eta_s != null) parts.push(`about ${fmtDur(j.eta_s)} left`);
  if (j.model) parts.push(j.model);
  if (j.device) parts.push(j.device === "cuda" ? `GPU (${j.compute_type}${j.batched ? ", batched" : ""})` : j.device === "cpu" ? "CPU" : j.device);
  return parts.join(" · ");
}

// ---------------- router ----------------

function route() {
  stopPoll();
  const r = location.hash.match(/^#\/read\/([a-z0-9]+)(\/highlights)?/);
  if (r) { Reader.open(r[1], app, { highlights: !!r[2] }); return; }
  Reader.close();
  const m = location.hash.match(/^#\/book\/([a-z0-9]+)/);
  if (m) showBook(m[1]);
  else showLibrary();
}
window.addEventListener("hashchange", route);

// ---------------- library ----------------

const chosen = { epub: null, m4b: null };

const picker = (key, label, accept, ph) => `
  <div class="picker">
    <div class="label">${label}</div>
    <label class="drop" id="drop-${key}">
      <input type="file" accept="${accept}" id="file-${key}" hidden>
      <span id="dropText-${key}">Drop file here or click to pick</span>
    </label>
    <input type="text" id="path-${key}" placeholder="or path on disk, e.g. ${ph}">
  </div>`;

async function showLibrary() {
  chosen.epub = chosen.m4b = null;
  app.innerHTML = `
    <section class="card">
      <h2>Add a book</h2>
      <div class="pickers">
        ${picker("epub", "EPUB", ".epub", "~/Books/title.epub")}
        ${picker("m4b", "Audiobook (.m4b)", ".m4b,.m4a", "~/Audiobooks/title.m4b")}
      </div>
      <p class="hint">Dropping or picking a file copies it into the app's data folder.
        Typing a path links to the file where it is, with no copy. Use a path for big .m4b files.</p>
      <div class="row">
        <button class="primary" id="addBtn">Add book</button>
        <div class="progress hidden" id="upProg"><div></div></div>
        <span id="addMsg"></span>
      </div>
    </section>
    <section class="card phone-card">
      <div class="row">
        <h2 style="margin:0">Import from phone</h2>
        <button class="primary" id="scanBtn">Scan phone</button>
        <span id="scanMsg" class="meta">Finds .m4b and .epub files on the phone over adb (USB or wireless).</span>
      </div>
      <div class="row phone-opts">
        <label>Search only in <input type="text" id="phFolders" placeholder="whole phone storage, or e.g. /sdcard/Audiobooks, /sdcard/Books"></label>
        <label><input type="checkbox" id="phDelete"> Delete the audio from the PC once transcribed</label>
        <label><input type="checkbox" id="phAuto"> Start transcribing right away</label>
      </div>
      <div id="phImports"></div>
      <div id="phList"></div>
    </section>
    <h2>Library</h2>
    <div class="health" id="health"></div>
    <div class="list" id="list"><div class="empty">Loading...</div></div>
    <details class="card settings" id="settingsBox">
      <summary>Transcription settings</summary>
      <div id="settingsBody" class="settings-body">Loading...</div>
    </details>`;

  for (const key of ["epub", "m4b"]) wirePicker(key);
  document.getElementById("addBtn").onclick = addBook;
  wirePhone();
  loadHealth();
  loadSettings();
  await loadList();
}

function wirePicker(key) {
  const drop = document.getElementById("drop-" + key);
  const input = document.getElementById("file-" + key);
  const path = document.getElementById("path-" + key);
  const text = document.getElementById("dropText-" + key);

  const setFile = (f) => {
    chosen[key] = f;
    if (f) {
      drop.classList.add("chosen");
      text.textContent = `${f.name} (${fmtSize(f.size)})`;
      path.value = "";
    } else {
      drop.classList.remove("chosen");
      text.textContent = "Drop file here or click to pick";
    }
  };
  input.onchange = () => setFile(input.files[0] || null);
  path.oninput = () => { if (path.value.trim()) setFile(null); };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  };
}

function addBook() {
  const msg = document.getElementById("addMsg");
  const btn = document.getElementById("addBtn");
  const prog = document.getElementById("upProg");
  const bar = prog.firstElementChild;
  const fd = new FormData();
  let uploading = false;

  for (const key of ["epub", "m4b"]) {
    const p = document.getElementById("path-" + key).value.trim();
    if (chosen[key]) { fd.append(key + "_file", chosen[key]); uploading = true; }
    else if (p) fd.append(key + "_path", p);
    else {
      msg.className = "msg-err";
      msg.textContent = key === "epub" ? "Pick an EPUB or type its path." : "Pick an audiobook or type its path.";
      return;
    }
  }

  btn.disabled = true;
  msg.className = "";
  msg.textContent = uploading ? "Uploading..." : "Reading files...";
  if (uploading) { prog.classList.remove("hidden"); bar.style.width = "0"; }

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/books");
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = (e.loaded / e.total) * 100;
    bar.style.width = pct + "%";
    if (pct >= 100) msg.textContent = "Reading chapters and parsing EPUB...";
  };
  xhr.onload = () => {
    btn.disabled = false;
    prog.classList.add("hidden");
    let j = {};
    try { j = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status >= 200 && xhr.status < 300) {
      const text = j.duplicate ? "This book is already in your library." : "Book added.";
      showLibrary().then(() => {
        const m = document.getElementById("addMsg");
        m.className = "msg-ok";
        m.textContent = text;
      });
    } else {
      msg.className = "msg-err";
      msg.textContent = j.detail || "Upload failed.";
    }
  };
  xhr.onerror = () => {
    btn.disabled = false;
    prog.classList.add("hidden");
    msg.className = "msg-err";
    msg.textContent = "Network error. Is the server running?";
  };
  xhr.send(fd);
}

// ---------------- import from phone ----------------

let phoneScan = null;
let importTimer = null;

async function wirePhone() {
  const folders = document.getElementById("phFolders");
  const del = document.getElementById("phDelete");
  const auto = document.getElementById("phAuto");
  try {
    const { settings: s } = await api("/api/settings");
    folders.value = s.phone_folders || "";
    del.checked = !!s.delete_audio_after;
    auto.checked = !!s.auto_transcribe;
  } catch (_) {}
  const save = (k, v) => api("/api/settings", { method: "PUT", json: { [k]: v } }).catch(() => {});
  folders.onchange = () => save("phone_folders", folders.value);
  del.onchange = () => save("delete_audio_after", del.checked);
  auto.onchange = () => save("auto_transcribe", auto.checked);
  document.getElementById("scanBtn").onclick = scanPhone;
  if (phoneScan) renderPhoneList();
  pollImports();
}

async function scanPhone() {
  const msg = document.getElementById("scanMsg");
  const btn = document.getElementById("scanBtn");
  btn.disabled = true;
  msg.className = "meta";
  msg.textContent = "Scanning the phone...";
  try {
    phoneScan = await api("/api/phone/files");
    const n = phoneScan.audiobooks.length;
    msg.textContent = `${n} audiobook${n === 1 ? "" : "s"} and ${phoneScan.epubs.length} EPUB${phoneScan.epubs.length === 1 ? "" : "s"} found.`;
    renderPhoneList();
  } catch (e) {
    msg.className = "msg-err";
    msg.textContent = e.message;
  }
  btn.disabled = false;
}

function renderPhoneList() {
  const el = document.getElementById("phList");
  if (!el || !phoneScan) return;
  if (!phoneScan.audiobooks.length) {
    el.innerHTML = `<p class="hint">No .m4b or .m4a files found${phoneScan.folders.length ? " in those folders" : ""}.</p>`;
    return;
  }
  const allEpubs = phoneScan.epubs;
  el.innerHTML = `<table class="phtable">
    <tr><th>Audiobook on the phone</th><th class="num">Size</th><th>EPUB</th><th></th></tr>
    ${phoneScan.audiobooks.map((a, i) => {
      const sug = a.epub_suggestions || [];
      const sugSet = new Set(sug.map((x) => x.path));
      const opts = sug.map((x, k) => `<option value="phone:${esc(x.path)}" ${k === 0 ? "selected" : ""}>${esc(x.name)}${k === 0 ? " (best match)" : ""}</option>`).join("") +
        (allEpubs.filter((e) => !sugSet.has(e.path)).length ? `<optgroup label="Other EPUBs on the phone">` +
          allEpubs.filter((e) => !sugSet.has(e.path)).map((e) => `<option value="phone:${esc(e.path)}">${esc(e.name)}</option>`).join("") + `</optgroup>` : "") +
        `<option value="pc" ${sug.length ? "" : "selected"}>EPUB on this PC...</option>`;
      return `<tr data-i="${i}">
        <td><b>${esc(a.name)}</b><div class="dim small">${esc(a.path)}</div></td>
        <td class="num">${fmtSize(a.size)}</td>
        <td><select class="phEpub">${opts}</select>
          <div class="phPc ${sug.length ? "hidden" : ""}"><input type="text" class="phPcPath" placeholder="path, e.g. ~/Books/title.epub">
            <input type="file" class="phPcFile" accept=".epub"></div></td>
        <td>${a.book_id ? `<a href="#/book/${a.book_id}">Already imported</a>` : `<button class="primary mini phGo">Import</button>`}</td></tr>`;
    }).join("")}</table>`;
  el.querySelectorAll("tr[data-i]").forEach((tr) => {
    const sel = tr.querySelector(".phEpub");
    sel.onchange = () => tr.querySelector(".phPc").classList.toggle("hidden", sel.value !== "pc");
    const go = tr.querySelector(".phGo");
    if (go) go.onclick = () => startImport(tr, phoneScan.audiobooks[Number(tr.dataset.i)]);
  });
}

async function startImport(tr, a) {
  const sel = tr.querySelector(".phEpub").value;
  const fd = new FormData();
  fd.append("audio_path", a.path);
  fd.append("audio_size", a.size);
  if (sel.startsWith("phone:")) {
    const p = sel.slice(6);
    fd.append("epub_phone", p);
    const e = phoneScan.epubs.find((x) => x.path === p);
    if (e) fd.append("epub_size", e.size);
  } else {
    const f = tr.querySelector(".phPcFile").files[0];
    const p = tr.querySelector(".phPcPath").value.trim();
    if (f) fd.append("epub_file", f);
    else if (p) fd.append("epub_path", p);
    else { alert("Pick the EPUB on the PC, or type its path."); return; }
  }
  const btn = tr.querySelector(".phGo");
  btn.disabled = true;
  try {
    await api("/api/phone/import", { method: "POST", body: fd });
    btn.textContent = "Importing...";
    pollImports();
  } catch (e) {
    alert(e.message);
    btn.disabled = false;
  }
}

async function pollImports() {
  clearTimeout(importTimer);
  const el = document.getElementById("phImports");
  if (!el) return;
  let jobs = [];
  try { jobs = await api("/api/phone/imports"); } catch (_) { return; }
  const recent = jobs.filter((j) => Date.now() / 1000 - j.started < 3600).sort((a, b) => b.started - a.started);
  el.innerHTML = recent.map((j) => {
    const pct = j.total ? Math.min(100, (j.done / j.total) * 100) : 0;
    const copying = j.phase.startsWith("copying");
    return `<div class="phjob">
      <div class="row"><b>${esc(j.name)}</b>
        <span class="meta ${j.error ? "msg-err" : ""}">${esc(j.error || j.phase)}${copying && j.total ? ` · ${Math.floor(pct)}% of ${fmtSize(j.total)}` : ""}</span>
        ${j.book_id ? `<a href="#/book/${j.book_id}">Open</a>` : ""}</div>
      ${copying ? `<div class="progress wide"><div style="width:${pct.toFixed(1)}%"></div></div>` : ""}</div>`;
  }).join("");
  // refresh the library once for each import that just finished
  let fresh = false;
  for (const j of recent) {
    if ((j.book_id || j.error) && !importsSeen.has(j.id)) { importsSeen.add(j.id); fresh = fresh || !!j.book_id; }
  }
  if (fresh) { loadList(); if (phoneScan) scanPhoneQuiet(); }
  if (recent.some((j) => !j.error && !j.book_id)) importTimer = setTimeout(pollImports, 800);
}

const importsSeen = new Set();

async function scanPhoneQuiet() {
  try { phoneScan = await api("/api/phone/files"); renderPhoneList(); } catch (_) {}
}

async function loadHealth() {
  const el = document.getElementById("health");
  try {
    const h = await api("/api/health");
    const pill = (ok, label, bad) => `<span class="pill ${ok ? "ok" : "bad"}">${ok ? label : bad}</span>`;
    el.innerHTML =
      pill(h.ffprobe, "ffprobe found", "ffprobe missing") +
      pill(h.aac_decoder, "AAC decoding ok", "no AAC decoder in ffmpeg") +
      pill(h.adb, "adb found", "adb missing") +
      `<span class="pill" id="gpuPill">GPU not checked</span>` +
      `<span class="pill" title="Data folder">${esc(h.data_dir)}</span>`;
    checkGpu();
  } catch (e) {
    el.innerHTML = `<span class="pill bad">Server unreachable</span>`;
  }
}

async function checkGpu() {
  const pill = document.getElementById("gpuPill");
  if (!pill) return;
  pill.textContent = "Checking GPU...";
  try {
    const g = await api("/api/gpu");
    pill.className = "pill " + (g.ok ? "ok" : "bad");
    pill.textContent = g.ok ? "GPU ready for transcription" : "GPU not usable";
    pill.title = g.ok ? `ctranslate2 ${g.ctranslate2}, faster-whisper ${g.faster_whisper}`
      : (g.hint || g.error || "");
  } catch (e) {
    pill.className = "pill bad";
    pill.textContent = "GPU check failed";
  }
}

async function loadSettings() {
  const body = document.getElementById("settingsBody");
  try {
    const { settings: s, models, compute_types, devices } = await api("/api/settings");
    const opts = (list, cur) => list.map((v) => `<option ${v === cur ? "selected" : ""}>${v}</option>`).join("");
    body.innerHTML = `
      <label>Model <select data-k="model">${opts(models, s.model)}</select></label>
      <label>Precision (GPU) <select data-k="compute_type">${opts(compute_types, s.compute_type)}</select></label>
      <label>Device <select data-k="device">${opts(devices, s.device)}</select></label>
      <label>Language <input type="text" data-k="language" value="${esc(s.language)}" placeholder="auto"></label>
      <label>Beam size <input type="text" data-k="beam_size" value="${s.beam_size}"></label>
      <label>Batched (GPU) <select data-k="batched"><option value="true" ${s.batched ? "selected" : ""}>On</option><option value="false" ${s.batched ? "" : "selected"}>Off</option></select></label>
      <label>Batch size <input type="text" data-k="batch_size" value="${s.batch_size}"></label>
      <span id="setMsg" class="hint"></span>
      <p class="hint"><b>auto</b> (recommended) picks for you: large-v3-turbo on an NVIDIA card, small on a
        computer without one. On a CPU, "base" is faster still, and "large-v3-turbo" is more accurate but slow.
        large-v3 is the most accurate and the slowest. Batched mode (GPU only) decodes many pieces at once and
        is much faster; lower the batch size if the card runs out of memory. Beam size 1 is faster. Language: leave empty to detect it from the first chapter, or type a code like "en".
        Changes apply to chapters transcribed from now on. Finished chapters are never redone.</p>`;
    body.querySelectorAll("[data-k]").forEach((el) => {
      el.onchange = async () => {
        const msg = document.getElementById("setMsg");
        try {
          const v = el.dataset.k === "batched" ? el.value === "true" : el.value;
          await api("/api/settings", { method: "PUT", json: { [el.dataset.k]: v } });
          msg.className = "msg-ok"; msg.textContent = "Saved.";
        } catch (e) {
          msg.className = "msg-err"; msg.textContent = e.message;
        }
      };
    });
  } catch (e) {
    body.innerHTML = `<span class="msg-err">${esc(e.message)}</span>`;
  }
}

function bookCard(b) {
  const j = b.job;
  const active = isActive(b.status);
  let progress = "";
  if (active && j) {
    progress = `
      <div class="progress wide"><div style="width:${(jobFraction(j) * 100).toFixed(1)}%"></div></div>
      <div class="meta">${esc(jobLine(j))}</div>`;
  } else if (b.transcribed_count > 0 && b.transcribed_count < b.chapter_count) {
    progress = `<div class="meta">${b.transcribed_count} of ${b.chapter_count} chapters transcribed</div>`;
  }
  const action = active
    ? `<button data-act="pause" data-id="${b.id}">Pause</button>`
    : isDone(b.status) && b.transcribed_count >= b.chapter_count
      ? ""
      : `<button class="primary" data-act="transcribe" data-id="${b.id}">${b.transcribed_count ? "Resume" : "Transcribe"}</button>`;
  return `
    <div class="card book" data-book="${b.id}">
      <div><span class="badge ${b.status}">${STATUS[b.status] || b.status}</span></div>
      <div class="title">${esc(b.title)}</div>
      <div class="author">${esc(b.author || "Unknown author")}</div>
      <div class="meta">${fmtTime(b.duration)} audio, ${b.chapter_count} chapters,
        ${b.section_count} EPUB sections, ${Number(b.word_count).toLocaleString()} words</div>
      ${progress}
      ${b.status === "error" ? `<div class="err-box">${esc(b.status_note)}</div>` : ""}
      ${b.status_note && b.status !== "error" ? `<div class="warn-line">${esc(b.status_note)}</div>` : ""}
      ${b.m4b_missing && b.phone_path ? `<div class="meta">Audio stays on the phone. It is copied over again if you re-transcribe.</div>`
        : b.m4b_missing && b.audio_removed ? `<div class="meta">Audio removed from this PC. Reading still works.</div>`
        : b.m4b_missing ? `<div class="warn-line">Audiobook file not found at its saved path.</div>` : ""}
      ${b.epub_missing ? `<div class="warn-line">EPUB file not found at its saved path.</div>` : ""}
      <div class="actions">
        ${b.status === "ready" || b.status === "needs_review" ? `<a href="#/read/${b.id}"><button class="primary">Read</button></a>` : ""}
        <a href="#/book/${b.id}"><button>Details</button></a>
        ${action}
        <button class="danger" data-act="del" data-id="${b.id}" data-title="${esc(b.title)}">Remove</button>
      </div>
    </div>`;
}

async function bookAction(btn, after) {
  const id = btn.dataset.id;
  try {
    if (btn.dataset.act === "transcribe") await api(`/api/books/${id}/transcribe`, { method: "POST" });
    else if (btn.dataset.act === "pause") await api(`/api/books/${id}/pause`, { method: "POST" });
    else if (btn.dataset.act === "del") {
      if (!confirm(`Remove "${btn.dataset.title}" from the library?\nLinked files on disk are not deleted.`)) return;
      await api("/api/books/" + id, { method: "DELETE" });
    }
  } catch (e) {
    alert(e.message);
  }
  after();
}

async function loadList() {
  const el = document.getElementById("list");
  if (!el) return;
  try {
    const books = await api("/api/books");
    if (!books.length) {
      el.innerHTML = `<div class="empty">No books yet. Add an EPUB and its audiobook above.</div>`;
    } else {
      el.innerHTML = books.map(bookCard).join("");
      el.querySelectorAll("[data-act]").forEach((btn) => { btn.onclick = () => bookAction(btn, loadList); });
    }
    if (books.some((b) => isActive(b.status))) poll(loadList, 1500);
    else stopPoll();
  } catch (e) {
    el.innerHTML = `<div class="empty msg-err">${esc(e.message)}</div>`;
  }
}

// ---------------- book detail ----------------

let currentBook = null;
let preview = null; // {kind: "section"|"chapter", idx}
let chapterView = "aligned"; // "aligned" | "heard"
let mapOpen = null; // chapter idx whose mapping editor is open

const MATCH = {
  ok: ["ok", "Matched"],
  low: ["low", "Low match, check it"],
  unmatched: ["bad", "No match, check it"],
  no_text: ["muted", "No book text (credits, intro)"],
  excluded: ["muted", "Set to no text"],
  not_transcribed: ["muted", "Not transcribed"],
};

async function showBook(id) {
  app.innerHTML = `<div class="empty">Loading...</div>`;
  let b;
  try { b = await api("/api/books/" + id); }
  catch (e) { app.innerHTML = `<div class="empty msg-err">${esc(e.message)}</div>`; return; }
  currentBook = b;
  preview = null;
  mapOpen = null;

  const totalWords = b.sections.reduce((n, s) => n + s.word_count, 0);
  app.innerHTML = `
    <div class="book-head">
      <div>
        <a href="#/">&larr; Library</a>
        <h1>${esc(b.title)}</h1>
        <div class="sub">${esc(b.author || "Unknown author")}
          <span class="badge ${b.status}" id="bkBadge" style="margin-left:8px">${STATUS[b.status] || b.status}</span>
          <a href="#/read/${b.id}" id="readBtn" class="${b.alignment ? "" : "hidden"}"><button class="primary" style="margin-left:12px">Open reader</button></a>
          <a href="#/read/${b.id}/highlights" id="hlBtn" class="${b.alignment ? "" : "hidden"}"><button style="margin-left:6px">Highlights</button></a></div>
      </div>
      <div class="paths">
        ${b.phone_path ? `Audio (from phone): ${esc(b.phone_path)}${b.m4b_missing ? " <span class='dim'>(PC copy removed after transcription)</span>" : ""}`
          : `Audio (${b.m4b_mode}): ${esc(b.m4b_path)}${b.m4b_missing ? (b.audio_removed ? " <span class='dim'>(removed from this PC)</span>" : " <b class='msg-err'>missing</b>") : ""}`}
        ${b.audio_in_app && isDone(b.status) ? `<button class="mini" id="rmAudioBtn" title="Delete the app's copy of the audio. Reading keeps working.">Remove audio from PC</button>` : ""}<br>
        EPUB (${b.epub_mode}): ${esc(b.epub_path)}${b.epub_missing ? " <b class='msg-err'>missing</b>" : ""}
      </div>
    </div>
    <section class="card tx" id="txCard"></section>
    <section class="card tx" id="alCard"></section>
    <div class="cols">
      <section class="card scroll">
        <h2>Audio chapters (${b.chapters.length}, ${fmtTime(b.duration)})</h2>
        <table id="chTable"></table>
      </section>
      <section class="card scroll">
        <h2>EPUB sections (${b.sections.length}, ${totalWords.toLocaleString()} words)</h2>
        <table>
          <tr><th class="num">#</th><th>Title</th><th class="num">Words</th></tr>
          ${b.sections.map((s) => `
            <tr class="sec ${s.word_count < 40 ? "dim" : ""}" data-sec="${s.idx}">
            <td class="num">${s.idx + 1}</td><td>${esc(s.title)}</td>
            <td class="num">${s.word_count.toLocaleString()}</td></tr>`).join("")}
        </table>
      </section>
      <section class="card scroll preview">
        <h2 id="pvTitle">Preview</h2>
        <div class="body" id="pvBody"><div class="placeholder">Click an EPUB section to preview its text,
          or a transcribed audio chapter to check how it matched.</div></div>
      </section>
    </div>`;

  app.querySelectorAll("tr[data-sec]").forEach((tr) => {
    tr.onclick = () => selectRow(tr, () => previewSection(id, Number(tr.dataset.sec)));
  });
  renderBook(b);
  const rm = document.getElementById("rmAudioBtn");
  if (rm) rm.onclick = async () => {
    if (!confirm("Delete the app's copy of the audio for this book?\n\nReading keeps working. " +
      "You only need the audio again if you want to re-transcribe.")) return;
    try {
      const r = await api(`/api/books/${b.id}/remove-audio`, { method: "POST" });
      alert(`Removed. ${fmtSize(r.freed)} freed.`);
      showBook(b.id);
    } catch (e) { alert(e.message); }
  };
  const firstReal = b.sections.find((s) => s.word_count >= 300) || b.sections[0];
  if (firstReal) app.querySelector(`tr[data-sec="${firstReal.idx}"]`)?.click();
  if (isActive(b.status)) poll(() => refreshBook(id), 1000);
}

function selectRow(tr, load) {
  app.querySelectorAll("tr.active").forEach((r) => r.classList.remove("active"));
  tr.classList.add("active");
  load();
}

async function refreshBook(id) {
  if (!document.getElementById("txCard")) return stopPoll();
  try {
    const b = await api("/api/books/" + id);
    const wasActive = isActive(currentBook.status);
    currentBook = b;
    renderBook(b);
    if (!isActive(b.status)) {
      stopPoll();
      if (wasActive && preview && preview.kind === "chapter") previewChapter(id, preview.idx);
    }
  } catch (_) {}
}

function renderBook(b) {
  document.getElementById("readBtn")?.classList.toggle("hidden", !b.alignment);
  document.getElementById("hlBtn")?.classList.toggle("hidden", !b.alignment);
  const badge = document.getElementById("bkBadge");
  badge.className = "badge " + b.status;
  badge.textContent = STATUS[b.status] || b.status;
  renderTranscription(b);
  renderAlignment(b);
  renderChapters(b);
}

function renderTranscription(b) {
  const card = document.getElementById("txCard");
  const j = b.job;
  const doneSet = new Set(Object.keys(b.transcripts).map(Number));
  const doneN = doneSet.size;
  const active = isActive(b.status);
  const complete = doneN >= b.chapters.length;
  const frac = active && j ? jobFraction(j)
    : b.chapters.reduce((n, c) => n + (doneSet.has(c.idx) ? c.end - c.start : 0), 0) / (b.duration || 1);

  let line;
  if (active && j) line = jobLine(j);
  else if (b.status === "aligning") line = "Matching transcript to the EPUB...";
  else if (complete) {
    const wall = Object.values(b.transcripts).reduce((n, t) => n + (t.wall_s || 0), 0);
    const words = Object.values(b.transcripts).reduce((n, t) => n + t.word_count, 0);
    line = `All ${doneN} chapters done. ${words.toLocaleString()} words heard, took ${fmtDur(wall)}.`;
  } else if (doneN) line = `${doneN} of ${b.chapters.length} chapters done. Resume picks up where it stopped.`;
  else line = "Not transcribed yet. This runs once per book and the result is kept.";

  const btn = active
    ? (b.status === "aligning" ? "" : `<button id="txBtn" data-act="pause" data-id="${b.id}">Pause</button>`)
    : complete ? `<button id="reTxBtn" title="Transcribe again with the current settings">Re-transcribe</button>`
      : `<button id="txBtn" class="primary" data-act="transcribe" data-id="${b.id}">${doneN ? "Resume" : "Transcribe"}</button>`;

  card.innerHTML = `
    <div class="row">
      <h2 style="margin:0">Transcription</h2>
      ${btn}
      <span class="meta">${esc(line)}</span>
    </div>
    <div class="progress wide"><div style="width:${(frac * 100).toFixed(1)}%"></div></div>
    ${b.status === "error" ? `<div class="err-box">${esc(b.status_note)}</div>` : ""}
    ${(j && j.note) || (b.status_note && b.status !== "error")
      ? `<div class="warn-line">${esc((j && j.note) || b.status_note)}</div>` : ""}
    ${runsTable(b)}`;
  const rb = document.getElementById("reTxBtn");
  if (rb) rb.onclick = async () => {
    if (!confirm("Transcribe this book again with the current settings?\n\nThe old transcript is replaced. " +
      "Its speed and match numbers are kept below so you can compare. Highlights and your position stay.")) return;
    try { await api(`/api/books/${b.id}/retranscribe`, { method: "POST" }); } catch (e) { alert(e.message); }
    await refreshBook(b.id);
    poll(() => refreshBook(b.id), 1000);
  };
  const tb = document.getElementById("txBtn");
  if (tb) tb.onclick = () => bookAction(tb, async () => {
    await refreshBook(b.id);
    if (isActive(currentBook.status)) poll(() => refreshBook(b.id), 1000);
  });
}

function runsTable(b) {
  const rows = [...(b.runs || []).map((r) => ({ ...r, label: "Earlier" }))];
  if (b.current_run) rows.push({ ...b.current_run, label: "Current" });
  if (rows.length < 2) return "";
  const pct = (x) => (x == null ? "-" : (x * 100).toFixed(1) + "%");
  return `<details class="runs" open><summary>Runs compared</summary><table>
    <tr><th></th><th>Model</th><th>Mode</th><th class="num">Took</th><th class="num">Speed</th>
      <th class="num">Words heard</th><th class="num">Words timed</th><th class="num">Chapters OK</th><th class="num">To check</th></tr>
    ${rows.map((r) => `<tr class="${r.label === "Current" ? "cur" : ""}"><td>${r.label}</td><td>${esc(r.model)}</td>
      <td>${r.device === "cuda" ? "GPU" : esc(r.device || "")}${r.batched ? ", batched" : ""}${r.beam_size ? `, beam ${r.beam_size}` : ""}</td>
      <td class="num">${fmtDur(r.wall_s)}</td><td class="num">${r.speed ? r.speed + "x" : "-"}</td>
      <td class="num">${Number(r.words_heard || 0).toLocaleString()}</td><td class="num">${pct(r.timed_ratio)}</td>
      <td class="num">${r.chapters_ok ?? "-"}</td><td class="num">${r.chapters_review ?? "-"}</td></tr>`).join("")}
  </table></details>`;
}

function renderAlignment(b) {
  const card = document.getElementById("alCard");
  const al = b.alignment;
  const doneN = Object.keys(b.transcripts).length;
  if (!doneN) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const busy = isActive(b.status);

  let body;
  if (!al) {
    body = `<span class="meta">Not aligned yet. It runs by itself when transcription finishes.
      You can also align the chapters that are done now.</span>`;
  } else {
    const s = al.summary;
    const parts = [`${(s.timed_ratio * 100).toFixed(1)}% of book words have a time`,
      `${s.chapters_ok} chapters matched`];
    if (s.chapters_review) parts.push(`<b class="warnc">${s.chapters_review} to check</b>`);
    if (s.chapters_no_text) parts.push(`${s.chapters_no_text} with no book text`);
    if (s.chapters_missing) parts.push(`${s.chapters_missing} not transcribed yet`);
    body = `<span class="meta">${parts.join(" · ")}</span>`;
  }
  const gaps = al ? al.gaps : [];
  card.innerHTML = `
    <div class="row">
      <h2 style="margin:0">Alignment</h2>
      <button id="alBtn" ${busy ? "disabled" : ""}>${al ? "Re-run" : "Align now"}</button>
      ${body}
      <span id="alMsg" class="meta"></span>
    </div>
    ${b.status === "needs_review" ? `<p class="hint">Some chapters matched poorly. Usually the EPUB and audio
      differ there (another edition, an abridgement), or a chapter is not in the book. Click a chapter to look,
      then use Map to point it at the right sections or mark it as having no book text.</p>` : ""}
    ${gaps.length ? `<details class="gaps"><summary>${gaps.length} gap${gaps.length > 1 ? "s" : ""} found</summary>
      <ul>${gaps.map((g, i) => `<li data-gap="${i}">${g.type === "text_not_heard"
        ? (g.whole_section
          ? `<b>Section not narrated</b>: "${esc(sectionTitle(b, g.sec))}" (${g.words} words). Normal for notes or appendices.`
          : `<b>Text not heard</b>: ${g.words} words in "${esc(sectionTitle(b, g.sec))}" around ${fmtTime(g.time)}`)
        : `<b>Audio not in book</b>: about ${g.words} words in chapter ${g.chapter + 1} at ${fmtTime(g.time)}`}
        <span class="dim">"${esc(g.preview)}..."</span></li>`).join("")}</ul></details>` : ""}`;
  document.getElementById("alBtn").onclick = async () => {
    const msg = document.getElementById("alMsg");
    msg.textContent = "Aligning...";
    try {
      const r = await api(`/api/books/${b.id}/align`, { method: "POST" });
      msg.textContent = `Done in ${r.seconds} s.`;
    } catch (e) { msg.className = "msg-err"; msg.textContent = e.message; }
    await refreshBook(b.id);
    if (preview && preview.kind === "chapter") previewChapter(b.id, preview.idx);
  };
  card.querySelectorAll("[data-gap]").forEach((li) => {
    li.onclick = () => {
      const g = gaps[Number(li.dataset.gap)];
      if (g.type === "text_not_heard") {
        const tr = app.querySelector(`tr[data-sec="${g.sec}"]`);
        if (tr) selectRow(tr, () => previewSection(b.id, g.sec));
      } else {
        const tr = app.querySelector(`tr[data-ch="${g.chapter}"]`);
        if (tr) selectRow(tr, () => previewChapter(b.id, g.chapter));
      }
    };
  });
}

const sectionTitle = (b, idx) => (b.sections[idx] || {}).title || `Section ${idx + 1}`;

function epubRange(b, c) {
  if (!c || !c.epub_start) return `<span class="dim">-</span>`;
  const a = c.epub_start[0], z = c.epub_end[0];
  const t = esc(sectionTitle(b, a));
  return a === z ? t : `${t} <span class="dim">to</span> ${esc(sectionTitle(b, z))}`;
}

function renderChapters(b) {
  const table = document.getElementById("chTable");
  const j = b.job;
  const active = isActive(b.status);
  const running = active && j && j.phase === "transcribing" ? j.current_idx : null;
  const alCh = {};
  (b.alignment ? b.alignment.chapters : []).forEach((c) => { alCh[c.idx] = c; });

  const secOpts = (sel) => b.sections.map((s) =>
    `<option value="${s.idx}" ${s.idx === sel ? "selected" : ""}>${s.idx + 1}. ${esc(s.title).slice(0, 50)}</option>`).join("");

  table.innerHTML = `
    <tr><th class="num">#</th><th>Title</th><th class="num">Start</th><th class="num">Len</th>
      <th class="num">Text</th><th>Match</th></tr>
    ${b.chapters.map((c) => {
      const t = b.transcripts[c.idx];
      const a = alCh[c.idx];
      let state = `<span class="dim">-</span>`;
      if (t) state = `<span class="msg-ok" title="${t.word_count} words, ${t.model}">&#10003;</span>`;
      else if (c.idx === running) state = `<span class="run">${Math.floor((j.current_t / (j.current_len || 1)) * 100)}%</span>`;
      let match = "";
      if (a && a.status && a.status !== "not_transcribed") {
        const [cls, label] = MATCH[a.status] || ["muted", a.status];
        const pct = a.ratio != null && (a.status === "ok" || a.status === "low") ? ` ${Math.round(a.ratio * 100)}%` : "";
        match = `<span class="mtag ${cls}" title="${label}">${a.status === "ok" ? "&#10003;" : label.split(",")[0]}${pct}</span>
          ${a.mode !== "auto" ? `<span class="dim" title="Set by hand">(manual)</span>` : ""}
          <div class="erange">${epubRange(b, a)}</div>`;
      }
      const mapBtn = t && b.alignment ? `<button class="mini" data-map="${c.idx}">Map</button>` : "";
      const row = `<tr class="${t ? "sec" : ""} ${preview && preview.kind === "chapter" && preview.idx === c.idx ? "active" : ""}" data-ch="${c.idx}">
        <td class="num">${c.idx + 1}</td><td>${esc(c.title)}</td>
        <td class="num">${fmtTime(c.start)}</td><td class="num">${fmtTime(c.end - c.start)}</td>
        <td class="num">${state}</td><td>${match} ${mapBtn}</td></tr>`;
      if (mapOpen !== c.idx) return row;
      const cm = b.chapter_map[c.idx] || { mode: "auto" };
      const s0 = cm.sec_start ?? (a && a.epub_start ? a.epub_start[0] : 0);
      const s1 = cm.sec_end ?? (a && a.epub_end ? a.epub_end[0] : s0);
      return row + `<tr class="maprow"><td></td><td colspan="5">
        <div class="mapbox">
          <label><input type="radio" name="mm" value="auto" ${cm.mode === "auto" ? "checked" : ""}> Find it automatically</label>
          <label><input type="radio" name="mm" value="range" ${cm.mode === "range" ? "checked" : ""}> Only match these sections:</label>
          <div class="row"><select id="mapFrom">${secOpts(s0)}</select> <span class="dim">to</span> <select id="mapTo">${secOpts(s1)}</select></div>
          <label><input type="radio" name="mm" value="none" ${cm.mode === "none" ? "checked" : ""}> No book text (credits, intro, ads)</label>
          <div class="row"><button class="primary mini" id="mapSave">Save and re-align</button>
            <button class="mini" id="mapCancel">Cancel</button><span id="mapMsg" class="meta"></span></div>
        </div></td></tr>`;
    }).join("")}`;

  table.querySelectorAll("tr.sec[data-ch]").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.closest("button")) return;
      selectRow(tr, () => previewChapter(b.id, Number(tr.dataset.ch)));
    };
  });
  table.querySelectorAll("[data-map]").forEach((btn) => {
    btn.onclick = () => { mapOpen = mapOpen === Number(btn.dataset.map) ? null : Number(btn.dataset.map); renderChapters(b); };
  });
  const save = document.getElementById("mapSave");
  if (save) {
    document.getElementById("mapCancel").onclick = () => { mapOpen = null; renderChapters(b); };
    save.onclick = async () => {
      const mode = table.querySelector("input[name=mm]:checked").value;
      const body = { mode };
      if (mode === "range") {
        body.sec_start = Number(document.getElementById("mapFrom").value);
        body.sec_end = Number(document.getElementById("mapTo").value);
        if (body.sec_end < body.sec_start) [body.sec_start, body.sec_end] = [body.sec_end, body.sec_start];
      }
      const msg = document.getElementById("mapMsg");
      msg.textContent = "Saving and re-aligning...";
      try {
        const idx = mapOpen;
        await api(`/api/books/${b.id}/chapters/${idx}/map`, { method: "PUT", json: body });
        mapOpen = null;
        await refreshBook(b.id);
        previewChapter(b.id, idx);
      } catch (e) { msg.className = "msg-err"; msg.textContent = e.message; }
    };
  }
}

async function previewSection(id, idx) {
  preview = { kind: "section", idx };
  const body = document.getElementById("pvBody");
  const title = document.getElementById("pvTitle");
  body.innerHTML = `<div class="placeholder">Loading...</div>`;
  try {
    const s = await api(`/api/books/${id}/sections/${idx}`);
    title.textContent = s.title;
    body.innerHTML = s.blocks.map((bl) => {
      if (bl.type === "img") return `<img src="/api/books/${id}/images/${encodeURIComponent(bl.src)}" alt="${esc(bl.alt)}">`;
      const tag = /^h[1-6]$/.test(bl.type) ? bl.type : bl.type === "li" ? "li" : "p";
      return `<${tag}>${esc(bl.text)}</${tag}>`;
    }).join("") || `<div class="placeholder">This section has no text.</div>`;
    body.parentElement.scrollTop = 0;
  } catch (e) {
    body.innerHTML = `<div class="placeholder msg-err">${esc(e.message)}</div>`;
  }
}

function viewToggle(id, idx, hasAligned) {
  if (!hasAligned) return "";
  return `<div class="toggle">
    <button class="mini ${chapterView === "aligned" ? "on" : ""}" data-view="aligned">Book text with times</button>
    <button class="mini ${chapterView === "heard" ? "on" : ""}" data-view="heard">What Whisper heard</button></div>`;
}

function wireToggle(id, idx) {
  document.querySelectorAll("[data-view]").forEach((btn) => {
    btn.onclick = () => { chapterView = btn.dataset.view; previewChapter(id, idx); };
  });
}

async function previewChapter(id, idx) {
  preview = { kind: "chapter", idx };
  const body = document.getElementById("pvBody");
  const title = document.getElementById("pvTitle");
  const b = currentBook;
  const a = b && b.alignment ? b.alignment.chapters.find((c) => c.idx === idx) : null;
  const hasAligned = !!(a && a.epub_start);
  body.innerHTML = `<div class="placeholder">Loading...</div>`;
  try {
    if (hasAligned && chapterView === "aligned") {
      const r = await api(`/api/books/${id}/aligned/${idx}`);
      title.textContent = `Matched text: ${b.chapters[idx].title}`;
      body.innerHTML = viewToggle(id, idx, true) + `
        <p class="hint">Hover a word to see its time. <span class="k-M">Matched</span> ·
          <span class="k-F">heard slightly differently</span> · <span class="k-I">time estimated</span> ·
          <span class="k-N">no time (not narrated)</span>. Faded text belongs to a neighbouring chapter.</p>` +
        renderAligned(id, r);
      body.querySelectorAll("[data-t]").forEach((w) => {
        w.title = fmtTime(Number(w.dataset.t)) + "." + String(Math.round((Number(w.dataset.t) % 1) * 10));
      });
    } else {
      const t = await api(`/api/books/${id}/transcript/${idx}`);
      title.textContent = `Heard: ${t.title}`;
      body.innerHTML = viewToggle(id, idx, hasAligned) +
        `<p class="hint">Whisper ${esc(t.model)} on ${esc(t.device)}, language ${esc(t.language)}.
          Times are from the start of the chapter.</p>` +
        (t.segments.map((s) => `<p><span class="ts">${fmtTime(s[0])}</span>${esc(s[2])}</p>`).join("")
          || `<div class="placeholder">No speech found in this chapter.</div>`);
    }
    wireToggle(id, idx);
    body.parentElement.scrollTop = 0;
  } catch (e) {
    body.innerHTML = viewToggle(id, idx, hasAligned) + `<div class="placeholder msg-err">${esc(e.message)}</div>`;
    wireToggle(id, idx);
  }
}

function renderAligned(id, r) {
  const [s0, w0] = r.chapter.epub_start;
  const [s1, w1] = r.chapter.epub_end;
  const parts = [];
  for (const sec of r.sections) {
    let wi = 0;
    for (const bl of sec.blocks) {
      if (bl.type === "img") {
        parts.push(`<img src="/api/books/${id}/images/${encodeURIComponent(bl.src)}" alt="${esc(bl.alt)}">`);
        continue;
      }
      const tag = /^h[1-6]$/.test(bl.type) ? bl.type : bl.type === "li" ? "li" : "p";
      const words = bl.text.split(" ").map((w) => {
        const i = wi++;
        const outside = (sec.idx === s0 && i < w0) || (sec.idx === s1 && i > w1);
        const t = sec.t[i];
        const k = sec.k[i] || "N";
        return `<span class="k-${k}${outside ? " out" : ""}"${t != null ? ` data-t="${t}"` : ""}>${esc(w)}</span>`;
      });
      parts.push(`<${tag}>${words.join(" ")}</${tag}>`);
    }
  }
  return parts.join("");
}

// Start only after every declaration above exists.
route();
