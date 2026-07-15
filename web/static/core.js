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
function confirmClick(btn, onConfirm, confirmLabel) {
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
    btn.textContent = confirmLabel || "Confirm delete?";
    timer = setTimeout(btn._disarm, 3000);
  });
}

function reloadSoon() { setTimeout(function () { window.location.reload(); }, 250); }

// ---- server-rendered partials ----------------------------------------------
// The server owns every card's markup (the _macros.html macros), so an in-place
// update asks it to re-render rather than hand-building a node here — that's what
// keeps a spliced card identical to a page-load one instead of drifting from it.
function htmlToNode(html) {
  var tmp = document.createElement("div");
  tmp.innerHTML = (html || "").trim();
  return tmp.firstElementChild;
}
// Swap every on-page node for a task id with freshly-rendered markup (a task can appear
// twice — Today's hero AND the week pool). Returns the count replaced; 0 means the page
// has no node for it, so the caller decides whether to insert or ignore.
function swapCard(id, html) {
  var node = htmlToNode(html);
  if (!node) return 0;
  var els = document.querySelectorAll('[data-task-id="' + id + '"][data-title]');
  var n = 0;
  els.forEach(function (el) {
    var fresh = n === 0 ? node : htmlToNode(html);
    el.replaceWith(fresh);
    if (window.LifeOS && window.LifeOS.wireTaskRow) window.LifeOS.wireTaskRow(fresh);
    n++;
  });
  return n;
}

