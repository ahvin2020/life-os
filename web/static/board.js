/* Life OS — tasks board + plan pills + task editor.
   Loads AFTER core.js; calls its globals (post, postJSON, toast, reloadSoon,
   confirmClick, titleSpan, updateRing, bindRingInput) by bare name. Wrapped in
   ONE IIFE so boardStack/recountBoard/applyPlanState stay shared between the
   board code and the task-editor modal (as they were in the old closure).
   Every block is null-guarded so it no-ops on pages without a board/editor. */
(function () {
  "use strict";

  // ---- board helpers (kanban page only; null-guarded elsewhere) ----------------
  function boardStack(col) {
    return document.querySelector('.col[data-col="' + col + '"] .kstack');
  }
  function recountBoard() {
    document.querySelectorAll(".col[data-col]").forEach(function (colEl) {
      var cnt = colEl.querySelector("h3 .count");
      if (cnt) cnt.textContent = colEl.querySelectorAll(".kcard").length;
    });
  }

  // ---- per-row wiring hook: lets a freshly-inserted row (e.g. a just-captured
  // task spliced in without a reload) get the same handlers as page-load rows.
  // wireEditorRow is filled in by the editor IIFE below. ------------------------
  var wireEditorRow = null;
  function wireTaskRow(row) {
    if (!row) return;
    var c = row.querySelector("input.tcheck"); if (c) wireCheckbox(c);
    var p = row.querySelector(".planbtn"); if (p) wirePlan(p);
    if (wireEditorRow) wireEditorRow(row);
  }
  window.LifeOS = window.LifeOS || {};
  window.LifeOS.wireTaskRow = wireTaskRow;

  // ---- task complete with undo (Today rows + week-pool rows; the BOARD has no
  // checkboxes — there, drag-to-Done is the completion gesture) ------------------
  function wireCheckbox(c) {
    c.addEventListener("change", function () {
      var row = c.closest(".task"); var id = c.dataset.taskId;
      row.classList.toggle("done", c.checked);
      if (c.checked) {
        post("/tasks/" + id + "/complete", { done: "1" }).then(function (res) {
          // A recurring task spawns its next occurrence on completion; the undo
          // inverse must also remove that fresh copy (soft-delete, itself undoable).
          var respawn = res.data && res.data.respawned;
          toast("Task completed", function () {
            c.checked = false; row.classList.remove("done");
            post("/tasks/" + id + "/complete", { done: "0" });
            if (respawn) post("/tasks/" + respawn + "/delete");
          });
        });
      } else {
        post("/tasks/" + id + "/complete", { done: "0" });
      }
    });
  }
  document.querySelectorAll(".task input.tcheck").forEach(wireCheckbox);

  // ---- plan-state applier (shared by the pill and the task editor's ☀) ---------
  // Syncs EVERY on-page representation of a task's on-today state — all its pills,
  // its data-planned attrs, and (on the board) its pinned position — so no surface
  // is ever left showing a stale ☀ state.
  function applyPlanState(id, on) {
    document.querySelectorAll('.planbtn[data-task-id="' + id + '"]').forEach(function (p) {
      p.classList.toggle("on", !!on);
      p.textContent = on ? "☀ On today ✓" : "☀ Do today";
    });
    document.querySelectorAll('[data-task-id="' + id + '"]').forEach(function (el) {
      if (el.dataset.planned !== undefined) el.dataset.planned = on ? "1" : "0";
    });
    // On the board, planning MOVES the card: ☀ on → pinned to the top of This
    // week; ☀ off → back to its stored column. A card still due today / overdue
    // stays pinned (a date, not the pill, is what holds it on Today).
    var card = document.querySelector('.kcard[data-task-id="' + id + '"]');
    if (card && boardStack("week")) {
      if (on) {
        card.classList.add("pinned");
        var wk = boardStack("week");
        wk.insertBefore(card, wk.firstChild);
      } else if (!card.querySelector(".due.today, .due.over")) {
        // unticking "On today" does NOT demote the task — not-today ≠
        // not-this-week: it stays in This week, right below the pinned group
        // (the server moves its col to 'week' and bumps it to the top of the
        // unpinned order). Only an explicit drag to Backlog demotes it.
        card.classList.remove("pinned");
        card.dataset.col = "week";
        var wk2 = boardStack("week");
        if (wk2) {
          var next = null, cards = wk2.querySelectorAll(".kcard");
          for (var i = 0; i < cards.length; i++) {
            if (cards[i] !== card && !cards[i].classList.contains("pinned")) { next = cards[i]; break; }
          }
          wk2.insertBefore(card, next);   // before first unpinned; null → append
        }
      }
      recountBoard();
    }
  }

  // ---- home page: move a row between "This week" and "Today" in place ----------
  // Planning from the week pool promotes the row into Today; unticking a Today row
  // drops it back into the week pool. Same DOM node (keeps its listeners), a short
  // fade across — no full-page reload.
  function moveHomeRow(id, on) {
    var hero = document.querySelector(".card.hero");
    var week = document.querySelector(".weekpool");
    if (!hero || !week) return;
    var src = on ? week : hero;
    var row = src.querySelector('.task[data-task-id="' + id + '"], .ptask[data-task-id="' + id + '"]');
    if (!row) return;
    row.style.transition = "opacity var(--dur-fast) var(--ease)";
    row.style.opacity = "0";
    setTimeout(function () {
      if (on) {
        var empty = hero.querySelector(".empty");
        if (empty) empty.remove();
        hero.insertBefore(row, hero.querySelector(".donefold"));   // null target → append
      } else {
        var head = week.querySelector(".chead");
        week.insertBefore(row, head ? head.nextSibling : week.firstChild);
      }
      requestAnimationFrame(function () { row.style.opacity = "1"; });
    }, 160);
  }

  // ---- "Do today" plan pill ---------------------------------------------------
  function wirePlan(b) {
    b.addEventListener("click", function (e) {
      e.stopPropagation();
      var id = b.dataset.taskId;
      var inWeekPool = !!b.closest(".weekpool");
      var inHero = !!b.closest(".card.hero");
      post("/tasks/" + id + "/plan").then(function (res) {
        var on = res.data && res.data.planned;
        // ☀ on a done task reopened it server-side — lift the done styling and
        // point the card at its new home column BEFORE the pin move runs.
        if (res.data && res.data.reopened) {
          document.querySelectorAll('[data-task-id="' + id + '"]').forEach(function (el) {
            el.classList.remove("done");
            var cb = el.querySelector("input.tcheck"); if (cb) cb.checked = false;
          });
          var kc = document.querySelector('.kcard[data-task-id="' + id + '"]');
          if (kc) kc.dataset.col = "week";
        }
        applyPlanState(id, on);
        toast(res.data && res.data.reopened ? "Reopened + planned for today ☀"
              : (on ? "Planned for today ☀" : "Removed from today"));
        // Home page moves between lists re-render: promoting from the "This week"
        // pool into Today, or un-ticking a Today row (it lands in the week pool —
        // not-today ≠ not-this-week).
        if ((inWeekPool && on) || (inHero && !on)) {
          moveHomeRow(id, on);
        }
      });
    });
  }
  document.querySelectorAll(".planbtn").forEach(wirePlan);

  // ---- kanban drag (SortableJS) -----------------------------------------------
  function initKanban() {
    if (typeof Sortable === "undefined") { setTimeout(initKanban, 100); return; }
    document.querySelectorAll(".col[data-col] .kstack").forEach(function (stack) {
      var col = stack.closest(".col").dataset.col;
      Sortable.create(stack, {
        group: "kanban", animation: 140, draggable: ".kcard", ghostClass: "sortable-ghost",
        // interactive controls never start a drag; taps keep their default action
        filter: ".planbtn", preventOnFilter: false,
        // touch: a 150ms hold starts a drag; a plain swipe scrolls the page
        delay: 150, delayOnTouchOnly: true,
        // columns now scroll internally (viewport-height board) — keep drag usable by
        // auto-scrolling the stack under the cursor while dragging near its edges.
        scroll: true, scrollSensitivity: 90, scrollSpeed: 12, bubbleScroll: true,
        onEnd: function (evt) {
          var item = evt.item;
          var id = item.dataset.taskId;
          var destCol = evt.to.closest(".col").dataset.col;

          // ---- pinned (on-today) cards: today-ness governs the drop ----------
          if (item.classList.contains("pinned")) {
            if (destCol === "done") {
              // drag-to-Done IS completion (the board has no checkboxes)
              item.classList.remove("pinned");
              item.classList.add("done");
              item.dataset.col = "done";
              recountBoard();
              post("/tasks/" + id + "/complete", { done: "1" }).then(function (res) {
                var respawn = res.data && res.data.respawned;
                toast("Task completed", function () {
                  post("/tasks/" + id + "/complete", { done: "0" });
                  if (respawn) post("/tasks/" + respawn + "/delete");
                  reloadSoon();            // re-render restores the pinned spot
                });
              });
            } else if (destCol === "backlog") {
              if (item.querySelector(".due.today, .due.over")) {
                // a DATE holds it on Today — the drop can't unpin it; snap back
                var wk = boardStack("week");
                if (wk) { wk.insertBefore(item, wk.firstChild); recountBoard(); }
                toast("Due today — change the due date to move it off today");
              } else {
                // dropping into Backlog = take it off Today AND re-home it there
                post("/tasks/" + id + "/plan").then(function () {
                  item.classList.remove("pinned");
                  item.dataset.planned = "0"; item.dataset.col = "backlog";
                  var p = item.querySelector(".planbtn");
                  if (p) { p.classList.remove("on"); p.textContent = "☀ Do today"; }
                  recountBoard();
                  var ids = [].map.call(evt.to.querySelectorAll(".kcard"), function (k) { return k.dataset.taskId; });
                  postJSON("/tasks/reorder", { col: "backlog", ids: ids });
                  toast("Removed from today", function () {
                    post("/tasks/" + id + "/plan").then(reloadSoon);
                  });
                });
              }
            } else {
              // reorder within This week = reordering today's list (the home page
              // follows the same sort_order). Sort-only persist — NO col write, so
              // a pinned backlog visitor keeps its home column.
              var ids = [].map.call(evt.to.querySelectorAll(".kcard"), function (k) { return k.dataset.taskId; });
              postJSON("/tasks/reorder", { ids: ids });
            }
            return;
          }

          // ---- normal cards ---------------------------------------------------
          // The moved card's visual + dataset state must follow its new column
          // immediately: dragging out of Done un-completes server-side, so the
          // dimmed/struck styling has to lift now, not on the next reload (and
          // vice versa when dropping INTO Done). Counts shift too.
          item.dataset.col = destCol;
          item.classList.toggle("done", destCol === "done");
          recountBoard();
          // cross-column drop: evt.to is where the card landed; the source column's
          // order shifted too, so persist both (once when they're the same list).
          // Pinned cards are excluded from col-writing payloads: they render in
          // This week regardless of their stored col, and a reorder POST must
          // never silently rewrite that col.
          var stacks = evt.to === evt.from ? [evt.to] : [evt.to, evt.from];
          stacks.forEach(function (s) {
            var c = s.closest(".col").dataset.col;
            var ids = [].map.call(s.querySelectorAll(".kcard:not(.pinned)"), function (k) { return k.dataset.taskId; });
            postJSON("/tasks/reorder", { col: c, ids: ids });
          });
        }
      });
    });
  }
  if (document.querySelector(".board")) initKanban();

  // ---- task category filter + live search (Tasks page) ------------------------
  (function () {
    if (!document.querySelector(".board")) return;
    var search = document.getElementById("tsearch");
    var activeCat = "all";
    function apply() {
      var q = search ? search.value.trim().toLowerCase() : "";
      document.querySelectorAll(".kcard").forEach(function (k) {
        var catOk = activeCat === "all" || k.dataset.cat === activeCat;
        var qOk = !q || k.textContent.toLowerCase().indexOf(q) !== -1;
        k.style.display = (catOk && qOk) ? "" : "none";
      });
    }
    document.querySelectorAll(".tagbtn[data-cat]").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll(".tagbtn[data-cat]").forEach(function (x) { x.classList.toggle("active", x === b); });
        activeCat = b.dataset.cat;
        apply();
      });
    });
    if (search) search.addEventListener("input", apply);
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
    var current = null, planned = false, newCol = null;
    var teAttach = document.getElementById("te-attach");
    if (teAttach) initAttach(teAttach);
    // populate goal dropdown
    (window.LIFEOS_GOALS || []).forEach(function (g) {
      var o = document.createElement("option"); o.value = g.id; o.textContent = g.title; f.goal.appendChild(o);
    });

    // ── segmented chip selectors + structured recurrence ─────────────────────
    // The chips and the recurrence picker write into the ORIGINAL hidden inputs
    // (#te-priority / #te-category / #te-col / #te-recur), so every open()/save()
    // line that reads or sets `.value` keeps working unchanged.
    var recurType = document.getElementById("te-recur-type"),
        recurWday = document.getElementById("te-recur-wday"),
        recurMday = document.getElementById("te-recur-mday");
    for (var dm = 1; dm <= 28; dm++) {
      var mo = document.createElement("option"); mo.value = dm; mo.textContent = dm; recurMday.appendChild(mo);
    }
    function reflectSeg(seg, hidden) {
      var v = hidden.value || "";
      [].forEach.call(seg.querySelectorAll(".segbtn"), function (b) {
        b.classList.toggle("on", (b.dataset.val || "") === v);
      });
    }
    function initSeg(segId, hidden) {
      var seg = document.getElementById(segId);
      seg.addEventListener("click", function (e) {
        var b = e.target.closest(".segbtn"); if (!b) return;
        hidden.value = b.dataset.val || ""; reflectSeg(seg, hidden);
      });
      return seg;
    }
    var segPriority = initSeg("te-priority-seg", f.priority),
        segCategory = initSeg("te-category-seg", f.category),
        segCol = initSeg("te-col-seg", f.col);
    // recompose the stored rule string from the type + detail selects
    function composeRecur() {
      var t = recurType.value;
      recurWday.hidden = (t !== "weekly");
      recurMday.hidden = (t !== "monthly");
      if (t === "weekly") f.recur.value = "weekly:" + (recurWday.value || "mon");
      else if (t === "monthly") f.recur.value = "monthly:" + (recurMday.value || "1");
      else f.recur.value = t;                 // "" or "daily"
    }
    recurType.addEventListener("change", composeRecur);
    recurWday.addEventListener("change", composeRecur);
    recurMday.addEventListener("change", composeRecur);
    // parse "daily" | "weekly:sun" | "monthly:12" back into the controls on open
    function parseRecur(rule) {
      rule = rule || "";
      var kind = rule.split(":")[0], detail = rule.split(":")[1] || "";
      recurType.value = (kind === "daily" || kind === "weekly" || kind === "monthly") ? kind : "";
      if (kind === "weekly") recurWday.value = detail || "mon";
      if (kind === "monthly") recurMday.value = detail || "1";
      recurWday.hidden = (kind !== "weekly");
      recurMday.hidden = (kind !== "monthly");
    }
    // reflect the hidden inputs onto the visible controls each time the editor opens
    function syncControls() {
      reflectSeg(segPriority, f.priority);
      reflectSeg(segCategory, f.category);
      reflectSeg(segCol, f.col);
      parseRecur(f.recur.value);
    }

    // Blank editor for ＋ New task — NOTHING persists until Save with a real
    // title (lazy creation, mirroring the notes editor: opening then closing an
    // empty editor leaves no orphan "New task" card).
    function openBlank(col) {
      current = null; planned = false; newCol = col || "backlog";
      f.title.value = ""; f.due.value = ""; f.priority.value = "";
      f.category.value = ""; f.col.value = newCol; f.recur.value = "";
      f.goal.value = "";
      f.plan.classList.remove("on"); f.plan.textContent = "☀ Do today";
      f.saved.textContent = "";
      syncControls();
      var d = document.getElementById("te-delete"); if (d._disarm) d._disarm();
      if (teAttach) setAttach(teAttach, []);
      renderSubs("[]");
      ov.classList.add("on"); f.title.focus();
    }
    function open(el) {
      current = el.dataset.taskId; newCol = null;
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
      syncControls();
      var d = document.getElementById("te-delete"); if (d._disarm) d._disarm();
      if (teAttach) setAttach(teAttach, (el.dataset.media || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean));
      renderSubs(el.dataset.subs);
      ov.classList.add("on"); f.title.focus();
    }
    function addSubRow(s) {
      var row = document.createElement("div"); row.className = "sub" + (s.done ? " done" : "");
      var cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!s.done;
      cb.addEventListener("change", function () {
        post("/tasks/" + s.id + "/complete", { done: cb.checked ? "1" : "0" });
        row.classList.toggle("done", cb.checked);
      });
      var lab = document.createElement("label"); lab.appendChild(titleSpan(s.title));
      row.appendChild(cb); row.appendChild(lab); f.subs.appendChild(row);
    }
    function renderSubs(json) {
      f.subs.innerHTML = "";
      var subs = [];
      try { subs = JSON.parse(json || "[]"); } catch (e) {}
      subs.forEach(addSubRow);
    }
    // ＋ subtask stays IN the editor: the row appends in place (no reload that
    // would tear the editor down mid-flow), and the underlying card's sub-list,
    // ring, and data-subs are kept in sync.
    document.getElementById("te-subadd").addEventListener("click", function () {
      var inp = document.getElementById("te-subnew"); var t = inp.value.trim();
      if (!t) return;
      if (!current) { toast("Save the task first, then add subtasks"); return; }
      post("/tasks/new", { title: t, parent_id: current }).then(function (res) {
        inp.value = "";
        var sid = res.data && res.data.id;
        addSubRow({ id: sid, title: t, done: 0 });
        var src = document.querySelector('[data-task-id="' + current + '"][data-subs]');
        if (src) {
          var subs = [];
          try { subs = JSON.parse(src.dataset.subs || "[]"); } catch (e) {}
          subs.push({ id: sid, title: t, done: 0 });
          src.dataset.subs = JSON.stringify(subs);
          var list = src.querySelector(".subs");
          if (list && sid) {
            var row = document.createElement("div"); row.className = "sub";
            var cb = document.createElement("input"); cb.type = "checkbox";
            cb.dataset.ring = "t" + current; cb.dataset.subId = sid;
            bindRingInput(cb);
            var lab = document.createElement("label"); lab.appendChild(titleSpan(t));
            row.appendChild(cb); row.appendChild(lab); list.appendChild(row);
            updateRing("t" + current);
          }
        }
      });
    });
    f.plan.addEventListener("click", function () {
      if (!current) {                       // blank editor: staged, applied on Save
        planned = !planned;
        f.plan.classList.toggle("on", planned);
        f.plan.textContent = planned ? "☀ On today ✓" : "☀ Do today";
        return;
      }
      post("/tasks/" + current + "/plan").then(function (res) {
        planned = res.data && res.data.planned;
        f.plan.classList.toggle("on", !!planned);
        f.plan.textContent = planned ? "☀ On today ✓" : "☀ Do today";
        applyPlanState(current, planned);   // the page behind must never go stale
      });
    });
    // mirrors the server's due_label filter — the in-place patch must not render
    // a different vocabulary from the next full page load
    function dueLabel(iso, today) {
      if (!iso) return "";
      var d = new Date(iso + "T00:00:00");
      var n = today ? Math.round((new Date(today + "T00:00:00") - d) / 864e5) : null;
      if (n === 0) return "today";
      if (n === 1) return "yesterday";
      if (n > 1) return n + "d over";
      if (n === -1) return "tomorrow";
      if (n !== null && n >= -6) return d.toLocaleString("en-GB", { weekday: "short" });
      var label = d.getDate() + " " + d.toLocaleString("en-GB", { month: "short" });
      if (!today || iso.slice(0, 4) !== today.slice(0, 4)) label += " " + d.getFullYear();
      return label;
    }
    // Save reflects back onto the page in place for the common edits (rename,
    // re-date). Structural changes — priority/category/column/recurrence/goal, or
    // a due chip appearing/disappearing — re-render via reload (the server owns
    // that markup).
    function patchOrReload() {
      var els = document.querySelectorAll('[data-task-id="' + current + '"][data-title]');
      if (!els.length) { reloadSoon(); return; }
      var el0 = els[0];
      var structural =
        (el0.dataset.priority || "") !== f.priority.value ||
        (el0.dataset.category || "") !== f.category.value ||
        (el0.dataset.col || "") !== f.col.value ||
        (el0.dataset.recur || "") !== f.recur.value ||
        (el0.dataset.goalId || "") !== (f.goal.value || "");
      var dueChanged = (el0.dataset.due || "") !== f.due.value;
      els.forEach(function (el) {
        if (structural) return;
        var t = el.querySelector(".taskedit");
        if (t && t.querySelector(".pri, .rec")) { structural = true; return; }
        if (dueChanged && (!el.querySelector(".due") || !f.due.value)) structural = true;
      });
      if (structural) { reloadSoon(); return; }
      var today = window.LIFEOS_TODAY || "";
      els.forEach(function (el) {
        el.dataset.title = f.title.value; el.dataset.due = f.due.value;
        var t = el.querySelector(".taskedit");
        // title text lives in the inner .tt span (strike-through target) where present
        if (t) (t.querySelector(".tt") || t).textContent = f.title.value;
        if (dueChanged) {
          var d = el.querySelector(".due");
          if (d) {
            d.classList.toggle("over", !!(f.due.value && today && f.due.value < today));
            d.classList.toggle("today", !!(today && f.due.value === today));
            d.textContent = dueLabel(f.due.value, today);
          }
        }
      });
    }
    document.getElementById("te-save").addEventListener("click", function () {
      var title = f.title.value.trim();
      if (!current) {                        // lazy create on first real Save
        if (!title) { f.title.focus(); return; }
        var data = {
          title: title, col: f.col.value || newCol || "backlog",
          due_date: f.due.value, priority: f.priority.value,
          category: f.category.value, recur_rule: f.recur.value,
          goal_id: f.goal.value, media: teAttach ? getAttach(teAttach).join(",") : ""
        };
        if (planned) data.planned_on = window.LIFEOS_TODAY || "";
        post("/tasks/new", data).then(function () {
          f.saved.textContent = "saved ✓"; reloadSoon();
        });
        return;
      }
      post("/tasks/" + current + "/edit", {
        title: f.title.value, due_date: f.due.value, priority: f.priority.value,
        category: f.category.value, col: f.col.value, recur_rule: f.recur.value,
        goal_id: f.goal.value, media: teAttach ? getAttach(teAttach).join(",") : ""
      }).then(function () { f.saved.textContent = "saved ✓"; patchOrReload(); });
    });
    confirmClick(document.getElementById("te-delete"), function () {
      var id = current;
      if (!id) { ov.classList.remove("on"); return; }   // blank draft — nothing to delete
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

    // Wire editor-open for a subtree (document at load, or a single spliced-in
    // row). Assigned to the outer wireEditorRow so wireTaskRow() can reuse it.
    var CARD_SEL = ".task[data-task-id], .ptask[data-task-id], .kcard[data-task-id]";
    wireEditorRow = function (root) {
      root.querySelectorAll(".taskedit").forEach(function (el) {
        el.addEventListener("click", function (e) { e.stopPropagation(); open(el.closest("[data-task-id]")); });
      });
      // The WHOLE card/row opens the editor — not just the title. A click anywhere
      // in the panel (category, due, the empty krow band) opens it; clicks that land
      // on a real control — the checkbox, the ☀ pill, a link — keep their own action
      // (the title's .taskedit handler already fires + stops, so no double-open). A
      // drag on the board moves the pointer, so no click fires — this can't hijack it.
      var cards = (root.matches && root.matches(CARD_SEL)) ? [root] : root.querySelectorAll(CARD_SEL);
      cards.forEach(function (card) {
        card.addEventListener("click", function (e) {
          if (e.target.closest("input, button, a, label, .planbtn")) return;
          open(card);
        });
      });
    };
    wireEditorRow(document);

    // ＋ New task / ＋ add task (per column) → blank editor; created on Save
    document.querySelectorAll("[data-newtask]").forEach(function (btn) {
      btn.addEventListener("click", function () { openBlank(btn.dataset.newtask || "backlog"); });
    });
  })();
})();
