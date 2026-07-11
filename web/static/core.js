/* Life OS — shared front-end helpers + cross-page behaviours.
   Split out of the old single-IIFE app.js. This file is NOT wrapped in an IIFE:
   its top-level `function` declarations are intentionally GLOBAL so the ordered
   page bundles that load AFTER it (board.js / notes.js / feed.js) can call them
   by bare name — exactly as they did inside the original shared closure.
   Everything is guarded on element presence so one bundle serves every page.
   Load ORDER matters: core.js FIRST, then the page files. */
"use strict";

// ---- fetch helper (CSRF + XRW headers added by base.html patch) -------------
function post(url, data) {
  var opts = { method: "POST" };
  if (data instanceof FormData) {
    opts.body = data;
  } else if (data) {
    var fd = new FormData();
    Object.keys(data).forEach(function (k) { fd.append(k, data[k]); });
    opts.body = fd;
  }
  return fetch(url, opts).then(function (r) {
    return r.json().catch(function () { return {}; }).then(function (j) {
      return { ok: r.ok, data: j };
    });
  });
}
function postJSON(url, obj) {
  return fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj)
  }).then(function (r) { return r.json().catch(function () { return {}; }); });
}

// ---- toast with optional undo ----------------------------------------------
var toastEl = null, toastTimer = null;
function toast(msg, undoCb) {
  if (!toastEl) { toastEl = document.createElement("div"); toastEl.className = "toast"; document.body.appendChild(toastEl); }
  toastEl.innerHTML = "";
  toastEl.appendChild(document.createTextNode(msg));
  if (undoCb) {
    var u = document.createElement("button");
    u.textContent = "Undo";
    u.addEventListener("click", function () { undoCb(); toastEl.classList.remove("on"); });
    toastEl.appendChild(u);
  }
  requestAnimationFrame(function () { toastEl.classList.add("on"); });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { toastEl.classList.remove("on"); }, 3800);
}
window.lifeToast = toast;
window.post = post;   // exposed so per-page inline scripts (Shuffle) reuse CSRF-aware POST

// ---- two-step confirm for destructive buttons -------------------------------
// First click ARMS the button (label → "Confirm delete?", red .arming state); a
// second click within 3s runs onConfirm. Auto-disarms after 3s. Undo toast stays
// as the recovery net. btn._disarm lets a reopened editor reset a stale armed state.
function confirmClick(btn, onConfirm) {
  if (!btn) return;
  var timer = null, label = btn.textContent;
  btn._disarm = function () {
    btn.classList.remove("arming");
    btn.textContent = label;
    if (timer) { clearTimeout(timer); timer = null; }
  };
  btn.addEventListener("click", function () {
    if (timer) { btn._disarm(); onConfirm(); return; }
    btn.classList.add("arming");
    btn.textContent = "Confirm delete?";
    timer = setTimeout(btn._disarm, 3000);
  });
}

function reloadSoon() { setTimeout(function () { window.location.reload(); }, 250); }

// ---- fade an element out, then Undo-toast to restore it ---------------------
// Folds the identical remove-then-restore dance shared by the note / captured /
// goal / journal deletes. opts: {label → "<label> deleted", OR msg for the exact
// toast text; restore: URL (posted with restoreData if given); after: optional
// fn run once the element is gone}. REMOVE_MS matches CSS --dur (the .removing
// transition) — the old inline 220 had drifted out of sync with it.
var REMOVE_MS = 180;
function removeWithUndo(el, opts) {
  if (el) {
    el.classList.add("removing");
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
      if (opts.after) opts.after();
    }, REMOVE_MS);
  } else if (opts.after) { opts.after(); }
  toast(opts.msg || (opts.label + " deleted"), function () {
    post(opts.restore, opts.restoreData).then(reloadSoon);
  });
}

// task/subtask title text wrapper — the .tt span is the strikethrough target
// (the line spans the text, not the label's full flex width)
function titleSpan(text) {
  var s = document.createElement("span");
  s.className = "tt"; s.textContent = text;
  return s;
}

