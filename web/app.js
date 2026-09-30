"use strict";

const $ = (id) => document.getElementById(id);
const api = async (url, opts = {}) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
};

const CATS = [
  { key: "pause", name: "Длинные паузы", cls: "t-pause" },
  { key: "filler", name: "Мычание (э-э, мм)", cls: "t-filler" },
  { key: "parasite", name: "Слова-паразиты", cls: "t-parasite" },
  { key: "stumble", name: "Оговорки", cls: "t-stumble" },
  { key: "retake", name: "Дубли (перезаписанные фразы)", cls: "t-retake" },
  { key: "profanity", name: "Мат и ругательства", cls: "t-profanity" },
];
const TYPE_NAMES = {
  filler: "мычание", parasite: "паразит", stumble: "оговорка", retake: "дубль", profanity: "мат",
};

// ============ мат (ищется прямо в браузере, без повторного анализа) ============

const normWord = (w) => w.toLowerCase().replace(/ё/g, "е").replace(/[^\p{L}\p{N}-]/gu, "").replace(/^-+|-+$/g, "");

const PROFANITY = [
  /ху[йеяюи]/, /пизд/, /^(а|за|вы|про|у|до|на|от|по|съ|въ|разъ|раз|об|отъ|подъ|недо|пере|из|изъ)?еб(а|у|л|и|е|н|ш|т|ыв|ись|ло)/,
  /^бля/, /^сук(а|и|у|ой|е|ам|ами)?$/, /^сучк/, /^муда[кч]/, /^мудил/, /^пид[оа]р/, /^залуп/, /^г[ао]ндон/,
  /^шлюх/, /^(на|по|ни)?хер(ня|ни|ов|ом|а|у)?$/, /^говн/, /^дерьм/, /^жоп/, /^мраз/,
];

function localMarks(words) {
  const n = words.map((w) => normWord(w.w));
  const marks = [];
  n.forEach((t, i) => {
    if (t && PROFANITY.some((re) => re.test(t))) marks.push({ from: i, to: i, type: "profanity", reason: "нецензурное слово" });
  });
  return marks;
}
const GAP_SHOW = 0.3; // паузы короче не показываем в тексте

const fmt = (t) => {
  t = Math.max(0, t);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = Math.floor(t % 60);
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
};

// ============ маршрутизация ============

let pollTimer = null;
function route() {
  clearInterval(pollTimer);
  editor.teardown();
  const m = location.hash.match(/^#\/job\/([0-9a-f]+)/);
  for (const v of ["home", "progress", "editor"]) $(`view-${v}`).hidden = true;
  $("job-title").textContent = "";
  $("new-btn").hidden = !m;
  if (m) openJob(m[1]); else showHome();
}
window.addEventListener("hashchange", route);

// ============ главная ============

async function showHome() {
  $("view-home").hidden = false;
  api("/api/status").then((s) => { $("claude-note").hidden = s.claude; }).catch(() => {});
  const render = async () => {
    const jobs = await api("/api/jobs");
    const box = $("jobs");
    box.innerHTML = jobs.length ? "" : `<div class="empty">Пока пусто — загрузите первое видео.</div>`;
    const labels = { ready: "готово", error: "ошибка", processing: "обработка", queued: "в очереди" };
    for (const j of jobs) {
      const a = document.createElement("a");
      a.className = "job";
      a.href = `#/job/${j.id}`;
      const dur = j.media ? fmt(j.media.duration) : "";
      const date = new Date(j.created * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
      a.innerHTML = `<span class="job-name"></span><span class="job-meta">${dur} · ${date}</span>
        <span class="badge ${j.status}">${labels[j.status] || j.status}</span>
        <button class="btn small ghost del" title="Удалить">✕</button>`;
      a.querySelector(".job-name").textContent = j.name;
      a.querySelector(".del").onclick = async (e) => {
        e.preventDefault();
        if (!confirm(`Удалить «${j.name}» и все результаты?`)) return;
        try { await api(`/api/jobs/${j.id}`, { method: "DELETE" }); render(); } catch (err) { alert(err.message); }
      };
      box.append(a);
    }
    if (jobs.some((j) => j.status === "processing" || j.status === "queued")) {
      clearTimeout(render.t); render.t = setTimeout(() => !$("view-home").hidden && render(), 2000);
    }
  };
  render();
}

const drop = $("drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault(); drop.classList.remove("over");
  if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
});
$("file-input").addEventListener("change", (e) => e.target.files[0] && upload(e.target.files[0]));

function upload(file) {
  $("view-home").hidden = true;
  $("view-progress").hidden = false;
  $("progress-name").textContent = file.name;
  $("progress-error").hidden = true;
  drawStages("upload", 0);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/jobs?filename=${encodeURIComponent(file.name)}`);
  xhr.upload.onprogress = (e) => e.lengthComputable && drawStages("upload", e.loaded / e.total);
  xhr.onload = () => {
    if (xhr.status !== 200) return showError(`Не удалось загрузить: ${xhr.responseText}`);
    location.hash = `#/job/${JSON.parse(xhr.responseText).id}`;
  };
  xhr.onerror = () => showError("Сеть прервалась во время загрузки");
  xhr.send(file);
  $("file-input").value = "";
}

