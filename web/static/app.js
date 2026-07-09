/* Life OS — front-end behaviours, replicating the approved mockup's interactions
   against the real endpoints. Everything is guarded on element presence so one
   bundle serves every page. */
(function () {
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

  // ---- quick-add composer -----------------------------------------------------
  (function () {
    var qin = document.getElementById("qin");
    if (!qin) return;
    var qgo = document.getElementById("qgo");
    var chips = document.querySelectorAll("#qtypes .qt");
    var manual = null;
    function setActive(t) { chips.forEach(function (c) { c.classList.toggle("active", c.dataset.t === t); }); }
    chips.forEach(function (c) {
      c.addEventListener("click", function () { manual = c.dataset.t; setActive(manual); qin.focus(); });
    });
    qin.addEventListener("input", function () {
      if (manual) return;
      var v = qin.value.trim().toLowerCase(), t = "auto";
      if (v.startsWith("t:")) t = "task";
      else if (v.startsWith("n:") || v.startsWith("i:")) t = "note";
      else if (v.startsWith("j:")) t = "journal";
      else if (/^https?:\/\//.test(v) || v.includes("instagram.com") || v.includes("youtube.com") || v.includes("youtu.be")) t = "note";
      setActive(t);
    });
    function add() {
      var text = qin.value.trim();
      if (!text) { qin.focus(); return; }
      var active = document.querySelector("#qtypes .qt.active");
      var type = active ? active.dataset.t : "auto";
      post("/capture", { text: text, type: type }).then(function (res) {
        if (!res.ok) { toast("Could not add"); return; }
        qgo.textContent = "✓ Added"; qgo.classList.add("did");
        qin.value = ""; manual = null; setActive("auto");
        toast("Added → " + (res.data.label || "filed"));
        setTimeout(function () { qgo.textContent = "Add"; qgo.classList.remove("did"); reloadSoon(); }, 900);
      });
    }
    if (qgo) qgo.addEventListener("click", add);
    qin.addEventListener("keydown", function (e) { if (e.key === "Enter") add(); });
  })();

  function reloadSoon() { setTimeout(function () { window.location.reload(); }, 250); }

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
  document.querySelectorAll("input[data-ring]").forEach(function (c) {
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
  });
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

  // ---- simple task rows: complete with undo -----------------------------------
  document.querySelectorAll(".task input.tcheck").forEach(function (c) {
    c.addEventListener("change", function () {
      var row = c.closest(".task"); var id = c.dataset.taskId;
      row.classList.toggle("done", c.checked);
      var wasChecked = c.checked;
      post("/tasks/" + id + "/complete", { done: wasChecked ? "1" : "0" });
      if (wasChecked) {
        toast("Task completed", function () {
          c.checked = false; row.classList.remove("done");
          post("/tasks/" + id + "/complete", { done: "0" });
        });
      }
    });
  });

  // ---- "Do today" plan pill ---------------------------------------------------
  document.querySelectorAll(".planbtn").forEach(function (b) {
    b.addEventListener("click", function (e) {
      e.stopPropagation();
      var id = b.dataset.taskId;
      post("/tasks/" + id + "/plan").then(function (res) {
        var on = res.data && res.data.planned;
        b.classList.toggle("on", !!on);
        b.textContent = on ? "☀ On today ✓" : "☀ Do today";
        toast(on ? "Planned for today ☀" : "Removed from today");
      });
    });
  });

  // ---- kanban drag (SortableJS) -----------------------------------------------
  function initKanban() {
    if (typeof Sortable === "undefined") { setTimeout(initKanban, 100); return; }
    document.querySelectorAll(".col[data-col] .kstack").forEach(function (stack) {
      var col = stack.closest(".col").dataset.col;
      Sortable.create(stack, {
        group: "kanban", animation: 140, draggable: ".kcard", ghostClass: "sortable-ghost",
        // columns now scroll internally (viewport-height board) — keep drag usable by
        // auto-scrolling the stack under the cursor while dragging near its edges.
        scroll: true, scrollSensitivity: 90, scrollSpeed: 12, bubbleScroll: true,
        onEnd: function () {
          var targetStack = stack;
          var targetCol = targetStack.closest(".col").dataset.col;
          var ids = [].map.call(targetStack.querySelectorAll(".kcard"), function (k) { return k.dataset.taskId; });
          postJSON("/tasks/reorder", { col: targetCol, ids: ids });
        }
      });
    });
  }
  if (document.querySelector(".board")) initKanban();

  // ---- notes: tag filter + live search ---------------------------------------
  (function () {
    var search = document.getElementById("nsearch");
    if (!search && !document.querySelector(".tagbtn[data-tag]")) return;
    var activeTag = "all";
    function filterNotes() {
      var q = (search ? search.value : "").trim().toLowerCase();
      document.querySelectorAll(".note").forEach(function (n) {
        var tags = (n.dataset.tags || "").split(" ");
        var tagOk = activeTag === "all" || tags.indexOf(activeTag) !== -1;
        var qOk = !q || n.textContent.toLowerCase().indexOf(q) !== -1;
        n.classList.toggle("hide", !(tagOk && qOk));
      });
    }
    document.querySelectorAll(".tagbtn[data-tag]").forEach(function (b) {
      b.addEventListener("click", function () {
        activeTag = b.dataset.tag;
        document.querySelectorAll(".tagbtn[data-tag]").forEach(function (x) { x.classList.toggle("active", x === b); });
        filterNotes();
      });
    });
    if (search) search.addEventListener("input", filterNotes);
  })();

  // ---- task category filter (Tasks page) --------------------------------------
  document.querySelectorAll(".tagbtn[data-cat]").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".tagbtn[data-cat]").forEach(function (x) { x.classList.toggle("active", x === b); });
      var want = b.dataset.cat;
      document.querySelectorAll(".kcard").forEach(function (k) {
        k.style.display = (want === "all" || k.dataset.cat === want) ? "" : "none";
      });
    });
  });

  // ---- note editor modal ------------------------------------------------------
  (function () {
    var ov = document.getElementById("noteoverlay");
    if (!ov) return;
    var elTitle = document.getElementById("ed-title"), elTags = document.getElementById("ed-tags"),
        elBody = document.getElementById("ed-body"), elSaved = document.getElementById("ed-saved"),
        elPin = document.getElementById("ed-pin");
    var current = null, saveTimer = null, pinned = false, creating = false, savedTimer = null;
    function flashSaved() {
      elSaved.textContent = "saved ✓";
      clearTimeout(savedTimer);
      savedTimer = setTimeout(function () { elSaved.textContent = ""; }, 1800);
    }
    function open(slug) {
      fetch("/notes/" + slug).then(function (r) { return r.json(); }).then(function (j) {
        if (j.status !== "ok") return;
        current = slug; var n = j.note;
        elTitle.value = n.title; elTags.value = (n.tags || []).join(", ");
        elBody.value = n.body; pinned = n.pinned;
        elPin.classList.toggle("active", pinned);
        elPin.textContent = pinned ? "★ pinned" : "★ pin";
        elSaved.textContent = "";
        ov.classList.add("on"); elBody.focus();
      });
    }
    // Open a blank editor without persisting anything — the note file is only
    // created on the first save that carries a title or body (lazy creation, so
    // opening then closing an empty editor leaves no orphan "Untitled" note).
    function openBlank() {
      clearTimeout(saveTimer); current = null; pinned = false; creating = false;
      elTitle.value = ""; elTags.value = ""; elBody.value = "";
      elPin.classList.remove("active"); elPin.textContent = "★ pin";
      elSaved.textContent = "";
      ov.classList.add("on"); elTitle.focus();
    }
    window.openNote = open;
    function save() {
      if (current) {
        elSaved.textContent = "saving…";
        post("/notes/" + current + "/save", {
          title: elTitle.value, tags: elTags.value, body: elBody.value,
          pinned: pinned ? "1" : "0"
        }).then(flashSaved);
        return;
      }
      // no note yet: only create once there is real content (guard against a
      // debounce + blur double-fire creating two notes before the POST resolves)
      if (creating) return;
      if (!elTitle.value.trim() && !elBody.value.trim()) return;
      creating = true; elSaved.textContent = "saving…";
      post("/notes/new", {
        title: elTitle.value, tags: elTags.value, body: elBody.value
      }).then(function (res) {
        creating = false;
        if (res.data && res.data.slug) {
          current = res.data.slug; flashSaved();
        } else { elSaved.textContent = ""; }
      });
    }
    function debounced() { clearTimeout(saveTimer); saveTimer = setTimeout(save, 700); }
    [elTitle, elTags, elBody].forEach(function (el) {
      el.addEventListener("input", debounced);
      el.addEventListener("blur", save);
    });
    elPin.addEventListener("click", function () {
      pinned = !pinned; elPin.classList.toggle("active", pinned);
      elPin.textContent = pinned ? "★ pinned" : "★ pin"; save();
    });
    document.getElementById("ed-delete").addEventListener("click", function () {
      var slug = current;
      ov.classList.remove("on");
      if (!slug) { current = null; return; }   // blank unsaved note — nothing to delete
      post("/notes/" + slug + "/delete").then(function () {
        // Remove the card in place (quick fade via the motion system) instead of
        // reloading the whole page; only undo does a minimal reload to restore it.
        var card = document.querySelector('.note[data-slug="' + slug + '"]');
        if (card) {
          card.classList.add("removing");
          setTimeout(function () { if (card.parentNode) card.parentNode.removeChild(card); }, 220);
        }
        toast("Note deleted", function () { post("/notes/" + slug + "/restore").then(reloadSoon); });
      });
      current = null;
    });
    function close() { save(); ov.classList.remove("on"); current = null; }
    document.getElementById("ed-close").addEventListener("click", close);
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && ov.classList.contains("on")) close(); });

    document.querySelectorAll(".note[data-slug]").forEach(function (n) {
      n.addEventListener("click", function (e) {
        // a real link on the card (domain chip, linkified snippet URL) navigates on
        // its own — don't hijack that click to open the editor.
        if (e.target.closest("a")) return;
        open(n.dataset.slug);
      });
    });
    var newBtn = document.getElementById("note-new");
    if (newBtn) newBtn.addEventListener("click", openBlank);
  })();

  // ---- task editor modal ------------------------------------------------------
  (function () {
    var ov = document.getElementById("taskoverlay");
    if (!ov) return;
    var f = {
      title: document.getElementById("te-title"), due: document.getElementById("te-due"),
      priority: document.getElementById("te-priority"), category: document.getElementById("te-category"),
      col: document.getElementById("te-col"), recur: document.getElementById("te-recur"),
      goal: document.getElementById("te-goal"), plan: document.getElementById("te-plan"),
      saved: document.getElementById("te-saved"), subs: document.getElementById("te-subs")
    };
    var current = null, planned = false;
    // populate goal dropdown
    (window.LIFEOS_GOALS || []).forEach(function (g) {
      var o = document.createElement("option"); o.value = g.id; o.textContent = g.title; f.goal.appendChild(o);
    });
    function open(el) {
      current = el.dataset.taskId;
      f.title.value = el.dataset.title || "";
      f.due.value = el.dataset.due || "";
      f.priority.value = el.dataset.priority || "";
      f.category.value = el.dataset.category || "";
      f.col.value = el.dataset.col || "backlog";
      f.recur.value = el.dataset.recur || "";
      f.goal.value = el.dataset.goalId || "";
      planned = el.dataset.planned === "1";
      f.plan.classList.toggle("on", planned);
      f.plan.textContent = planned ? "☀ On today ✓" : "☀ Do today";
      f.saved.textContent = "";
      renderSubs(el.dataset.subs);
      ov.classList.add("on"); f.title.focus();
    }
    window.openTask = open;
    function renderSubs(json) {
      f.subs.innerHTML = "";
      var subs = [];
      try { subs = JSON.parse(json || "[]"); } catch (e) {}
      subs.forEach(function (s) {
        var row = document.createElement("div"); row.className = "sub" + (s.done ? " done" : "");
        var cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!s.done;
        cb.addEventListener("change", function () {
          post("/tasks/" + s.id + "/complete", { done: cb.checked ? "1" : "0" });
          row.classList.toggle("done", cb.checked);
        });
        var lab = document.createElement("label"); lab.textContent = s.title;
        row.appendChild(cb); row.appendChild(lab); f.subs.appendChild(row);
      });
    }
    document.getElementById("te-subadd").addEventListener("click", function () {
      var inp = document.getElementById("te-subnew"); var t = inp.value.trim();
      if (!t || !current) return;
      post("/tasks/new", { title: t, parent_id: current }).then(function () { inp.value = ""; reloadSoon(); });
    });
    f.plan.addEventListener("click", function () {
      post("/tasks/" + current + "/plan").then(function (res) {
        planned = res.data && res.data.planned;
        f.plan.classList.toggle("on", !!planned);
        f.plan.textContent = planned ? "☀ On today ✓" : "☀ Do today";
      });
    });
    document.getElementById("te-save").addEventListener("click", function () {
      post("/tasks/" + current + "/edit", {
        title: f.title.value, due_date: f.due.value, priority: f.priority.value,
        category: f.category.value, col: f.col.value, recur_rule: f.recur.value,
        goal_id: f.goal.value
      }).then(function () { f.saved.textContent = "saved ✓"; reloadSoon(); });
    });
    document.getElementById("te-delete").addEventListener("click", function () {
      var id = current;
      post("/tasks/" + id + "/delete").then(function () {
        ov.classList.remove("on");
        toast("Task deleted", function () { post("/tasks/" + id + "/restore").then(reloadSoon); });
        reloadSoon();
      });
    });
    function close() { ov.classList.remove("on"); current = null; }
    document.getElementById("te-close").addEventListener("click", close);
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && ov.classList.contains("on")) close(); });

    document.querySelectorAll(".taskedit").forEach(function (el) {
      el.addEventListener("click", function (e) { e.stopPropagation(); open(el.closest("[data-task-id]")); });
    });

    // + New task / + add task (per column) → create then open editor
    document.querySelectorAll("[data-newtask]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var col = btn.dataset.newtask || "backlog";
        post("/tasks/new", { title: "New task", col: col }).then(function (res) {
          reloadSoon();
        });
      });
    });
  })();

  // ---- captured-today feed: Change / refile (task ↔ note ↔ journal) -----------
  document.querySelectorAll(".cap .chg-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var cap = btn.closest(".cap");
      var box = cap.querySelector(".refile");
      if (box) box.style.display = box.style.display === "none" ? "flex" : "none";
    });
  });
  document.querySelectorAll(".cap .refile .chg").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var cap = btn.closest(".cap");
      var kind = cap.dataset.kind, ref = cap.dataset.ref, to = btn.dataset.to;
      post("/capture/refile", { kind: kind, ref: ref, to: to }).then(function (res) {
        if (!res.ok) { toast("Could not refile"); return; }
        toast("Refiled → " + (res.data.label || to));
        reloadSoon();
      });
    });
  });
  // Delete a captured item (soft-delete + undo). Dispatch by kind, remove the row
  // in place with a fade — no full reload; only undo restores via a minimal reload.
  document.querySelectorAll(".cap .cap-del").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var cap = btn.closest(".cap");
      var kind = cap.dataset.kind, ref = cap.dataset.ref;
      var del = kind === "task" ? "/tasks/" + ref + "/delete" : "/notes/" + ref + "/delete";
      var restore = kind === "task" ? "/tasks/" + ref + "/restore" : "/notes/" + ref + "/restore";
      post(del).then(function (res) {
        if (!res.ok) { toast("Could not delete"); return; }
        cap.classList.add("removing");
        setTimeout(function () { if (cap.parentNode) cap.parentNode.removeChild(cap); }, 220);
        toast("Deleted", function () { post(restore).then(reloadSoon); });
      });
    });
  });

  // ---- goals: manual number inline edit ---------------------------------------
  document.querySelectorAll(".gnum.edit").forEach(function (el) {
    el.addEventListener("click", function () {
      var id = el.dataset.goalId;
      var cur = el.dataset.current || "0";
      var val = window.prompt("Update number", cur);
      if (val === null) return;
      var n = parseFloat(val); if (isNaN(n)) return;
      post("/goals/" + id + "/update", { current: n }).then(reloadSoon);
    });
  });
  // ---- goals: mark a milestone achieved (toggle) ------------------------------
  document.querySelectorAll(".gachieve").forEach(function (el) {
    el.addEventListener("click", function () {
      post("/goals/" + el.dataset.goalId + "/achieve", {}).then(function (res) {
        if (!res.ok) { toast("Could not update"); return; }
        reloadSoon();
      });
    });
  });

  // ---- goals: new-goal form (open/close, timeframe chips, measure reveal) ------
  var goalForm = document.getElementById("goalform");
  var goalNew = document.getElementById("goal-new");
  function closeGoalForm() { if (goalForm) goalForm.classList.add("hide"); }
  if (goalNew && goalForm) {
    goalNew.addEventListener("click", function () {
      goalForm.classList.toggle("hide");
      if (!goalForm.classList.contains("hide")) {
        var t = goalForm.querySelector('input[name="title"]');
        if (t) t.focus();
      }
    });
    var gcancel = document.getElementById("gcancel");
    if (gcancel) gcancel.addEventListener("click", closeGoalForm);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !goalForm.classList.contains("hide")) closeGoalForm();
    });
    // timeframe segmented chips → hidden input; "By date" reveals the date field
    var tfInput = document.getElementById("gtf-input");
    var dateField = document.getElementById("gdatefield");
    goalForm.querySelectorAll("#gtimeframe .qt").forEach(function (chip) {
      chip.addEventListener("click", function () {
        goalForm.querySelectorAll("#gtimeframe .qt").forEach(function (c) { c.classList.remove("active"); });
        chip.classList.add("active");
        var tf = chip.dataset.tf;
        if (tfInput) tfInput.value = tf;
        if (dateField) dateField.classList.toggle("hide", tf !== "by_date");
      });
    });
    // collapsed "add a measurable target" reveal
    var mToggle = document.getElementById("gmeasure-toggle");
    var mBox = document.getElementById("gmeasure");
    if (mToggle && mBox) mToggle.addEventListener("click", function () {
      mBox.classList.toggle("hide");
      mToggle.classList.toggle("hide");
    });
  }

  // ---- journal: add entry -----------------------------------------------------
  var jAdd = document.getElementById("j-add");
  if (jAdd) jAdd.addEventListener("click", function () {
    var box = document.getElementById("j-text"); var t = box.value.trim();
    if (!t) { box.focus(); return; }
    post("/journal/entry", { text: t, day: box.dataset.day || "" }).then(function () { toast("Entry added"); reloadSoon(); });
  });

  // ---- ⋯ reveals row actions on touch (desktop reveals on hover via CSS) -------
  document.querySelectorAll(".cap .cap-more").forEach(function (btn) {
    btn.addEventListener("click", function () { btn.closest(".cap").classList.toggle("acts"); });
  });
  document.querySelectorAll(".jentry .jmore").forEach(function (btn) {
    btn.addEventListener("click", function () { btn.closest(".jentry").classList.toggle("acts"); });
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

  // ---- journal: per-entry edit / delete (byte-preserving, with undo) -----------
  // Undo restores the whole day's page from the prev_raw snapshot the API returns.
  document.querySelectorAll(".jentry .jdel").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var entry = btn.closest(".jentry");
      var day = entry.dataset.day, time = entry.dataset.time, idx = entry.dataset.idx;
      post("/journal/" + day + "/entry/" + time + "/delete", { idx: idx }).then(function (res) {
        if (!res.ok) { toast("Could not delete"); return; }
        var prev = res.data.prev_raw;
        entry.classList.add("removing");
        setTimeout(function () { if (entry.parentNode) entry.parentNode.removeChild(entry); }, 220);
        toast("Entry deleted", function () {
          post("/journal/" + day + "/save", { raw: prev }).then(reloadSoon);
        });
      });
    });
  });
  document.querySelectorAll(".jentry .jedit").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var entry = btn.closest(".jentry");
      if (entry.querySelector(".jedit-area")) return;          // already editing
      var day = entry.dataset.day, time = entry.dataset.time, idx = entry.dataset.idx;
      var p = entry.querySelector(".jtext");
      var area = document.createElement("textarea");
      area.className = "jedit-area";
      area.value = p.textContent;
      var row = document.createElement("div");
      row.className = "erow";
      var cancel = document.createElement("button");
      cancel.type = "button"; cancel.className = "mini"; cancel.textContent = "Cancel";
      var save = document.createElement("button");
      save.type = "button"; save.className = "qgo"; save.textContent = "Save";
      row.appendChild(cancel); row.appendChild(save);
      p.style.display = "none";
      p.after(area, row);
      autogrow(area); area.focus();
      area.addEventListener("input", function () { autogrow(area); });
      function teardown() { area.remove(); row.remove(); p.style.display = ""; }
      cancel.addEventListener("click", teardown);
      save.addEventListener("click", function () {
        post("/journal/" + day + "/entry/" + time + "/save", { idx: idx, text: area.value })
          .then(function (res) {
            if (!res.ok) { toast("Could not save"); return; }
            var prev = res.data.prev_raw;
            p.textContent = area.value.trim();
            teardown();
            toast("Entry updated", function () {
              post("/journal/" + day + "/save", { raw: prev }).then(reloadSoon);
            });
          });
      });
    });
  });
})();