// '/' focuses quick-add (desktop). On pages without the composer, navigate to
// Today and focus it on arrival (#qin). Ignored while typing in a field.
document.addEventListener("keydown", function (e) {
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    e.preventDefault();
    var qin = document.getElementById("qin");
    if (qin) { qin.focus(); }
    else { window.location.href = "/#qin"; }
  }
});
// Arriving at Today via /#qin (FAB, mobile, or the '/' hop) focuses the composer.
if (window.location.hash === "#qin") {
  var _q = document.getElementById("qin");
  if (_q) _q.focus();
}

// ---- subtask progress rings -------------------------------------------------
function updateRing(id) {
  var boxes = document.querySelectorAll('input[data-ring="' + id + '"]');
  var done = 0; boxes.forEach(function (b) { if (b.checked) done++; });
  var pct = boxes.length ? (done / boxes.length) * 100 : 0;
  var ring = document.getElementById("ring-" + id);
  var wrap = document.getElementById("rw-" + id);
  var cnt = document.getElementById("cnt-" + id);
  if (ring) { ring.querySelector(".fill").style.strokeDasharray = pct + " 100"; ring.classList.toggle("full", pct === 100); }
  if (wrap) wrap.classList.toggle("full", pct === 100);
  if (cnt) cnt.textContent = done + "/" + boxes.length;
}
function bindRingInput(c) {
  c.addEventListener("change", function () {
    var sub = c.closest(".sub"); if (sub) sub.classList.toggle("done", c.checked);
    updateRing(c.dataset.ring);
    var id = c.dataset.subId;
    if (id) {
      post("/tasks/" + id + "/complete", { done: c.checked ? "1" : "0" }).then(function (res) {
        if (res.data && res.data.parent_completed === true) toast("All subtasks done — task complete");
      });
    }
  });
}
document.querySelectorAll("input[data-ring]").forEach(bindRingInput);
document.querySelectorAll("[data-ring-init]").forEach(function (el) { updateRing(el.getAttribute("data-ring-init")); });

// ---- animate progress indicators 0 → value on load -------------------------
// The day-score ring and goal bars render at their target inline value; nudge
// them to 0 for one frame so the CSS transition sweeps up to the real figure.
// (Subtask rings already grow from 0 via updateRing above.) No-op under
// prefers-reduced-motion because the transition is disabled by the kill-switch.
(function () {
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) return;
  // day-score ring(s): fills carrying an inline stroke-dasharray target
  var rings = [].filter.call(document.querySelectorAll(".ring .fill"), function (f) {
    return f.style && f.style.strokeDasharray;
  });
  var bars = [].slice.call(document.querySelectorAll(".bar i"));
  var ringTargets = rings.map(function (f) { return f.style.strokeDasharray; });
  var barTargets = bars.map(function (i) { return i.style.width; });
  rings.forEach(function (f) { f.style.strokeDasharray = "0 100"; });
  bars.forEach(function (i) { i.style.width = "0%"; });
  requestAnimationFrame(function () {
    requestAnimationFrame(function () {
      rings.forEach(function (f, k) { f.style.strokeDasharray = ringTargets[k]; });
      bars.forEach(function (i, k) { i.style.width = barTargets[k]; });
    });
  });
})();

