"use strict";
/* Reader: shows the EPUB, highlights the line being narrated, keeps it in view.

   Position source (whole-file seconds):
   - phone: Smart Audiobook Player over adb, polled every second, extrapolated between polls
   - manual clock: anchor + elapsed wall time x speed
   Mode "auto" follows the phone when it answers and falls back to the manual clock from the
   last known position when it doesn't, so a dropped link never loses your place.
*/
const Reader = (() => {
  const LS = "tmm.reader.";
  const prefs = {
    fs: 24, col: 68, theme: "oldbook", wordHL: false, syncMode: null,
    lh: 1.75, ws: 0, ps: 0.85, font: "",
    ...JSON.parse(localStorage.getItem(LS + "prefs") || "{}"),
  };
  // The look is saved in the browser and on the server, so it survives cleared browser data
  // and is the same in any browser on this PC.
  const LOOK_KEYS = ["theme", "fs", "col", "lh", "ws", "ps", "font", "wordHL"];
  let looks = {};                   // saved named looks: {name: {theme, fs, ...}}
  let prefsTimer = null;
  const pickLook = (src) => Object.fromEntries(LOOK_KEYS.filter((k) => k in src).map((k) => [k, src[k]]));
  function savePrefs() {
    try { localStorage.setItem(LS + "prefs", JSON.stringify(prefs)); } catch (_) {}
    clearTimeout(prefsTimer);
    prefsTimer = setTimeout(saveReaderConfig, 600);
  }
  function saveReaderConfig() {
    fetch("/api/reader/config", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prefs: pickLook(prefs), looks }),
    }).catch(() => {});
  }
  async function loadReaderConfig() {
    try {
      const c = await (await fetch("/api/reader/config")).json();
      if (c.prefs) Object.assign(prefs, pickLook(c.prefs));
      looks = c.looks || {};
    } catch (_) {}
  }

  let data = null, bookId = null, root = null;
  // per global word (reading order across the whole book)
  let wSec, wIdx, wTime;            // Int32Array, Int32Array, Float64Array (NaN = no time)
  let timedIdx, timedT;             // timed words only, in time order
  let secFirst = [];                // global index of each section's first word
  // current render
  let curSec = -1, spans = [], lines = [], lineOf = null;
  let curWord = -1, curLine = -1;
  // sync
  const clock = { pos: 0, wall: 0, playing: false, speed: 1 };
  const phone = { s: null, recv: 0, ok: false, err: "", lastOk: 0 };
  let mode = "auto";                // auto | phone | manual
  let source = "manual";            // what is driving right now: phone | manual
  let raf = null, pollTimer = null, saveTimer = null, statusTimer = null, lastTick = 0;
  let userScrollUntil = 0, lastSaved = -1;
  let highlights = [];              // user highlights for this book
  let zen = false;                  // fullscreen text-only mode
  let fontsLoaded = false;
  const TEXT_DEFAULTS = { lh: 1.75, ws: 0, ps: 0.85, font: "" };
  const HL_COLORS = ["yellow", "green", "blue", "pink"];

  const $ = (s) => root.querySelector(s);
  const now = () => performance.now();

  // ---------------- time helpers ----------------

  function fmt(sec, tenths = false) {
    const neg = sec < 0; sec = Math.abs(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    let out = (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(s).padStart(2, "0");
    if (tenths) out += "." + Math.floor((sec % 1) * 10);
    return (neg ? "-" : "") + out;
  }

  function parseTime(str) {
    str = String(str).trim();
    if (!str) return null;
    if (/^\d+(\.\d+)?$/.test(str)) return Number(str);
    const parts = str.split(":").map(Number);
    if (parts.some((x) => Number.isNaN(x)) || parts.length > 3) return null;
    return parts.reduce((acc, x) => acc * 60 + x, 0);
  }

  // ---------------- data ----------------

  function index() {
    const n = data.sections.reduce((acc, s) => acc + (data.times[s.idx] || []).length, 0);
    wSec = new Int32Array(n); wIdx = new Int32Array(n); wTime = new Float64Array(n);
    let g = 0;
    secFirst = [];
    for (const s of data.sections) {
      secFirst[s.idx] = g;
      const t = data.times[s.idx] || [];
      for (let i = 0; i < t.length; i++, g++) {
        wSec[g] = s.idx; wIdx[g] = i; wTime[g] = t[i] == null ? NaN : t[i];
      }
    }
    // timed words, kept non-decreasing in time
    const idx = [], tt = [];
    let last = -Infinity;
    for (let i = 0; i < n; i++) {
      const t = wTime[i];
      if (Number.isNaN(t) || t < last) continue;
      idx.push(i); tt.push(t); last = t;
    }
    timedIdx = Int32Array.from(idx);
    timedT = Float64Array.from(tt);
  }

  // last timed word at or before time t (index into timedIdx), or -1
  function timedAt(t) {
    let lo = 0, hi = timedT.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (timedT[mid] <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  function chapterAt(t) {
    const ch = data.chapters;
    let ans = 0;
    for (let i = 0; i < ch.length; i++) if (ch[i].start <= t + 0.001) ans = i;
    return ch[ans];
  }

  // first timed word at or after global word g
  function timeOfWordOrNext(g) {
    for (let i = g; i < wTime.length; i++) if (!Number.isNaN(wTime[i])) return wTime[i];
    for (let i = g; i >= 0; i--) if (!Number.isNaN(wTime[i])) return wTime[i];
    return 0;
  }

  // ---------------- position ----------------

  function phoneFresh() {
    return phone.ok && now() - phone.recv < 5000;
  }

  function phonePos() {
    const s = phone.s;
    return s.position + (s.playing ? ((now() - phone.recv) / 1000) * s.speed : 0);
  }

  function clockPos() {
    return clock.pos + (clock.playing ? ((now() - clock.wall) / 1000) * clock.speed : 0);
  }

  function pickSource() {
    let want = "manual";
    if (mode === "phone") want = "phone";
    else if (mode === "auto" && phoneFresh()) want = "phone";
    if (source === "phone" && want === "manual" && phone.s) {
      // phone dropped: carry on from where it was, same play state and speed
      setClock(phonePos(), phone.s.playing, phone.s.speed, false);
      toast("Phone not answering. Running on the manual clock from the last position.");
    }
    source = want;
  }

  function position() {
    if (source === "phone") return phone.s ? phonePos() : clock.pos;
    return clockPos();
  }

  function setClock(pos, playing = clock.playing, speed = clock.speed, render = true) {
    clock.pos = Math.max(0, Math.min(pos, data.book.duration || pos));
    clock.wall = now();
    clock.playing = playing;
    clock.speed = speed;
    if (render) { lastTick = 0; updateControls(); }
  }

  // ---------------- rendering ----------------

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderSection(sec) {
    const s = data.sections.find((x) => x.idx === sec);
    const text = $("#rText");
    const base = secFirst[sec];
    let wi = 0;
    const html = [];
    for (const bl of s.blocks) {
      if (bl.type === "img") {
        html.push(`<img src="/api/books/${bookId}/images/${encodeURIComponent(bl.src)}" alt="${esc(bl.alt)}">`);
        continue;
      }
      const tag = /^h[1-6]$/.test(bl.type) ? bl.type : bl.type === "li" ? "li" : "p";
      const words = bl.text.split(" ").filter(Boolean).map((w) => `<span data-w="${base + wi++}">${esc(w)}</span>`);
      html.push(`<${tag}>${words.join(" ")}</${tag}>`);
    }
    text.innerHTML = `<div class="hl" id="rHl"></div>` + html.join("");
    spans = Array.from(text.querySelectorAll("span[data-w]"));
    curSec = sec;
    curWord = curLine = -1;
    paintHighlights();
    computeLines();
    text.querySelectorAll("img").forEach((img) => img.addEventListener("load", scheduleRelayout, { once: true }));
    const sel = $("#rSection");
    sel.value = String(sec);
  }

  function computeLines() {
    lines = [];
    lineOf = new Int32Array(spans.length);
    let cur = null;
    for (let i = 0; i < spans.length; i++) {
      const sp = spans[i];
      const top = sp.offsetTop, h = sp.offsetHeight, blk = sp.parentElement;
      // words on one line share the same top; a new line starts as soon as the top changes
      if (!cur || blk !== cur.blk || Math.abs(top - cur.top) > 1) {
        cur = { top, bottom: top + h, blk, first: i, last: i };
        lines.push(cur);
      } else {
        cur.bottom = Math.max(cur.bottom, top + h);
        cur.last = i;
      }
      lineOf[i] = lines.length - 1;
    }
  }

  let relayoutTimer = null;
  function scheduleRelayout() {
    clearTimeout(relayoutTimer);
    relayoutTimer = setTimeout(() => {
      if (curSec < 0) return;
      computeLines();
      const w = curWord; curWord = curLine = -1;
      if (w >= 0) highlight(w, true);
    }, 120);
  }

  function highlight(g, force = false) {
    if (g === curWord && !force) return;
    const local = g - secFirst[curSec];
    if (local < 0 || local >= spans.length) return;
    if (prefs.wordHL) {
      if (curWord >= 0 && spans[curWord - secFirst[curSec]]) spans[curWord - secFirst[curSec]].classList.remove("cw");
      spans[local].classList.add("cw");
    }
    curWord = g;
    const li = lineOf[local];
    if (li === curLine && !force) return;
    curLine = li;
    const L = lines[li];
    const hl = $("#rHl");
    const pad = Math.round(prefs.fs * 0.18);
    hl.style.top = (L.top - pad) + "px";
    hl.style.height = (L.bottom - L.top + pad * 2) + "px";
    hl.style.left = (L.blk.offsetLeft - 14) + "px";
    hl.style.width = (L.blk.offsetWidth + 28) + "px";
    hl.style.opacity = "1";
    keepInView(L);
  }

  function keepInView(L, forceScroll = false) {
    const page = $("#rPage");
    if (!forceScroll && now() < userScrollUntil) { $("#rBack").classList.remove("hidden"); return; }
    $("#rBack").classList.add("hidden");
    const textTop = $("#rText").offsetTop;
    const y = textTop + L.top - page.scrollTop;
    const h = page.clientHeight;
    if (forceScroll || y < h * 0.18 || y > h * 0.62) {
      page.scrollTo({ top: textTop + L.top - h * 0.36, behavior: forceScroll ? "auto" : "smooth" });
    }
  }

  // ---------------- main loop ----------------

  function tick() {
    raf = requestAnimationFrame(tick);
    const t = now();
    if (t - lastTick < 90) return;
    lastTick = t;

    pickSource();
    const pos = position();
    const ch = chapterAt(pos);
    $("#rTime").textContent = `${fmt(pos - ch.start)} / ${fmt(ch.end - ch.start)}`;
    $("#rTotal").textContent = `${fmt(pos)} of ${fmt(data.book.duration)}`;
    const chSel = $("#rChapter");
    if (document.activeElement !== chSel && chSel.value !== String(ch.idx)) chSel.value = String(ch.idx);

    const k = timedAt(pos);
    let note = "";
    if (k < 0) {
      note = "Before the book text starts (intro or credits).";
      if (curSec < 0) renderSection(firstTextSection());
    } else {
      const g = timedIdx[k];
      const next = k + 1 < timedT.length ? timedT[k + 1] : Infinity;
      if (pos - timedT[k] > 8 && next - timedT[k] > 12) {
        note = ch.status === "no_text" || ch.status === "excluded"
          ? "This part of the audio has no book text (credits, intro)."
          : "Narration here is not in the book text.";
      }
      if (wSec[g] !== curSec) renderSection(wSec[g]);
      highlight(g);
    }
    const n = $("#rNote");
    n.textContent = note;
    n.classList.toggle("hidden", !note);
    root.classList.toggle("off-text", !!note);
    updateBadge();
  }

  function firstTextSection() {
    return timedIdx.length ? wSec[timedIdx[0]] : data.sections[0].idx;
  }

  // ---------------- phone polling ----------------

  async function poll() {
    if (mode === "manual") return;
    try {
      const r = await fetch("/api/sync/adb");
      const s = await r.json();
      if (s.ok && data.book.duration && s.position > data.book.duration + 30) {
        phone.ok = false;
        phone.err = "The phone is playing something longer than this book. Is the same book open in your player?";
      } else if (s.ok) {
        phone.s = s; phone.recv = now(); phone.ok = true; phone.err = ""; phone.lastOk = now();
        phone.meta = s.meta;
      } else {
        phone.ok = false; phone.err = s.error || "Phone not available";
      }
    } catch (_) {
      phone.ok = false; phone.err = "Server not reachable";
    }
  }

  // ---------------- controls ----------------

  function updateBadge() {
    const b = $("#rBadge");
    let cls = "", text = "";
    if (source === "phone" && phone.ok) {
      cls = "phone";
      text = phone.s.playing ? `Following phone · ${phone.s.speed}x` : "Phone paused";
    } else if (mode === "phone") {
      cls = "lost"; text = "Waiting for phone";
    } else if (mode === "auto" && phone.lastOk) {
      cls = "lost"; text = "Phone lost · manual clock";
    } else {
      cls = "manual"; text = clock.playing ? `Manual clock · ${clock.speed}x` : "Manual clock · paused";
    }
    if (b.dataset.cls !== cls || b.textContent !== text) {
      b.className = "rbadge " + cls; b.dataset.cls = cls; b.textContent = text;
      b.title = phone.err || (phone.meta ? `Player: ${phone.meta}` : "");
      updateControls();
    }
  }

  function updateControls() {
    if (!root) return;
    const manual = source === "manual";
    root.querySelectorAll(".manual-only").forEach((el) => { el.disabled = !manual; });
    $("#rPlay").textContent = clock.playing ? "Pause" : "Play";
    if (document.activeElement !== $("#rSpeed")) $("#rSpeed").value = clock.speed;
    $("#rCtl").classList.toggle("following", !manual);
  }

  function toast(msg) {
    const t = $("#rToast");
    if (!t) return;
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("show"), 3500);
  }

  function needManual() {
    if (source === "manual") return true;
    toast("Following the phone. Switch the mode to Manual to move the clock by hand.");
    return false;
  }

  function togglePlay() {
    if (!needManual()) return;
    setClock(clockPos(), !clock.playing);
  }

  function nudge(sec) {
    if (!needManual()) return;
    setClock(clockPos() + sec);
  }

  function stepLine(dir) {
    if (!needManual() || curLine < 0) return;
    const li = Math.max(0, Math.min(lines.length - 1, curLine + dir));
    const g = secFirst[curSec] + lines[li].first;
    setClock(timeOfWordOrNext(g) + 0.01);
  }

  function jump() {
    if (!needManual()) return;
    const t = parseTime($("#rJump").value);
    if (t == null) { toast("Type a time like 12:34 or 1:02:03"); return; }
    const scope = $("#rJumpScope").value;
    const ch = data.chapters.find((c) => String(c.idx) === $("#rChapter").value) || chapterAt(clockPos());
    setClock(scope === "chapter" ? ch.start + t : t);
    $("#rJump").blur();
  }

  function onWordClick(e) {
    const sp = e.target.closest("span[data-w]");
    if (!sp) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;   // finishing a text selection, not a sync click
    if (!needManual()) return;
    const g = Number(sp.dataset.w);
    setClock(timeOfWordOrNext(g) + 0.01);
    userScrollUntil = 0;
  }

  function fontStack(name) {
    if (!name) return "var(--serif)";
    if (name === "__serif") return "serif";
    if (name === "__sans") return "sans-serif";
    return `"${name.replace(/"/g, "")}", var(--serif)`;
  }

  function applyPrefs() {
    root.dataset.theme = prefs.theme;
    root.style.setProperty("--rfs", prefs.fs + "px");
    root.style.setProperty("--rcol", prefs.col + "ch");
    root.style.setProperty("--rlh", prefs.lh);
    root.style.setProperty("--rws", prefs.ws + "em");
    root.style.setProperty("--rps", prefs.ps + "em");
    root.style.setProperty("--rfont", fontStack(prefs.font));
    root.classList.toggle("word-hl", prefs.wordHL);
    $("#rFsVal").textContent = prefs.fs;
    $("#rWordHL").checked = prefs.wordHL;
    $("#rTheme").value = prefs.theme;
    $("#rLh").value = prefs.lh; $("#rLhVal").textContent = Number(prefs.lh).toFixed(2);
    $("#rWs").value = prefs.ws; $("#rWsVal").textContent = Number(prefs.ws).toFixed(2) + " em";
    $("#rPs").value = prefs.ps; $("#rPsVal").textContent = Number(prefs.ps).toFixed(2) + " em";
    const fsel = $("#rFont");
    if (![...fsel.options].some((o) => o.value === prefs.font)) {
      fsel.insertAdjacentHTML("beforeend", `<option value="${esc(prefs.font)}">${esc(prefs.font)}</option>`);
    }
    fsel.value = prefs.font;
    savePrefs();
    scheduleRelayout();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleRelayout);
  }

  async function loadFonts() {
    if (fontsLoaded) return;
    fontsLoaded = true;
    try {
      const r = await (await fetch("/api/fonts")).json();
      const fsel = $("#rFont");
      const have = new Set([...fsel.options].map((o) => o.value));
      fsel.insertAdjacentHTML("beforeend", `<optgroup label="Installed fonts">` +
        r.fonts.filter((f) => !have.has(f)).map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join("") +
        `</optgroup>`);
      fsel.value = prefs.font;
    } catch (_) { fontsLoaded = false; }
  }

  // ---------------- fullscreen (text only) ----------------

  function setZen(on) {
    zen = on;
    root.classList.toggle("zen", on);
    $("#rMenu").classList.add("hidden");
    $("#rHlPanel").classList.add("hidden");
    scheduleRelayout();
    setTimeout(() => { if (curLine >= 0) keepInView(lines[curLine], true); }, 200);
  }

  // ---------------- saved looks ----------------

  function renderLooks() {
    const sel = $("#rLookSel");
    if (!sel) return;
    const names = Object.keys(looks).sort((a, b) => a.localeCompare(b));
    sel.innerHTML = names.length ? names.map((n) => `<option>${esc(n)}</option>`).join("")
      : `<option value="">No saved themes yet</option>`;
    $("#rLookApply").disabled = $("#rLookDel").disabled = !names.length;
  }

  function saveLook() {
    const name = $("#rLookName").value.trim();
    if (!name) { toast("Give the theme a name first"); return; }
    looks[name] = pickLook(prefs);
    $("#rLookName").value = "";
    saveReaderConfig();
    renderLooks();
    $("#rLookSel").value = name;
    toast(`Saved "${name}"`);
  }

  function applyLook() {
    const l = looks[$("#rLookSel").value];
    if (!l) return;
    Object.assign(prefs, l);
    applyPrefs();
    toast(`Applied "${$("#rLookSel").value}"`);
  }

  function deleteLook() {
    const name = $("#rLookSel").value;
    if (!looks[name] || !confirm(`Delete the saved theme "${name}"?`)) return;
    delete looks[name];
    saveReaderConfig();
    renderLooks();
  }

  // ---------------- user highlights ----------------

  async function loadHighlights() {
    try { highlights = await (await fetch(`/api/books/${bookId}/highlights`)).json(); }
    catch (_) { highlights = []; }
    updateHlCount();
  }

  function updateHlCount() {
    const b = $("#rHlBtn");
    if (b) b.textContent = highlights.length ? `Highlights (${highlights.length})` : "Highlights";
  }

  function paintHighlights() {
    if (curSec < 0 && !spans.length) return;
    spans.forEach((sp) => {
      if (sp.dataset.hid) {
        sp.classList.remove("uh", "uh-end", ...HL_COLORS.map((c) => "uh-" + c));
        delete sp.dataset.hid;
      }
    });
    const sec = curSec;
    for (const h of highlights) {
      if (h.sec !== sec) continue;
      for (let w = h.w_start; w <= h.w_end && w < spans.length; w++) {
        const sp = spans[w];
        sp.classList.add("uh", "uh-" + h.color);
        if (w === h.w_end) sp.classList.add("uh-end");
        sp.dataset.hid = h.id;
      }
    }
  }

  function selectionWords() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const text = $("#rText");
    if (!text.contains(range.commonAncestorContainer)) return null;
    const inText = (node) => (node.nodeType === 3 ? node.parentElement : node);
    let a = inText(range.startContainer).closest("span[data-w]");
    let b = inText(range.endContainer).closest("span[data-w]");
    // selection may start/end in the gap between words or at a block edge
    const all = spans.filter((sp) => range.intersectsNode(sp));
    if (!all.length) return null;
    a = a && range.intersectsNode(a) ? a : all[0];
    b = b && range.intersectsNode(b) ? b : all[all.length - 1];
    const base = secFirst[curSec];
    const wa = Number(a.dataset.w) - base, wb = Number(b.dataset.w) - base;
    let rect;
    try { rect = range.getBoundingClientRect(); } catch (_) { rect = null; }
    if (!rect || (!rect.width && !rect.height)) rect = a.getBoundingClientRect();
    return { start: Math.min(wa, wb), end: Math.max(wa, wb), rect };
  }

  function showPopover(rect, html, wireFn) {
    const pop = $("#rPop");
    pop.innerHTML = html;
    pop.classList.remove("hidden");
    const w = pop.offsetWidth || 220;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, rect.left + rect.width / 2 - w / 2));
    const top = rect.top > 70 ? rect.top - 52 : rect.bottom + 10;
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    wireFn(pop);
  }

  function hidePopover() { const p = $("#rPop"); if (p) p.classList.add("hidden"); }

  function onSelectEnd() {
    setTimeout(() => {
      const sw = selectionWords();
      if (!sw) return;
      const dots = HL_COLORS.map((c) => `<button class="hdot hd-${c}" data-c="${c}" title="Highlight ${c}"></button>`).join("");
      showPopover(sw.rect, `${dots}<button class="mini" data-x="1">Cancel</button>`, (pop) => {
        pop.querySelectorAll("[data-c]").forEach((b) => {
          b.onclick = () => addHighlight(curSec, sw.start, sw.end, b.dataset.c);
        });
        pop.querySelector("[data-x]").onclick = () => { hidePopover(); window.getSelection().removeAllRanges(); };
      });
    }, 10);
  }

  async function addHighlight(sec, a, b, color) {
    hidePopover();
    window.getSelection().removeAllRanges();
    try {
      const r = await fetch(`/api/books/${bookId}/highlights`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sec, w_start: a, w_end: b, color }),
      });
      const h = await r.json();
      if (!r.ok) throw new Error(h.detail || "Could not save");
      highlights = highlights.filter((x) => !(x.sec === sec && x.w_start <= b && x.w_end >= a));
      highlights.push(h);
      highlights.sort((x, y) => x.sec - y.sec || x.w_start - y.w_start);
      paintHighlights(); updateHlCount(); renderHlPanel();
      toast("Highlighted. Right-click it to change or remove.");
    } catch (e) { toast(e.message); }
  }

  async function editHighlight(id, changes) {
    const r = await fetch(`/api/books/${bookId}/highlights/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
    });
    if (!r.ok) { toast("Could not save the change"); return; }
    const h = await r.json();
    highlights = highlights.map((x) => (x.id === id ? h : x));
    paintHighlights(); renderHlPanel();
  }

  async function deleteHighlight(id) {
    const r = await fetch(`/api/books/${bookId}/highlights/${id}`, { method: "DELETE" });
    if (!r.ok) { toast("Could not remove it"); return; }
    highlights = highlights.filter((x) => x.id !== id);
    paintHighlights(); updateHlCount(); renderHlPanel();
  }

  function onContextMenu(e) {
    const sp = e.target.closest("span[data-hid]");
    if (!sp) return;
    e.preventDefault();
    const id = Number(sp.dataset.hid);
    const h = highlights.find((x) => x.id === id);
    if (!h) return;
    const dots = HL_COLORS.map((c) => `<button class="hdot hd-${c}${c === h.color ? " on" : ""}" data-c="${c}"></button>`).join("");
    showPopover(sp.getBoundingClientRect(), `${dots}<button class="mini" data-n="1">Note</button><button class="mini danger" data-d="1">Remove</button>`, (pop) => {
      pop.querySelectorAll("[data-c]").forEach((b) => { b.onclick = () => { hidePopover(); editHighlight(id, { color: b.dataset.c }); }; });
      pop.querySelector("[data-n]").onclick = () => { hidePopover(); openHlPanel(id); };
      pop.querySelector("[data-d]").onclick = () => { hidePopover(); deleteHighlight(id); };
    });
  }

  function sectionTitle(sec) {
    const s = data.sections.find((x) => x.idx === sec);
    return s ? s.title : `Section ${sec + 1}`;
  }

  function renderHlPanel(focusId) {
    const list = $("#rHlList");
    if (!list) return;
    if (!highlights.length) {
      list.innerHTML = `<p class="rmuted">No highlights yet. Select text in the book and pick a colour.</p>`;
      return;
    }
    let lastSec = null;
    list.innerHTML = highlights.map((h) => {
      const head = h.sec !== lastSec ? `<div class="hsec">${esc(sectionTitle(h.sec))}</div>` : "";
      lastSec = h.sec;
      const t = wTime[secFirst[h.sec] + h.w_start];
      return `${head}<div class="hitem hi-${h.color}" data-id="${h.id}">
        <div class="htext">${esc(h.text.length > 400 ? h.text.slice(0, 400) + "..." : h.text)}</div>
        <textarea class="hnote" placeholder="Add a note">${esc(h.note)}</textarea>
        <div class="row">
          <button class="mini" data-go="${h.id}">Go to it</button>
          ${Number.isNaN(t) || t === undefined ? "" : `<span class="rmuted">${fmt(t)}</span>`}
          <span style="flex:1"></span>
          <button class="mini danger" data-del="${h.id}">Remove</button>
        </div></div>`;
    }).join("");
    list.querySelectorAll("[data-go]").forEach((b) => { b.onclick = () => goToHighlight(Number(b.dataset.go)); });
    list.querySelectorAll("[data-del]").forEach((b) => { b.onclick = () => deleteHighlight(Number(b.dataset.del)); });
    list.querySelectorAll(".hnote").forEach((ta) => {
      ta.onchange = () => editHighlight(Number(ta.closest(".hitem").dataset.id), { note: ta.value });
    });
    if (focusId) {
      const item = list.querySelector(`.hitem[data-id="${focusId}"]`);
      if (item) { item.scrollIntoView({ block: "center" }); item.querySelector("textarea").focus(); }
    }
  }

  function openHlPanel(focusId) {
    $("#rMenu").classList.add("hidden");
    $("#rHlPanel").classList.remove("hidden");
    renderHlPanel(focusId);
  }

  function goToHighlight(id) {
    const h = highlights.find((x) => x.id === id);
    if (!h) return;
    if (curSec !== h.sec) renderSection(h.sec);
    const sp = spans[h.w_start];
    if (!sp) return;
    userScrollUntil = now() + 120000;   // browsing: hold auto-scroll until "Back to current"
    $("#rBack").classList.remove("hidden");
    const page = $("#rPage");
    page.scrollTo({ top: $("#rText").offsetTop + sp.offsetTop - page.clientHeight * 0.3 });
    spans.slice(h.w_start, h.w_end + 1).forEach((x) => { x.classList.add("flash"); setTimeout(() => x.classList.remove("flash"), 1600); });
  }

  function exportHighlights() {
    const lines = [`# Highlights: ${data.book.title}`, data.book.author ? `*${data.book.author}*` : "", ""];
    let lastSec = null;
    for (const h of highlights) {
      if (h.sec !== lastSec) { lines.push(`## ${sectionTitle(h.sec)}`, ""); lastSec = h.sec; }
      lines.push(`> ${h.text}`, "");
      if (h.note) lines.push(h.note, "");
    }
    const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${data.book.title.replace(/[^\w\- ]+/g, "").trim() || "book"} - highlights.md`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  async function savePosition(force = false) {
    if (!data) return;
    const pos = position();
    if (!force && Math.abs(pos - lastSaved) < 2) return;
    lastSaved = pos;
    try {
      await fetch(`/api/books/${bookId}/position`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pos }), keepalive: true,
      });
    } catch (_) {}
  }

  function onKey(e) {
    if (!root || !document.body.contains(root)) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") {
      if (e.key === "Enter" && e.target.id === "rJump") jump();
      return;
    }
    const big = e.shiftKey ? 30 : 5;
    switch (e.key) {
      case " ": e.preventDefault(); togglePlay(); break;
      case "ArrowLeft": e.preventDefault(); nudge(-big); break;
      case "ArrowRight": e.preventDefault(); nudge(big); break;
      case "ArrowUp": e.preventDefault(); stepLine(-1); break;
      case "ArrowDown": e.preventDefault(); stepLine(1); break;
      case "j": case "g": e.preventDefault(); $("#rJump").focus(); break;
      case "c": e.preventDefault(); backToCurrent(); break;
      case "f": e.preventDefault(); setZen(!zen); break;
      case "h": e.preventDefault(); $("#rHlPanel").classList.contains("hidden") ? openHlPanel() : $("#rHlPanel").classList.add("hidden"); break;
      case "Escape":
        hidePopover(); $("#rMenu").classList.add("hidden"); $("#rHlPanel").classList.add("hidden");
        if (zen) setZen(false);
        break;
      case "+": case "=": prefs.fs = Math.min(48, prefs.fs + 2); applyPrefs(); break;
      case "-": prefs.fs = Math.max(14, prefs.fs - 2); applyPrefs(); break;
      default:
    }
  }

  function onSelectionChange() {
    if (!root) return;
    const sel = window.getSelection();
    // hold the page still while you select text
    if (sel && !sel.isCollapsed && $("#rText").contains(sel.anchorNode)) userScrollUntil = now() + 10000;
  }

  function onDocMouseDown(e) {
    if (!root) return;
    const pop = $("#rPop");
    if (pop && !pop.classList.contains("hidden") && !pop.contains(e.target)) hidePopover();
  }

  function backToCurrent() {
    userScrollUntil = 0;
    if (curLine >= 0) keepInView(lines[curLine], true);
  }

  // ---------------- open / close ----------------

  function layout() {
    const chOpts = data.chapters.map((c) =>
      `<option value="${c.idx}">${c.idx + 1}. ${esc(c.title)}${c.status === "no_text" || c.status === "excluded" ? " (no text)" : ""}</option>`).join("");
    const secOpts = data.sections.map((s) => `<option value="${s.idx}">${esc(s.title).slice(0, 60)}</option>`).join("");
    return `
      <div class="reader" id="reader">
        <header class="rbar">
          <a href="#/book/${bookId}" class="rback" title="Back to book">&larr;</a>
          <div class="rtitle"><b>${esc(data.book.title)}</b><span>${esc(data.book.author || "")}</span></div>
          <select id="rChapter" title="Audio chapter">${chOpts}</select>
          <div class="rtime"><b id="rTime">0:00</b><span id="rTotal"></span></div>
          <span class="rbadge" id="rBadge">...</span>
          <button id="rHlBtn" class="mini" title="Your highlights (H)">Highlights</button>
          <button id="rMenuBtn" class="mini" title="Settings">Aa &amp; sync</button>
          <button id="rZenBtn" class="mini" title="Full screen, text only (F)">Full screen</button>
        </header>
        <div class="rctl" id="rCtl">
          <button id="rPlay" class="manual-only primary">Play</button>
          <button class="manual-only" data-nudge="-30" title="Back 30 s (Shift+Left)">-30</button>
          <button class="manual-only" data-nudge="-5" title="Back 5 s (Left)">-5</button>
          <button class="manual-only" data-nudge="5" title="Forward 5 s (Right)">+5</button>
          <button class="manual-only" data-nudge="30" title="Forward 30 s (Shift+Right)">+30</button>
          <label class="rspeed">Speed <input id="rSpeed" class="manual-only" type="number" step="0.05" min="0.5" max="3.5" value="1"></label>
          <span class="rsep"></span>
          <input id="rJump" class="manual-only" type="text" placeholder="time your player shows, e.g. 12:34" title="Press J to jump here">
          <select id="rJumpScope" class="manual-only"><option value="chapter">in chapter</option><option value="book">whole book</option></select>
          <button id="rJumpGo" class="manual-only">Go</button>
          <span class="rhint">Space play/pause · arrows nudge · up/down line · click a line to sync</span>
          <span class="rfollow">Following the phone. Pause, seek and speed come from your player.</span>
          <span class="rnote hidden" id="rNote"></span>
        </div>
        <div class="rmenu hidden" id="rMenu">
          <div class="rgroup"><b>Text</b>
            <div class="row">Size <button class="mini" id="rFsDown">A-</button><span id="rFsVal"></span><button class="mini" id="rFsUp">A+</button></div>
            <div class="row">Width <button class="mini" id="rColDown">Narrower</button><button class="mini" id="rColUp">Wider</button></div>
            <div class="row">Theme <select id="rTheme"><option value="dark">Dark</option><option value="oldbook">Old book</option><option value="sepia">Sepia</option><option value="light">Light</option></select></div>
            <div class="row">Font <select id="rFont"><option value="">Default (book serif)</option><option value="__serif">System serif</option><option value="__sans">System sans</option></select></div>
            <label class="rslider">Line spacing <input type="range" id="rLh" min="0.5" max="2.6" step="0.05"><span id="rLhVal"></span></label>
            <label class="rslider">Word spacing <input type="range" id="rWs" min="-0.05" max="0.6" step="0.01"><span id="rWsVal"></span></label>
            <label class="rslider">Paragraph spacing <input type="range" id="rPs" min="0" max="2.5" step="0.05"><span id="rPsVal"></span></label>
            <div class="row"><button class="mini" id="rTextReset">Reset spacing and font</button></div>
            <label class="row"><input type="checkbox" id="rWordHL"> Highlight the current word too</label>
            <div class="row">Jump to section <select id="rSection">${secOpts}</select></div>
          </div>
          <div class="rgroup"><b>Saved themes</b>
            <span class="rmuted">Your current look is always remembered. Save it under a name to switch between looks.</span>
            <div class="row"><select id="rLookSel"></select><button class="mini" id="rLookApply">Apply</button><button class="mini danger" id="rLookDel">Delete</button></div>
            <div class="row"><input type="text" id="rLookName" placeholder="Name, e.g. Night reading"><button class="mini primary" id="rLookSave">Save current look</button></div>
          </div>
          <div class="rgroup"><b>Sync</b>
            <label class="row"><input type="radio" name="rmode" value="auto"> Auto: follow the phone, manual clock when it's away</label>
            <label class="row"><input type="radio" name="rmode" value="phone"> Phone only</label>
            <label class="row"><input type="radio" name="rmode" value="manual"> Manual clock only</label>
            <div class="row"><input type="text" id="rAddr" placeholder="wireless adb, e.g. 192.168.1.23:5555"><button class="mini" id="rConnect">Connect</button></div>
            <div class="row"><select id="rSerial"><option value="">Any connected phone</option></select><button class="mini" id="rRefreshDev">Refresh</button></div>
            <div class="rstatus" id="rPhoneStatus"></div>
          </div>
        </div>
        <div class="rpage" id="rPage">
          <article class="rtext" id="rText"></article>
        </div>
        <aside class="rhlpanel hidden" id="rHlPanel">
          <div class="row"><b>Highlights</b><span style="flex:1"></span>
            <button class="mini" id="rHlExport" title="Download as Markdown">Export</button>
            <button class="mini" id="rHlClose">Close</button></div>
          <div id="rHlList"></div>
        </aside>
        <div class="rpop hidden" id="rPop"></div>
        <button class="rzenexit" id="rZenExit" title="Exit full screen (F or Esc)">&#x2715;</button>
        <button class="rbackbtn hidden" id="rBack">Back to current line (C)</button>
        <div class="rtoast" id="rToast"></div>
      </div>`;
  }

  async function loadSyncSettings() {
    try {
      const r = await (await fetch("/api/settings")).json();
      const s = r.settings;
      mode = prefs.syncMode || s.sync_mode || "auto";
      root.querySelectorAll("input[name=rmode]").forEach((el) => { el.checked = el.value === mode; });
      $("#rAddr").value = s.adb_address || "";
      await refreshDevices(s.adb_serial);
    } catch (_) {}
  }

  async function refreshDevices(selected) {
    try {
      const r = await (await fetch("/api/sync/devices")).json();
      const sel = $("#rSerial");
      const cur = selected ?? r.serial ?? "";
      sel.innerHTML = `<option value="">Any connected phone</option>` +
        r.devices.map((d) => `<option value="${esc(d.serial)}">${esc(d.model || d.serial)} (${esc(d.state)})</option>`).join("");
      sel.value = cur;
    } catch (_) {}
  }

  function wire() {
    $("#rText").addEventListener("click", onWordClick);
    $("#rPlay").onclick = togglePlay;
    root.querySelectorAll("[data-nudge]").forEach((b) => { b.onclick = () => nudge(Number(b.dataset.nudge)); });
    $("#rSpeed").onchange = () => {
      const v = Number($("#rSpeed").value);
      if (v >= 0.5 && v <= 3.5 && needManual()) setClock(clockPos(), clock.playing, v);
    };
    $("#rJumpGo").onclick = jump;
    $("#rChapter").onchange = () => {
      const c = data.chapters.find((x) => String(x.idx) === $("#rChapter").value);
      if (!c) return;
      if (source === "manual") { setClock(c.start, false); userScrollUntil = 0; }
      else toast("Following the phone. Pick the chapter in your player, or switch to Manual.");
      $("#rChapter").blur();
    };
    $("#rSection").onchange = () => {
      const sec = Number($("#rSection").value);
      renderSection(sec);
      userScrollUntil = now() + 60000;  // browsing: stop auto-scroll until "back to current"
      $("#rPage").scrollTop = 0;
      $("#rBack").classList.remove("hidden");
    };
    $("#rMenuBtn").onclick = () => { $("#rHlPanel").classList.add("hidden"); $("#rMenu").classList.toggle("hidden"); loadFonts(); };
    $("#rHlBtn").onclick = () => ($("#rHlPanel").classList.contains("hidden") ? openHlPanel() : $("#rHlPanel").classList.add("hidden"));
    $("#rHlClose").onclick = () => $("#rHlPanel").classList.add("hidden");
    $("#rHlExport").onclick = exportHighlights;
    $("#rZenBtn").onclick = () => setZen(true);
    $("#rZenExit").onclick = () => setZen(false);
    $("#rFont").onchange = () => { prefs.font = $("#rFont").value; applyPrefs(); };
    $("#rLh").oninput = () => { prefs.lh = Number($("#rLh").value); applyPrefs(); };
    $("#rWs").oninput = () => { prefs.ws = Number($("#rWs").value); applyPrefs(); };
    $("#rPs").oninput = () => { prefs.ps = Number($("#rPs").value); applyPrefs(); };
    $("#rLookSave").onclick = saveLook;
    $("#rLookApply").onclick = applyLook;
    $("#rLookDel").onclick = deleteLook;
    $("#rLookName").addEventListener("keydown", (e) => { if (e.key === "Enter") saveLook(); });
    $("#rTextReset").onclick = () => { Object.assign(prefs, TEXT_DEFAULTS); applyPrefs(); };
    const text = $("#rText");
    text.addEventListener("mouseup", onSelectEnd);
    text.addEventListener("contextmenu", onContextMenu);
    document.addEventListener("selectionchange", onSelectionChange);
    document.addEventListener("mousedown", onDocMouseDown);
    $("#rFsDown").onclick = () => { prefs.fs = Math.max(14, prefs.fs - 2); applyPrefs(); };
    $("#rFsUp").onclick = () => { prefs.fs = Math.min(48, prefs.fs + 2); applyPrefs(); };
    $("#rColDown").onclick = () => { prefs.col = Math.max(40, prefs.col - 6); applyPrefs(); };
    $("#rColUp").onclick = () => { prefs.col = Math.min(120, prefs.col + 6); applyPrefs(); };
    $("#rTheme").onchange = () => { prefs.theme = $("#rTheme").value; applyPrefs(); };
    $("#rWordHL").onchange = () => {
      prefs.wordHL = $("#rWordHL").checked;
      root.querySelectorAll(".cw").forEach((x) => x.classList.remove("cw"));
      applyPrefs();
    };
    root.querySelectorAll("input[name=rmode]").forEach((el) => {
      el.onchange = () => {
        if (!el.checked) return;
        if (source === "phone" && phone.s && el.value === "manual") {
          setClock(phonePos(), phone.s.playing, phone.s.speed);
          source = "manual";
        }
        mode = el.value; prefs.syncMode = mode; savePrefs();
        fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sync_mode: mode }) }).catch(() => {});
        phone.lastOk = 0;
        if (mode !== "manual") poll();
      };
    });
    $("#rConnect").onclick = async () => {
      const st = $("#rPhoneStatus");
      st.textContent = "Connecting...";
      try {
        const r = await (await fetch("/api/sync/connect", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ address: $("#rAddr").value }) })).json();
        st.textContent = r.message || r.detail || "";
        refreshDevices();
      } catch (e) { st.textContent = String(e); }
    };
    $("#rRefreshDev").onclick = () => refreshDevices();
    $("#rSerial").onchange = () => {
      fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ adb_serial: $("#rSerial").value }) }).catch(() => {});
    };
    $("#rBack").onclick = backToCurrent;
    const page = $("#rPage");
    const userScrolled = () => { userScrollUntil = now() + 8000; };
    page.addEventListener("wheel", userScrolled, { passive: true });
    page.addEventListener("touchmove", userScrolled, { passive: true });
    page.addEventListener("mousedown", (e) => { if (e.target === page) userScrolled(); });
    window.addEventListener("resize", scheduleRelayout);
    document.addEventListener("keydown", onKey);
    statusTimer = setInterval(() => {
      if (!root || !document.body.contains(root)) return;
      $("#rPhoneStatus").textContent = phone.ok
        ? `Phone OK. ${phone.s.player === "ak.alizandro.smartaudiobookplayer" ? "Smart Audiobook Player" : (phone.s.player || "Player")}: ${phone.s.state}, ${phone.s.speed}x${phone.meta ? ", " + phone.meta : ""}`
        : (mode === "manual" ? "Phone not used in manual mode." : phone.err);
    }, 1000);
  }

  async function open(id, mount, opts) {
    close();
    bookId = id;
    mount.innerHTML = `<div class="empty">Loading book...</div>`;
    try {
      const r = await fetch(`/api/books/${id}/reader`);
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
      data = j;
    } catch (e) {
      mount.innerHTML = `<div class="empty msg-err">${esc(e.message)}</div>
        <p class="hint"><a href="#/book/${id}">Back to the book</a></p>`;
      return;
    }
    index();
    document.body.classList.add("reading");
    await loadReaderConfig();
    mount.innerHTML = layout();
    root = mount.querySelector("#reader");
    wire();
    applyPrefs();
    renderLooks();
    await loadSyncSettings();
    await loadHighlights();

    const start = data.position ? data.position.pos : (timedT.length ? timedT[0] : 0);
    setClock(start, false, 1, false);
    renderSection(wSec[timedIdx[Math.max(0, timedAt(start))]] ?? firstTextSection());
    updateControls();
    if (mode !== "manual") await poll();
    pollTimer = setInterval(poll, 1000);
    saveTimer = setInterval(() => savePosition(), 5000);
    window.addEventListener("beforeunload", onUnload);
    raf = requestAnimationFrame(tick);
    setTimeout(() => { if (curLine >= 0) keepInView(lines[curLine], true); }, 50);
    if (opts && opts.highlights) openHlPanel();
  }

  function onUnload() { savePosition(true); }

  function close() {
    if (raf) cancelAnimationFrame(raf);
    clearInterval(pollTimer); clearInterval(saveTimer); clearInterval(statusTimer);
    if (data) savePosition(true);
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("selectionchange", onSelectionChange);
    document.removeEventListener("mousedown", onDocMouseDown);
    zen = false; highlights = [];
    window.removeEventListener("resize", scheduleRelayout);
    window.removeEventListener("beforeunload", onUnload);
    document.body.classList.remove("reading");
    raf = pollTimer = saveTimer = null;
    data = null; root = null; curSec = -1; spans = []; lines = [];
    phone.ok = false; phone.lastOk = 0; phone.s = null; source = "manual";
  }

  // exposed for tests
  return { open, close, _parseTime: parseTime };
})();
