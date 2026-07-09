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

  // '/' focuses quick-add (desktop)
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
      var qin = document.getElementById("qin");
      if (qin) { e.preventDefault(); qin.focus(); }
    }
  });

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

  // ---- ☀ plan toggle ----------------------------------------------------------
  document.querySelectorAll(".sunbtn").forEach(function (b) {
    b.addEventListener("click", function (e) {
      e.stopPropagation();
      var id = b.dataset.taskId;
      post("/tasks/" + id + "/plan").then(function (res) {
        var on = res.data && res.data.planned;
        b.classList.toggle("on", !!on);
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
    var current = null, saveTimer = null, pinned = false;
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
    window.openNote = open;
    function save() {
      if (!current) return;
      elSaved.textContent = "saving…";
      post("/notes/" + current + "/save", {
        title: elTitle.value, tags: elTags.value, body: elBody.value,
        pinned: pinned ? "1" : "0"
      }).then(function () { elSaved.textContent = "saved ✓"; });
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
      post("/notes/" + slug + "/delete").then(function () {
        ov.classList.remove("on");
        toast("Note deleted", function () { post("/notes/" + slug + "/restore").then(reloadSoon); });
        reloadSoon();
      });
    });
    function close() { if (current) save(); ov.classList.remove("on"); current = null; }
    document.getElementById("ed-close").addEventListener("click", close);
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && ov.classList.contains("on")) close(); });

    document.querySelectorAll(".note[data-slug]").forEach(function (n) {
      n.addEventListener("click", function () { open(n.dataset.slug); });
    });
    var newBtn = document.getElementById("note-new");
    if (newBtn) newBtn.addEventListener("click", function () {
      post("/notes/new", { title: "Untitled", body: "", tags: "" }).then(function (res) {
        if (res.data && res.data.slug) open(res.data.slug);
      });
    });
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
      f.plan.textContent = planned ? "☀ planned for today" : "☀ plan for today";
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
        f.plan.textContent = planned ? "☀ planned for today" : "☀ plan for today";
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
      post("/tasks/" + id + "/delete").then(function () { ov.classList.remove("on"); toast("Task deleted"); reloadSoon(); });
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
  var goalNew = document.getElementById("goal-new");
  if (goalNew) goalNew.addEventListener("click", function () { document.getElementById("goalform").classList.toggle("hide"); });

  // ---- journal: add entry -----------------------------------------------------
  var jAdd = document.getElementById("j-add");
  if (jAdd) jAdd.addEventListener("click", function () {
    var box = document.getElementById("j-text"); var t = box.value.trim();
    if (!t) { box.focus(); return; }
    post("/journal/entry", { text: t, day: box.dataset.day || "" }).then(function () { toast("Entry added"); reloadSoon(); });
  });
})();