// ---- voice-recording player: custom dark control ----------------------------
// Replaces the native (white) <audio> pill. Each player stays HIDDEN until its
// recording's metadata actually loads, so a note/entry whose audio file is missing
// (404) shows nothing at all — never a dead "0:00 / 0:00".
function fmtDur(s) {
  if (!isFinite(s) || s < 0) s = 0;
  s = Math.floor(s);
  return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
}
var PLAY_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
var PAUSE_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>';
function enhanceAudio(audio) {
  if (audio._enh) return;
  audio._enh = true;
  var row = audio.closest(".nplay") || audio.parentNode;
  audio.removeAttribute("controls");
  audio.preload = "metadata";
  var wrap = document.createElement("div"); wrap.className = "aud";
  var btn = document.createElement("button"); btn.type = "button"; btn.className = "aud-btn";
  btn.setAttribute("aria-label", "Play"); btn.innerHTML = PLAY_SVG;
  var track = document.createElement("div"); track.className = "aud-track";
  var fill = document.createElement("div"); fill.className = "aud-fill"; track.appendChild(fill);
  var t = document.createElement("span"); t.className = "aud-t"; t.textContent = "0:00 / 0:00";
  wrap.appendChild(btn); wrap.appendChild(track); wrap.appendChild(t);
  audio.parentNode.insertBefore(wrap, audio);
  function show(v) { if (row) row.hidden = !v; }
  function setPlaying(p) {
    btn.innerHTML = p ? PAUSE_SVG : PLAY_SVG;
    btn.setAttribute("aria-label", p ? "Pause" : "Play");
  }
  function paint() {
    var d = audio.duration || 0, c = audio.currentTime || 0;
    fill.style.width = d ? (c / d * 100) + "%" : "0";
    t.textContent = fmtDur(c) + " / " + fmtDur(d);
  }
  audio.addEventListener("loadstart", function () { show(false); fill.style.width = "0"; setPlaying(false); });
  audio.addEventListener("loadedmetadata", function () {
    if (isFinite(audio.duration) && audio.duration > 0) { show(true); paint(); }
    else { show(false); }
  });
  audio.addEventListener("error", function () { show(false); });   // missing file → no player
  audio.addEventListener("timeupdate", paint);
  audio.addEventListener("play", function () { setPlaying(true); });
  audio.addEventListener("pause", function () { setPlaying(false); });
  audio.addEventListener("ended", function () { setPlaying(false); fill.style.width = "0"; audio.currentTime = 0; });
  btn.addEventListener("click", function () { if (audio.paused) audio.play(); else audio.pause(); });
  track.addEventListener("click", function (e) {
    if (!isFinite(audio.duration) || !audio.duration) return;
    var r = track.getBoundingClientRect();
    audio.currentTime = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * audio.duration;
    paint();
  });
  // Journal players carry their src at load time — start hidden and probe now.
  // (The note editor sets src later; its own loadstart/loadedmetadata drive reveal.)
  if (audio.getAttribute("src")) { show(false); audio.load(); }
}
document.querySelectorAll(".nplay audio").forEach(enhanceAudio);

// ---- cadence graph: instant custom tooltip (native `title` lags ~1s) ---------
(function () {
  var graph = document.querySelector(".ghgraph");
  if (!graph) return;
  var tip = document.createElement("div");
  tip.className = "ghtip";
  document.body.appendChild(tip);
  function show(cell) {
    var txt = cell.getAttribute("data-tip");
    if (!txt) return;
    tip.textContent = txt;
    var r = cell.getBoundingClientRect();
    tip.style.left = (r.left + r.width / 2) + "px";
    tip.style.top = r.top + "px";
    tip.classList.add("on");
  }
  graph.addEventListener("mouseover", function (e) {
    var cell = e.target.closest(".cd[data-tip]");
    if (cell) show(cell);
  });
  graph.addEventListener("mouseout", function (e) {
    if (e.target.closest(".cd[data-tip]")) tip.classList.remove("on");
  });
})();

// ---- keyboard: Enter/Space activate the click-only affordances ---------------
// (.taskedit titles, note cards, goal numbers carry tabindex="0" in markup)
document.addEventListener("keydown", function (e) {
  if (e.key !== "Enter" && e.key !== " ") return;
  var t = e.target;
  if (t && t.matches && t.matches(".taskedit, .note[data-slug], .gnum.edit")) {
    e.preventDefault(); t.click();
  }
});

// ---- textareas grow with their content (never a scrollbar inside a box) ------
function autogrow(el) {
  el.style.height = "auto";
  var min = parseInt(window.getComputedStyle(el).minHeight, 10) || 70;
  el.style.height = Math.max(el.scrollHeight + 2, min) + "px";
}
document.querySelectorAll("textarea").forEach(function (t) {
  t.addEventListener("input", function () { autogrow(t); });
  autogrow(t);   // size correctly on load (respects each textarea's min-height)
});