// ============ прогресс обработки ============

const STAGES = [
  { key: "upload", name: "Загрузка файла" },
  { key: "prepare", name: "Подготовка звука", sub: ["queued", "probe", "audio", "proxy"] },
  { key: "transcribe", name: "Распознавание речи" },
  { key: "analyze", name: "Поиск мычания, пауз и дублей", sub: ["acoustic"] },
];

function drawStages(current, progress) {
  const idx = STAGES.findIndex((s) => s.key === current || (s.sub || []).includes(current));
  $("stages").innerHTML = STAGES.map((s, i) => {
    const state = i < idx ? "done" : i === idx ? "active" : "";
    const pct = i === idx && progress > 0 ? `${Math.round(progress * 100)}%` : "";
    const bar = i === idx && progress > 0 ? `<div class="bar"><i style="width:${progress * 100}%"></i></div>` : "";
    return `<li class="stage ${state}"><span class="dot"></span><span>${s.name}</span><span class="pct">${pct}</span>${bar}</li>`;
  }).join("");
}

function showError(msg) {
  $("progress-error").hidden = false;
  $("progress-error").textContent = msg;
}

async function openJob(id) {
  let job;
  try { job = await api(`/api/jobs/${id}`); } catch { location.hash = "#/"; return; }
  $("job-title").textContent = job.name;
  if (job.status === "ready") return editor.open(id);

  $("view-progress").hidden = false;
  $("progress-name").textContent = job.name;
  $("progress-error").hidden = true;
  const tick = async () => {
    try { job = await api(`/api/jobs/${id}`); } catch { return; }
    if (job.status === "ready") { clearInterval(pollTimer); $("view-progress").hidden = true; editor.open(id); return; }
    const stage = job.stage === "proxy" ? "prepare" : job.stage;
    drawStages(stage, job.progress);
    if (job.status === "error") { clearInterval(pollTimer); showError(job.error); }
  };
  tick();
  pollTimer = setInterval(tick, 1000);
}

// ============ редактор ============