// ---- fade an element out, then Undo-toast to restore it ---------------------
// Folds the identical remove-then-restore dance shared by the note / captured /
// goal / journal deletes. opts: {label → "<label> deleted", OR msg for the exact
// toast text; restore: URL (posted with restoreData if given); after: optional
// fn run once the element is gone; onRestore: optional fn(res) → truthy if it put
// the element back itself, so Undo skips the reload}. REMOVE_MS matches CSS --dur
// (the .removing transition) — the old inline 220 had drifted out of sync with it.
var REMOVE_MS = 180;
// Fade a node out, then drop it — the remove half of removeWithUndo, for callers that
// bring their own undo (or need none). `after` runs once it's gone.
function removeNode(el, after) {
  if (!el) { if (after) after(); return; }
  el.classList.add("removing");
  setTimeout(function () {
    if (el.parentNode) el.parentNode.removeChild(el);
    if (after) after();
  }, REMOVE_MS);
}
function removeWithUndo(el, opts) {
  if (el || opts.after) { removeNode(el, opts.after); }
  toast(opts.msg || (opts.label + " deleted"), function () {
    post(opts.restore, opts.restoreData).then(function (res) {
      // an Undo that can put the row back in place shouldn't cost a whole page reload
      if (opts.onRestore && opts.onRestore(res)) return;
      reloadSoon();
    });
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

// ---- phone nav drawer: hamburger toggles the slide-in sidebar ---------------
(function () {
  var toggle = document.getElementById("navtoggle");
  var side = document.getElementById("sidenav");
  var scrim = document.getElementById("navscrim");
  if (!toggle || !side) return;
  function setOpen(on) {
    side.classList.toggle("open", on);
    if (scrim) scrim.classList.toggle("open", on);
    toggle.setAttribute("aria-expanded", on ? "true" : "false");
  }
  toggle.addEventListener("click", function () { setOpen(!side.classList.contains("open")); });
  if (scrim) scrim.addEventListener("click", function () { setOpen(false); });
  // Tapping a destination closes the drawer (it's navigating away anyway).
  side.addEventListener("click", function (e) { if (e.target.closest("a")) setOpen(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && side.classList.contains("open")) setOpen(false);
  });
})();

// ---- desktop: toggle the sidebar between full and an icon-only rail (persisted) ----
(function () {
  var root = document.documentElement;
  var toggle = document.getElementById("navcollapse");
  if (!toggle) return;
  toggle.addEventListener("click", function () {
    var on = !root.classList.contains("nav-collapsed");
    root.classList.toggle("nav-collapsed", on);
    try { localStorage.setItem("lifeos_nav_collapsed", on ? "1" : "0"); } catch (e) {}
  });
})();

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

// ---- shared file-attachment widget (notes / journal / tasks / capture) ------
// Wires a `[data-attach]` block (with .athumbs, .atadd button, and a hidden file
// input) for pick + drag-drop + paste upload of ANY file to /media/upload. Stores
// the media POINTERS on root._media; the editor reads getAttach(root) on save.
// Images render as thumbnails; other files as named tiles — both open in the
// smart media modal. Idempotent per element.
var _IMG_EXTS = ["jpg", "jpeg", "png", "gif", "webp", "heic"];
function pointerUrl(p) { return "/media/" + (p || "").split("/").pop(); }
// Stored basenames embed the original filename after "__"; recover it for display.
function pointerName(p) { var b = (p || "").split("/").pop(); var i = b.indexOf("__"); return i >= 0 ? b.slice(i + 2) : b; }
function pointerExt(p) { var n = pointerName(p); var i = n.lastIndexOf("."); return i >= 0 ? n.slice(i + 1).toLowerCase() : ""; }
function pointerIsImage(p) { return _IMG_EXTS.indexOf(pointerExt(p)) >= 0; }
function getAttach(root) { return (root && root._media) ? root._media.slice() : []; }
function setAttach(root, pointers) { if (root) { root._media = (pointers || []).filter(Boolean); renderAttach(root); } }
function renderAttach(root) {
  var wrap = root.querySelector(".athumbs"); if (!wrap) return;
  wrap.innerHTML = "";
  (root._media || []).forEach(function (p, i) {
    var t = document.createElement("div");
    if (pointerIsImage(p)) {
      t.className = "athumb";
      var img = document.createElement("img"); img.src = pointerUrl(p); img.loading = "lazy"; img.alt = "";
      img.addEventListener("click", function () { openMedia(p); });
      t.appendChild(img);
    } else {
      t.className = "atfile";
      var ic = document.createElement("span"); ic.className = "atficon"; ic.textContent = fileGlyph(pointerExt(p));
      var nm = document.createElement("span"); nm.className = "atfname"; nm.textContent = pointerName(p);
      t.appendChild(ic); t.appendChild(nm);
      t.addEventListener("click", function (e) { if (!e.target.classList.contains("atx")) openMedia(p); });
    }
    var x = document.createElement("button"); x.type = "button"; x.className = "atx"; x.textContent = "✕";
    x.addEventListener("click", function (e) { e.stopPropagation(); root._media.splice(i, 1); renderAttach(root); });
    t.appendChild(x); wrap.appendChild(t);
  });
}
function fileGlyph(ext) {
  if (ext === "pdf") return "📄";
  if (["doc", "docx", "odt", "rtf", "txt", "md"].indexOf(ext) >= 0) return "📝";
  if (["xls", "xlsx", "csv", "ods"].indexOf(ext) >= 0) return "📊";
  if (["ppt", "pptx", "key", "odp"].indexOf(ext) >= 0) return "📑";
  if (["zip", "rar", "7z", "tar", "gz"].indexOf(ext) >= 0) return "🗜";
  if (["mp3", "wav", "m4a", "oga", "ogg"].indexOf(ext) >= 0) return "🎵";
  if (["mp4", "mov", "mkv", "webm"].indexOf(ext) >= 0) return "🎬";
  return "📎";
}
function initAttach(root) {
  if (!root || root._attachInit) return; root._attachInit = true;
  root._media = root._media || [];
  var input = root.querySelector('input[type="file"]'), add = root.querySelector(".atadd");
  function upload(files) {
    Array.prototype.forEach.call(files || [], function (f) {
      var fd = new FormData(); fd.append("file", f);
      post("/media/upload", fd).then(function (res) {
        if (res.ok && res.data && res.data.pointer) { root._media.push(res.data.pointer); renderAttach(root); }
        else { toast((res.data && res.data.message) || "Upload failed"); }
      });
    });
  }
  root._upload = upload;   // let a host (e.g. the capture bar's 📎) drive uploads
  if (add) add.addEventListener("click", function () { input && input.click(); });
  if (input) input.addEventListener("change", function () { upload(input.files); input.value = ""; });
  // The whole editor / capture bar is the drop + paste zone, so a drag onto the
  // widget itself isn't required (it collapses when empty).
  var scope = root.closest(".editor") || root.closest(".qcap") || root;
  scope.addEventListener("dragover", function (e) { e.preventDefault(); scope.classList.add("atdrag"); });
  scope.addEventListener("dragleave", function (e) { if (e.target === scope) scope.classList.remove("atdrag"); });
  scope.addEventListener("drop", function (e) {
    e.preventDefault(); scope.classList.remove("atdrag");
    if (e.dataTransfer && e.dataTransfer.files) upload(e.dataTransfer.files);
  });
  scope.addEventListener("paste", function (e) {
    var items = e.clipboardData && e.clipboardData.files;
    if (items && items.length) { upload(items); }
  });
  renderAttach(root);
}

// ---- smart media modal: image / PDF preview inline, other files as a card ---
// `src` may be a pointer ("vault/.media/x") or a plain URL; `name` optional label.
function openMedia(src, name) {
  var url = /^https?:|^\//.test(src) ? src : pointerUrl(src);
  var fname = name || pointerName(src);
  var ext = (fname.split(".").pop() || "").toLowerCase();
  var isImg = _IMG_EXTS.indexOf(ext) >= 0;
  var isPdf = ext === "pdf";
  var lb = document.getElementById("lightbox");
  if (!lb) {
    lb = document.createElement("div"); lb.id = "lightbox"; lb.className = "lightbox";
    lb.setAttribute("role", "dialog");
    lb.setAttribute("aria-modal", "true");
    lb.setAttribute("aria-label", "Media preview");
    lb.tabIndex = -1;
    var close = function () {
      lb.classList.remove("on");
      var prev = lb._lastFocus; lb._lastFocus = null;
      if (prev && prev.focus) { try { prev.focus(); } catch (e) { } }   // restore focus
    };
    lb._close = close;
    lb.addEventListener("click", function (e) { if (e.target === lb) close(); });
    document.addEventListener("keydown", function (e) {
      if (!lb.classList.contains("on")) return;
      if (e.key === "Escape") { close(); return; }
      if (e.key !== "Tab") return;
      // trap focus inside the dialog while it's open
      var f = lb.querySelectorAll('a[href], button, iframe, [tabindex]:not([tabindex="-1"])');
      if (!f.length) { e.preventDefault(); lb.focus(); return; }
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    document.body.appendChild(lb);
  }
  var dl = url + (url.indexOf("?") >= 0 ? "&" : "?") + "download=1";
  if (isImg) {
    lb.innerHTML = '<img alt="">';
    lb.querySelector("img").src = url;
  } else if (isPdf) {
    lb.innerHTML = '<div class="mediabox"><iframe title="preview"></iframe>' +
      '<div class="mediabar"><span class="mfname"></span>' +
      '<a class="mini" target="_blank" rel="noopener">Open</a>' +
      '<a class="mini" download>Download</a></div></div>';
    lb.querySelector("iframe").src = url;
    lb.querySelector(".mfname").textContent = fname;
    lb.querySelectorAll("a")[0].href = url;
    lb.querySelectorAll("a")[1].href = dl;
  } else {
    lb.innerHTML = '<div class="mediabox docbox"><div class="docglyph"></div>' +
      '<div class="docname"></div><div class="dochint">No inline preview for this file type.</div>' +
      '<div class="mediabar"><a class="mini" target="_blank" rel="noopener">Open</a>' +
      '<a class="mini" download>Download</a></div></div>';
    lb.querySelector(".docglyph").textContent = fileGlyph(ext);
    lb.querySelector(".docname").textContent = fname;
    lb.querySelectorAll("a")[0].href = url;
    lb.querySelectorAll("a")[1].href = dl;
  }
  lb._lastFocus = document.activeElement;   // remember opener → restore focus on close
  lb.classList.add("on");
  lb.focus();
}
// Back-compat alias — older callers passed a plain image URL.
function openLightbox(url) { openMedia(url); }
