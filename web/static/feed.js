/* Life OS — Today feed + goals + journal + settings glue.
   Loads AFTER core.js; calls its globals (post, toast, reloadSoon, confirmClick,
   removeWithUndo, autogrow) by bare name. Every block is null-guarded so it
   no-ops on pages without the relevant DOM. Wrapped in one IIFE to keep its
   handful of top-level names (goalForm, jAdd, …) out of the global scope. */
(function () {
  "use strict";

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

  // ---- settings: a disabled schedule hides its controls (time, and day for triage) --
  document.querySelectorAll(".setrow .sctrls > input[type='checkbox']").forEach(function (cb) {
    var ctrls = Array.prototype.filter.call(cb.parentNode.children, function (el) {
      return el !== cb && el.classList.contains("schedctl");
    });
    if (!ctrls.length) return;
    var sync = function () { ctrls.forEach(function (el) { el.style.display = cb.checked ? "" : "none"; }); };
    cb.addEventListener("change", sync);
    sync();
  });

  // ---- settings: run / restart background jobs --------------------------------
  document.querySelectorAll(".runbtn[data-run]").forEach(function (b) {
    b.addEventListener("click", function () {
      var label = b.textContent;
      b.disabled = true; b.textContent = "…";
      post("/settings/run/" + b.dataset.run).then(function (res) {
        b.disabled = false; b.textContent = label;
        var ok = res.ok && res.data && res.data.status === "ok";
        toast((res.data && res.data.message) || (ok ? "Started" : "Couldn't run"));
      });
    });
  });

  // ---- settings: auto-save on change (no Save button) -------------------------
  (function () {
    var form = document.querySelector(".setform");
    if (!form) return;
    var saved = document.getElementById("setsaved");
    var t = null;
    function flash() {
      if (!saved) return;
      saved.classList.add("on");
      clearTimeout(t); t = setTimeout(function () { saved.classList.remove("on"); }, 1600);
    }
    function autosave() {
      post("/settings/save", new FormData(form)).then(function (res) {
        if (res.ok) flash();                                   // 200 = saved (atomic)
        else toast((res.data && res.data.message) || "Couldn't save");   // 400 = validation error
      });
    }
    // toggles/selects fire on flip; text/number/time fire on blur/commit — all via change
    form.addEventListener("change", autosave);
    form.addEventListener("submit", function (e) { e.preventDefault(); autosave(); });
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
    confirmClick(btn, function () {
      var cap = btn.closest(".cap");
      var kind = cap.dataset.kind, ref = cap.dataset.ref;
      var del = kind === "task" ? "/tasks/" + ref + "/delete" : "/notes/" + ref + "/delete";
      var restore = kind === "task" ? "/tasks/" + ref + "/restore" : "/notes/" + ref + "/restore";
      post(del).then(function (res) {
        if (!res.ok) { toast("Could not delete"); return; }
        removeWithUndo(cap, { msg: "Deleted", restore: restore });
      });
    });
  });

  // ---- goals: manual number inline edit (styled input in place — never the
  // native prompt, per the no-browser-default-controls rule) --------------------
  document.querySelectorAll(".gnum.edit").forEach(function (el) {
    el.addEventListener("click", function () {
      if (el.querySelector("input")) return;                 // already editing
      var id = el.dataset.goalId;
      var target = parseFloat(el.dataset.target || "0");
      var unit = el.dataset.unit || "";
      var prevText = el.textContent;
      var inp = document.createElement("input");
      inp.type = "number"; inp.step = "any"; inp.className = "txt gnum-in";
      inp.value = el.dataset.current || "0";
      el.textContent = ""; el.appendChild(inp); inp.focus(); inp.select();
      var doneEditing = false;
      function finish(saveIt) {
        if (doneEditing) return; doneEditing = true;
        var n = parseFloat(inp.value);
        if (!saveIt || isNaN(n)) { el.textContent = prevText; return; }
        post("/goals/" + id + "/update", { current: n }).then(function () {
          el.dataset.current = n;
          el.textContent = Math.round(n) + " / " + Math.round(target) + (unit ? " " + unit : "");
          var goal = el.closest(".goal");
          var bar = goal && goal.querySelector(".bar");
          if (bar) {                                          // fill sweeps in place
            var pct = target ? Math.min(100, n / target * 100) : 0;
            bar.classList.toggle("full", pct >= 100);
            var i = bar.querySelector("i"); if (i) i.style.width = pct + "%";
          }
        });
      }
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter") finish(true);
        if (e.key === "Escape") finish(false);
        e.stopPropagation();
      });
      inp.addEventListener("blur", function () { finish(true); });
      inp.addEventListener("click", function (e) { e.stopPropagation(); });
    });
  });
  // ---- goals: mark achieved — toggles in place (a completion beat, not a reload)
  document.querySelectorAll(".gachieve").forEach(function (el) {
    el.addEventListener("click", function () {
      post("/goals/" + el.dataset.goalId + "/achieve", {}).then(function (res) {
        if (!res.ok) { toast("Could not update"); return; }
        var on = res.data && res.data.achieved;
        el.classList.toggle("done", !!on);
        el.textContent = on ? "✓ achieved" : "mark achieved";
        el.setAttribute("aria-pressed", on ? "true" : "false");
      });
    });
  });
  // ---- goals: delete — arm-then-confirm + Undo toast (parity with tasks/notes) --
  document.querySelectorAll(".goal .gdel").forEach(function (btn) {
    confirmClick(btn, function () {
      var id = btn.dataset.goalId;
      var card = btn.closest(".goal");
      post("/goals/" + id + "/delete").then(function (res) {
        if (!res.ok) { toast("Could not delete"); return; }
        removeWithUndo(card, { label: "Goal", restore: "/goals/" + id + "/restore" });
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

  // ---- journal: per-entry edit / delete (byte-preserving, with undo) -----------
  // Delete is a two-step arm-then-confirm (like tasks/notes), with the Undo toast as
  // the recovery net. Undo restores the whole day's page from prev_raw.
  document.querySelectorAll(".jentry .jdel").forEach(function (btn) {
    confirmClick(btn, function () {
      var entry = btn.closest(".jentry");
      var day = entry.dataset.day, time = entry.dataset.time, idx = entry.dataset.idx;
      post("/journal/" + day + "/entry/" + time + "/delete", { idx: idx }).then(function (res) {
        if (!res.ok) { toast("Could not delete"); return; }
        var prev = res.data.prev_raw;
        // Same-minute siblings are disambiguated by occurrence idx; removing this
        // one shifts the file's indices down, so decrement every later sibling's
        // data-idx in place — otherwise its next edit/delete targets a stale idx (404).
        var delIdx = parseInt(idx, 10);
        document.querySelectorAll(".jentry").forEach(function (e) {
          if (e === entry || e.dataset.day !== day || e.dataset.time !== time) return;
          var ei = parseInt(e.dataset.idx, 10);
          if (ei > delIdx) e.dataset.idx = ei - 1;
        });
        removeWithUndo(entry, {
          label: "Entry", restore: "/journal/" + day + "/save", restoreData: { raw: prev }
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