const editor = (() => {
  const video = $("video");
  const tr = $("transcript");
  let jobId, job, words, dur, marks, wordMark, baseMarks, baseWordMark, edits, cuts = [], segments = [], spans = [], gapEls = new Map();
  let raf = 0, saveTimer = 0, renderPoll = 0, nowIdx = -1;

  const defaults = () => ({
    settings: { maxPause: 0.6, pad: 0.12 },
    cats: Object.fromEntries(CATS.map((c) => [c.key, true])),
    word: {}, // индекс слова → "cut" | "keep" (ручная правка поверх автоматики)
    gap: {},  // индекс паузы (перед словом i) → "cut" | "keep"
  });

  // ----- логика монтажа -----
  const markOf = (i) => (wordMark[i] != null ? marks[wordMark[i]] : null);

  // Разметка сервера + мат поверх неё (у него наивысший приоритет)
  function rebuildMarks() {
    marks = baseMarks.slice();
    wordMark = { ...baseWordMark };
    for (const m of localMarks(words)) {
      m.id = marks.length;
      marks.push(m);
      for (let i = m.from; i <= m.to; i++) wordMark[i] = m.id;
    }
  }
  function isCut(i) {
    const st = edits.word[i];
    if (st) return st === "cut";
    const m = markOf(i);
    return !!m && edits.cats[m.type];
  }
  const gapLen = (i) => (i < words.length ? words[i].s : dur) - (i > 0 ? words[i - 1].e : 0);
  function gapCut(i) {
    const st = edits.gap[i];
    if (st) return st === "cut";
    return edits.cats.pause && gapLen(i) > edits.settings.maxPause;
  }

  // Вычисляет вырезаемые интервалы: всё между оставленными словами, кроме «воздуха» pad
  function computeCuts() {
    const { pad } = edits.settings;
    const out = [];
    const kept = [];
    for (let i = 0; i < words.length; i++) if (!isCut(i)) kept.push(i);
    if (!kept.length) return words.length ? [[0, dur]] : [];

    const f = kept[0];
    if (f > 0) out.push([0, words[f].s - Math.min(pad, Math.max(0, words[f].s - words[f - 1].e))]);
    else if (gapCut(0) && words[0].s > 2 * pad) out.push([0, words[0].s - pad]);

    for (let k = 0; k + 1 < kept.length; k++) {
      const a = kept[k], b = kept[k + 1], A = words[a], B = words[b];
      if (b === a + 1) {
        if (gapCut(b) && B.s - A.e > 2 * pad) out.push([A.e + pad, B.s - pad]);
      } else {
        const s0 = A.e + Math.min(pad, Math.max(0, words[a + 1].s - A.e));
        const e0 = B.s - Math.min(pad, Math.max(0, B.s - words[b - 1].e));
        if (e0 > s0) out.push([s0, e0]);
      }
    }

    const l = kept[kept.length - 1], L = words[l];
    if (l < words.length - 1) out.push([L.e + Math.min(pad, Math.max(0, words[l + 1].s - L.e)), dur]);
    else if (gapCut(words.length) && dur - L.e > 2 * pad) out.push([L.e + pad, dur]);

    out.sort((x, y) => x[0] - y[0]);
    const merged = [];
    for (const c of out) {
      const last = merged[merged.length - 1];
      if (last && c[0] <= last[1] + 0.02) last[1] = Math.max(last[1], c[1]);
      else merged.push([...c]);
    }
    return merged;
  }

  function computeSegments(cs) {
    const segs = [];
    let t = 0;
    for (const [s, e] of cs) {
      if (s - t > 0.04) segs.push([t, s]);
      t = Math.max(t, e);
    }
    if (dur - t > 0.04) segs.push([t, dur]);
    return segs;
  }

  // ----- отрисовка -----
  function buildTranscript() {
    tr.innerHTML = "";
    spans = []; gapEls = new Map();
    const frag = document.createDocumentFragment();
    const addGap = (i) => {
      const len = gapLen(i);
      if (len < GAP_SHOW) return;
      const g = document.createElement("span");
      g.className = "gap";
      g.dataset.g = i;
      g.textContent = `${len.toFixed(1)}с`;
      frag.append(g, " ");
      gapEls.set(i, g);
    };
    for (let i = 0; i < words.length; i++) {
      addGap(i);
      const sp = document.createElement("span");
      sp.dataset.i = i;
      sp.textContent = words[i].w;
      frag.append(sp, " ");
      spans.push(sp);
    }
    addGap(words.length);
    tr.append(frag);
  }

  function paint() {
    for (let i = 0; i < words.length; i++) {
      const m = markOf(i), st = edits.word[i], cut = isCut(i);
      let cls = "w";
      if (cut) cls += ` cut ${st === "cut" && !m ? "t-manual" : m ? "t-" + m.type : "t-manual"}`;
      else if (m) cls += ` off t-${m.type}`;
      if (words[i].p < 0.4) cls += " low";
      if (words[i].synthetic) cls += " syn";
      if (i === nowIdx) cls += " now";
      spans[i].className = cls;
      spans[i].title = m ? `${TYPE_NAMES[m.type]}: ${m.reason}` : "";
    }
    const inside = (i) => i > 0 && i < words.length && isCut(i - 1) && isCut(i);
    for (const [i, g] of gapEls) g.classList.toggle("cut", gapCut(i) || inside(i));

    cuts = computeCuts();
    segments = computeSegments(cuts);
    const after = segments.reduce((a, [s, e]) => a + e - s, 0);
    $("st-before").textContent = fmt(dur);
    $("st-after").textContent = fmt(after);
    $("st-saved").textContent = `−${fmt(dur - after)}`;
    $("v-maxpause").textContent = `${edits.settings.maxPause.toFixed(2)} с`;
    $("v-pad").textContent = `${edits.settings.pad.toFixed(2)} с`;
    paintCats();
    paintIssues();
  }

  function paintCats() {
    const counts = Object.fromEntries(CATS.map((c) => [c.key, 0]));
    for (let i = 0; i <= words.length; i++) if (gapLen(i) > edits.settings.maxPause) counts.pause++;
    for (const m of marks) counts[m.type]++;
    $("cats").innerHTML = CATS.map((c) => `
      <label class="cat ${c.cls}">
        <span class="switch"><input type="checkbox" data-cat="${c.key}" ${edits.cats[c.key] ? "checked" : ""}><span></span></span>
        <span class="sw" style="background:var(--c)"></span>
        <span class="name">${c.name}</span><span class="cnt">${counts[c.key]}</span>
      </label>`).join("");
  }

  function paintIssues() {
    const list = marks.filter((m) => m.type === "retake" || m.type === "stumble");
    const box = $("issues");
    if (!list.length) { box.innerHTML = ""; return; }
    const open = box.querySelector("details")?.open ?? true;
    const scroll = box.querySelector(".issue-list")?.scrollTop ?? 0;
    box.innerHTML = `<details ${open ? "open" : ""}><summary>Найденные дубли и оговорки: ${list.length} — проверьте их в первую очередь</summary>
      <div class="issue-list">${list.map((m) => {
        const on = range(m.from, m.to).every(isCut);
        const snippet = words.slice(m.from, m.to + 1).map((w) => w.w).join(" ");
        return `<div class="issue t-${m.type}" data-m="${m.id}">
          <input type="checkbox" ${on ? "checked" : ""} title="Вырезать">
          <span class="why"><span class="tag">${TYPE_NAMES[m.type]}</span>${fmt(words[m.from].s)} · ${esc(m.reason)}</span>
          <span class="snippet">${esc(snippet)}</span></div>`;
      }).join("")}</div></details>`;
    box.querySelector(".issue-list").scrollTop = scroll;
  }

  const range = (a, b) => Array.from({ length: b - a + 1 }, (_, k) => a + k);
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  function changed() {
    paint();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => api(`/api/jobs/${jobId}/edits`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(edits),
    }).catch(() => {}), 600);
  }

  // ----- воспроизведение -----
  function wordAt(t) {
    let lo = 0, hi = words.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (words[mid].s <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans >= 0 && t <= words[ans].e + 0.15 ? ans : -1;
  }
  function cutAt(t) {
    let lo = 0, hi = cuts.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cuts[mid][1] <= t) lo = mid + 1;
      else if (cuts[mid][0] > t) hi = mid - 1;
      else return cuts[mid];
    }
    return null;
  }
  function loop() {
    const t = video.currentTime;
    if (!video.paused && $("preview").checked) {
      const c = cutAt(t);
      if (c) video.currentTime = Math.min(c[1] + 0.001, dur);
    }
    const w = wordAt(video.currentTime);
    if (w !== nowIdx) {
      if (nowIdx >= 0 && spans[nowIdx]) spans[nowIdx].classList.remove("now");
      nowIdx = w;
      if (w >= 0) {
        spans[w].classList.add("now");
        if (!video.paused) {
          const r = spans[w].getBoundingClientRect(), b = tr.getBoundingClientRect();
          if (r.top < b.top + 40 || r.bottom > b.bottom - 80) spans[w].scrollIntoView({ block: "center", behavior: "smooth" });
        }
      }
    }
    $("time").textContent = `${fmt(video.currentTime)} / ${fmt(dur)}`;
    raf = requestAnimationFrame(loop);
  }
  const seekTo = (t) => { video.currentTime = Math.max(0, t); };

  // ----- выделение текста -----
  const selBar = $("sel-bar");
  let selected = [];
  function onSelection() {
    const sel = getSelection();
    if (!sel.rangeCount || sel.isCollapsed || !tr.contains(sel.anchorNode)) { selBar.hidden = true; selected = []; return; }
    const r = sel.getRangeAt(0);
    const idx = [];
    for (const sp of spans) if (r.intersectsNode(sp)) idx.push(+sp.dataset.i);
    selected = idx;
    if (!idx.length) { selBar.hidden = true; return; }
    const rect = r.getBoundingClientRect();
    selBar.hidden = false;
    selBar.style.left = `${Math.max(8, Math.min(innerWidth - selBar.offsetWidth - 8, rect.left + rect.width / 2 - selBar.offsetWidth / 2))}px`;
    selBar.style.top = `${rect.top > 60 ? rect.top - selBar.offsetHeight - 8 : rect.bottom + 8}px`;
  }
  function applySelection(act) {
    if (!selected.length) return;
    for (const i of selected) {
      if (act === "reset") delete edits.word[i];
      else edits.word[i] = act;
    }
    // паузы внутри выделения следуют за словами
    for (let k = 1; k < selected.length; k++) {
      const g = selected[k];
      if (act === "reset") delete edits.gap[g]; else if (act === "keep") edits.gap[g] = "keep";
    }
    getSelection().removeAllRanges();
    selBar.hidden = true;
    selected = [];
    changed();
  }

  // ----- рендер -----
  function paintRender() {
    const r = job.render || { status: "idle" };
    const st = $("render-status");
    st.className = "render-status";
    $("render").disabled = r.status === "rendering";
    if (r.status === "rendering") {
      st.innerHTML = `Монтирую видео… ${Math.round((r.progress || 0) * 100)}%<div class="bar"><i style="width:${(r.progress || 0) * 100}%"></i></div>`;
    } else if (r.status === "error") {
      st.classList.add("error"); st.textContent = r.error;
    } else if (r.status === "done") {
      st.textContent = `Готово: ${fmt(r.duration || 0)}. Если поменяете разметку — нажмите «Смонтировать» ещё раз.`;
    } else st.textContent = "";
    const labels = {
      mp4: ["⬇ Видео MP4", "готовый ролик"],
      xml: ["⬇ Проект XML", "Premiere / DaVinci"],
      edl: ["⬇ EDL", "запасной формат"],
    };
    $("downloads").innerHTML = (r.files || []).map((k) =>
      `<a class="btn" href="/api/jobs/${jobId}/download/${k}?v=${job.render.progress}" download>${labels[k][0]}<small>${labels[k][1]}</small></a>`).join("");
  }
  async function pollRender() {
    clearTimeout(renderPoll);
    job = await api(`/api/jobs/${jobId}`);
    paintRender();
    if (job.render.status === "rendering") renderPoll = setTimeout(pollRender, 1000);
  }

  // ----- события -----
  tr.addEventListener("click", (e) => {
    const g = e.target.closest(".gap");
    if (g) {
      const i = +g.dataset.g;
      edits.gap[i] = gapCut(i) ? "keep" : "cut";
      return changed();
    }
    const w = e.target.closest(".w");
    if (!w || !getSelection().isCollapsed) return;
    const i = +w.dataset.i;
    if (e.ctrlKey || e.metaKey || e.altKey) return seekTo(words[i].s - 0.05);
    edits.word[i] = isCut(i) ? "keep" : "cut";
    changed();
  });
  document.addEventListener("selectionchange", () => { if (jobId) requestAnimationFrame(onSelection); });
  selBar.addEventListener("mousedown", (e) => e.preventDefault());
  selBar.addEventListener("click", (e) => { const b = e.target.closest("[data-act]"); if (b) applySelection(b.dataset.act); });
  document.addEventListener("keydown", (e) => {
    if (!jobId || $("view-editor").hidden || e.target.matches("input, textarea")) return;
    if ((e.key === "Delete" || e.key === "Backspace") && selected.length) { e.preventDefault(); applySelection("cut"); }
    else if (e.code === "Space") { e.preventDefault(); video.paused ? video.play() : video.pause(); }
  });
  $("play").onclick = () => (video.paused ? video.play() : video.pause());
  video.addEventListener("play", () => { $("play").textContent = "❚❚"; });
  video.addEventListener("pause", () => { $("play").textContent = "▶"; });
  $("cats").addEventListener("change", (e) => {
    const k = e.target.dataset.cat;
    if (k) { edits.cats[k] = e.target.checked; changed(); }
  });
  $("maxpause").addEventListener("input", (e) => { edits.settings.maxPause = +e.target.value; changed(); });
  $("pad").addEventListener("input", (e) => { edits.settings.pad = +e.target.value; changed(); });
  $("issues").addEventListener("click", (e) => {
    const row = e.target.closest(".issue");
    if (!row) return;
    const m = marks[+row.dataset.m];
    if (e.target.matches("input")) {
      for (const i of range(m.from, m.to)) edits.word[i] = e.target.checked ? "cut" : "keep";
      return changed();
    }
    seekTo(words[Math.max(0, m.from - 3)].s);
    spans[m.from].scrollIntoView({ block: "center", behavior: "smooth" });
  });
  $("render").onclick = async () => {
    try {
      await api(`/api/jobs/${jobId}/render`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ segments }),
      });
      pollRender();
    } catch (err) { alert(err.message); }
  };
  $("capcut").onclick = async () => {
    const st = $("capcut-status");
    st.className = "capcut-status"; st.textContent = "Создаю проект…";
    try {
      const r = await api(`/api/jobs/${jobId}/capcut`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ segments }),
      });
      st.className = "capcut-status ok";
      st.textContent = `Готово: проект «${r.name}», ${r.clips} кусков. Откройте CapCut — он первый в списке проектов` +
        ` (если CapCut был открыт, перезапустите его).`;
    } catch (err) { st.className = "capcut-status error"; st.textContent = err.message; }
  };
  $("reanalyze").onclick = async () => {
    if (!confirm("Заново проанализировать расшифровку? Ручные правки будут сброшены.")) return;
    await api(`/api/jobs/${jobId}/analyze`, { method: "POST" });
    location.reload();
  };

  async function open(id) {
    const data = await api(`/api/jobs/${id}/data`);
    jobId = id; job = data.job;
    words = data.transcript.words; dur = data.transcript.duration || job.media.duration;
    baseMarks = data.analysis.marks; baseWordMark = data.analysis.word_marks;
    rebuildMarks();
    const d = defaults();
    edits = data.edits ? { ...d, ...data.edits, settings: { ...d.settings, ...data.edits.settings }, cats: { ...d.cats, ...data.edits.cats } } : d;

    $("view-editor").hidden = false;
    $("maxpause").value = edits.settings.maxPause;
    $("pad").value = edits.settings.pad;
    const warn = data.analysis.claude_error;
    $("claude-warning").hidden = !warn;
    $("claude-warning-text").textContent = warn || "";
    video.src = `/api/jobs/${id}/media`;
    $("capcut-status").textContent = "";
    api("/api/capcut").then((c) => { $("capcut").hidden = !c.available; }).catch(() => {});
    buildTranscript();
    if (!words.length) tr.innerHTML = `<div class="empty">Речь не распознана.</div>`;
    paint();
    paintRender();
    if (job.render.status === "rendering") pollRender();
    raf = requestAnimationFrame(loop);
  }

  function teardown() {
    cancelAnimationFrame(raf);
    clearTimeout(renderPoll);
    if (jobId) { video.pause(); video.removeAttribute("src"); video.load(); }
    jobId = null; selBar.hidden = true;
  }

  return { open, teardown };
})();

route();
