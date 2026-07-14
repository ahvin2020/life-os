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
    // File attachments: shared widget + a 📎 that opens the picker. Drag/drop and paste
    // land anywhere on the bar (initAttach binds the .qcap scope).
    var qatt = document.getElementById("q-attach");
    var qclip = document.getElementById("qclip");
    if (qatt) initAttach(qatt);
    if (qclip && qatt) qclip.addEventListener("click", function () {
      var inp = qatt.querySelector('input[type="file"]'); if (inp) inp.click();
    });
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
    function flashIn(el) {
      el.style.transition = "opacity var(--dur-fast) var(--ease)";
      el.style.opacity = "0";
      requestAnimationFrame(function () { el.style.opacity = "1"; });
    }
    function htmlToNode(html) {
      var tmp = document.createElement("div");
      tmp.innerHTML = (html || "").trim();
      return tmp.firstElementChild;
    }
    // Splice the new capture into its home surfaces in place. Returns true if it
    // was fully handled (so the composer can skip the reload); false → reload, to
    // cover kinds/edge-states with no partial (journal) or a missing container.
    // Returns true when the capture was shown (or needs no slot) → composer skips the
    // reload. A task splices into This-week; a note/journal has no card on Today anymore,
    // so the toast is the whole confirmation. Only a task with no week card yet reloads.
    function insertCapture(d) {
      if (d.week_html) {
        var week = document.querySelector(".weekpool");
        if (week) {
          var row = htmlToNode(d.week_html);
          var whead = week.querySelector(".chead");
          week.insertBefore(row, whead ? whead.nextSibling : week.firstChild);
          if (window.LifeOS && window.LifeOS.wireTaskRow) window.LifeOS.wireTaskRow(row);
          flashIn(row); return true;
        }
        return false;   // no week-pool card yet → reload to render one
      }
      return true;       // note/journal: toast is enough, no reload
    }
    var busy = false;
    function add() {
      if (busy) return;                                // guard against double-tap / rapid Enter
      var text = qin.value.trim();
      var media = qatt ? getAttach(qatt).join(",") : "";
      if (!text && !media) { qin.focus(); return; }   // a lone attachment is enough
      var active = document.querySelector("#qtypes .qt.active");
      var type = active ? active.dataset.t : "auto";
      busy = true;
      post("/capture", { text: text, type: type, media: media }).then(function (res) {
        busy = false;
        if (!res.ok) { toast("Could not add"); return; }
        qgo.textContent = "✓ Added"; qgo.classList.add("did");
        qin.value = ""; manual = null; setActive("auto"); autogrow(qin);   // shrink back to one row
        if (qatt) setAttach(qatt, []);
        toast("Added → " + (res.data.label || "filed"));
        var handled = insertCapture(res.data || {});
        setTimeout(function () {
          qgo.textContent = "Add"; qgo.classList.remove("did");
          if (!handled) reloadSoon();
        }, 900);
      }).catch(function () { busy = false; toast("Could not add"); });
    }
    if (qgo) qgo.addEventListener("click", add);
    // Enter submits; Shift+Enter (and mobile return) inserts a newline — the composer is a
    // textarea, so multi-line captures (a note with line breaks) work like Todoist/Notion.
    qin.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); add(); }
    });
  })();

  // ---- Today calendar: view-only day/week/month, lazy-loaded (Google events) ---
  (function () {
    var card = document.getElementById("calcard");
    if (!card) return;
    var body = document.getElementById("calbody");
    var label = document.getElementById("callabel");
    var views = document.getElementById("calviews");
    var nav = card.querySelector(".calnav");
    var agBody = document.getElementById("agbody");
    var calopen = document.getElementById("calopen");
    var ov = document.getElementById("caloverlay");
    var calclose = document.getElementById("calclose");

    var VIEW_KEY = "lifeos_cal_view";
    // ?calview=week deep-links a view; otherwise remember the last-chosen one. Date always
    // resets to today on load (per spec).
    var qView = new URLSearchParams(location.search).get("calview");
    var view = ["day", "week", "month"].indexOf(qView) >= 0 ? qView : localStorage.getItem(VIEW_KEY);
    if (["day", "week", "month"].indexOf(view) < 0) view = "day";
    var anchor = startOfDay(new Date());
    var monthSel = null;               // selected day (ISO) in Month view → peek its events
    var cache = {};                    // "start|end" -> events[]
    var connected = true;

    var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    var DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

    function startOfDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
    function iso(d) {
      var m = d.getMonth() + 1, day = d.getDate();
      return d.getFullYear() + "-" + (m < 10 ? "0" + m : m) + "-" + (day < 10 ? "0" + day : day);
    }
    function addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
    function addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, 1); }
    function mondayOf(d) { return addDays(d, -((d.getDay() + 6) % 7)); }   // week starts Monday
    // innerHTML escapes < > &, but NOT quotes — so also entity-encode " and ' since esc()
    // output goes inside title="…" attributes (a raw " would break out → attribute XSS).
    function esc(s) { var e = document.createElement("div"); e.textContent = s == null ? "" : s; return e.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }
    function timeOf(ev) {
      if (ev.all_day) return "";
      var s = ev.start || "";
      return s.indexOf("T") >= 0 ? s.slice(11, 16) : "";
    }
    function eventsOn(list, dstr) { return list.filter(function (ev) { return ev.date === dstr; }); }

    function rangeFor() {
      if (view === "day") return { start: anchor, end: anchor };
      if (view === "week") { var m = mondayOf(anchor); return { start: m, end: addDays(m, 6) }; }
      var first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
      var last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
      return { start: mondayOf(first), end: addDays(last, 6 - ((last.getDay() + 6) % 7)) };
    }
    function labelText() {
      if (view === "day") return DOW[(anchor.getDay() + 6) % 7] + " " + anchor.getDate() + " " + MON[anchor.getMonth()];
      if (view === "week") {
        var m = mondayOf(anchor), e = addDays(m, 6);
        return m.getMonth() === e.getMonth()
          ? m.getDate() + "–" + e.getDate() + " " + MON[m.getMonth()]
          : m.getDate() + " " + MON[m.getMonth()] + " – " + e.getDate() + " " + MON[e.getMonth()];
      }
      return MON[anchor.getMonth()] + " " + anchor.getFullYear();
    }

    function fetchRange(cb) {
      // Always fetch the month-grid window around the anchor (which pads a few days
      // into the neighbouring months), NOT just the current view's range. Day/week/
      // month all fall inside it and filter locally by date — so switching views is
      // instant (one cached set) instead of a Google round-trip each time.
      var first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
      var last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
      var start = mondayOf(first), end = addDays(last, 6 - ((last.getDay() + 6) % 7));
      var key = iso(start) + "|" + iso(end);
      if (cache[key]) { cb(cache[key]); return; }
      fetch("/calendar/events?start=" + iso(start) + "&end=" + iso(end),
            { headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (res) { return res.json(); })
        .then(function (d) { connected = d.connected; cache[key] = d.events || []; cb(cache[key]); })
        .catch(function () { cb(null); });
    }

    // ── Google-style time-grid (day/week) ────────────────────────────────────
    var HOUR_H = 42;   // px per hour; keep in sync with the gridline gradient in CSS
    function hourLabel(h) { if (h === 0) return ""; var ap = h < 12 ? "AM" : "PM", x = h % 12 || 12; return x + " " + ap; }
    function shortTime(t) {
      if (!t) return "";
      var hh = parseInt(t.slice(0, 2), 10), mm = t.slice(3), ap = hh < 12 ? "am" : "pm", h = hh % 12 || 12;
      return mm === "00" ? h + ap : h + ":" + mm + ap;
    }
    function dayParts(list, dstr) {
      var allDay = [], timed = [];
      eventsOn(list, dstr).forEach(function (ev) {
        if (ev.all_day) { allDay.push(ev); return; }
        var s = ev.start || "", e = ev.end || "";
        var sm = s.indexOf("T") >= 0 ? parseInt(s.slice(11, 13), 10) * 60 + parseInt(s.slice(14, 16), 10) : 0;
        var em = e.indexOf("T") >= 0 ? parseInt(e.slice(11, 13), 10) * 60 + parseInt(e.slice(14, 16), 10) : sm + 60;
        if (em <= sm) em = sm + 30;
        timed.push({ startMin: Math.max(0, sm), endMin: Math.min(1440, em), ev: ev });
      });
      return { allDay: allDay, timed: timed };
    }
    function layoutColumns(timed) {   // greedy interval-partition → .col + .ncols per overlap cluster
      timed.sort(function (a, b) { return a.startMin - b.startMin || a.endMin - b.endMin; });
      var group = [], groupEnd = -1;
      function flush() {
        if (!group.length) return;
        var cols = [];
        group.forEach(function (e) {
          var placed = false;
          for (var c = 0; c < cols.length; c++) { if (cols[c] <= e.startMin) { e.col = c; cols[c] = e.endMin; placed = true; break; } }
          if (!placed) { e.col = cols.length; cols.push(e.endMin); }
        });
        group.forEach(function (e) { e.ncols = cols.length; });
        group = []; groupEnd = -1;
      }
      timed.forEach(function (e) { if (group.length && e.startMin >= groupEnd) flush(); group.push(e); groupEnd = Math.max(groupEnd, e.endMin); });
      flush();
      return timed;
    }
    function colHtml(parts, isToday) {
      var html = '<div class="cgcol' + (isToday ? " istoday" : "") + '">';
      if (isToday) { var now = new Date(); html += '<div class="cgnow" style="top:' + ((now.getHours() * 60 + now.getMinutes()) / 60 * HOUR_H) + 'px"></div>'; }
      layoutColumns(parts.timed).forEach(function (e) {
        var top = e.startMin / 60 * HOUR_H, h = Math.max(15, (e.endMin - e.startMin) / 60 * HOUR_H), w = 100 / e.ncols;
        html += '<div class="cgev" style="top:' + top + 'px;height:' + (h - 1) + 'px;left:' + (e.col * w) + '%;width:' + (w - 1.5) + '%" title="' + esc(e.ev.summary) + '">'
          + '<span class="cgevt mono">' + shortTime(timeOf(e.ev)) + '</span> ' + esc(e.ev.summary) + '</div>';
      });
      return html + '</div>';
    }
    function timeGrid(days) {   // days: [{dstr,label,isToday,parts}]
      var n = days.length, tpl = 'style="grid-template-columns:52px repeat(' + n + ',minmax(0,1fr))"';
      var head = '<div class="cghead" ' + tpl + '><div class="cggcorner"></div>';
      days.forEach(function (d) { head += '<div class="cgdaylabel' + (d.isToday ? " istoday" : "") + '"><span class="mono">' + d.label + '</span></div>'; });
      head += '</div>';
      var anyAD = days.some(function (d) { return d.parts.allDay.length; }), ad = "";
      if (anyAD) {
        ad = '<div class="cgallday" ' + tpl + '><div class="cggutlbl mono">all-day</div>';
        days.forEach(function (d) {
          ad += '<div class="cgadcell">' + d.parts.allDay.map(function (ev) { return '<div class="cgadev" title="' + esc(ev.summary) + '">' + esc(ev.summary) + '</div>'; }).join("") + '</div>';
        });
        ad += '</div>';
      }
      var gutter = '<div class="cggutter">';
      for (var h = 0; h < 24; h++) gutter += '<div class="cghour"><span>' + hourLabel(h) + '</span></div>';
      gutter += '</div>';
      var cols = ""; days.forEach(function (d) { cols += colHtml(d.parts, d.isToday); });
      var gridStyle = 'style="grid-template-columns:52px repeat(' + n + ',minmax(0,1fr));height:' + (24 * HOUR_H) + 'px"';
      var grid = '<div class="cgbody"><div class="cggrid" ' + gridStyle + '>' + gutter + cols + '</div></div>';
      return '<div class="cgrid' + (n > 1 ? " wk" : "") + '">' + head + ad + grid + '</div>';
    }
    function scrollMorning() { var b = body.querySelector(".cgbody"); if (b) b.scrollTop = 7 * HOUR_H - 6; }

    function renderDay(list) {
      var dstr = iso(anchor), todayStr = iso(startOfDay(new Date()));
      body.innerHTML = timeGrid([{ dstr: dstr, label: DOW[(anchor.getDay() + 6) % 7] + " " + anchor.getDate(),
        isToday: dstr === todayStr, parts: dayParts(list, dstr) }]);
      scrollMorning();
    }
    function renderWeek(list) {
      var m = mondayOf(anchor), todayStr = iso(startOfDay(new Date())), days = [];
      for (var i = 0; i < 7; i++) {
        var dd = addDays(m, i), ds = iso(dd);
        days.push({ dstr: ds, label: DOW[i] + " " + dd.getDate(), isToday: ds === todayStr, parts: dayParts(list, ds) });
      }
      body.innerHTML = timeGrid(days);
      scrollMorning();
    }
    function renderMonth(list) {
      var r = rangeFor(), todayStr = iso(startOfDay(new Date())), cur = anchor.getMonth(), byDate = {};
      list.forEach(function (ev) { (byDate[ev.date] = byDate[ev.date] || []).push(ev); });
      var html = '<div class="cmonth"><div class="cmdow">';
      DOW.forEach(function (x) { html += "<span>" + x + "</span>"; });
      html += '</div><div class="cmgrid">';
      var d = new Date(r.start);
      for (var i = 0; i < 42 && d <= r.end; i++) {
        var dstr = iso(d), evs = byDate[dstr] || [];
        html += '<div class="cmcell' + (d.getMonth() === cur ? "" : " out") + (dstr === todayStr ? " istoday" : "") + '" data-date="' + dstr + '">'
          + '<div class="cmnum mono">' + d.getDate() + '</div><div class="cmevs">';
        evs.slice(0, 3).forEach(function (ev) {
          html += '<div class="cmev' + (ev.all_day ? " allday" : "") + '" title="' + esc(ev.summary) + '">'
            + (ev.all_day ? "" : '<span class="cmt mono">' + shortTime(timeOf(ev)) + '</span> ')
            + '<span class="cmname">' + esc(ev.summary) + '</span></div>';
        });
        if (evs.length > 3) html += '<div class="cmmore">+' + (evs.length - 3) + ' more</div>';
        html += '</div></div>';
        d = addDays(d, 1);
      }
      body.innerHTML = html + '</div></div>';
      Array.prototype.forEach.call(body.querySelectorAll(".cmcell"), function (b) {
        b.addEventListener("click", function () { var p = b.dataset.date.split("-"); anchor = new Date(+p[0], +p[1] - 1, +p[2]); setView("day"); });
      });
    }

    // ── Compact agenda ("Up next") — the default surface in the right rail ────
    // Rolling: leads with Today/Tomorrow, then day-by-day through the next week,
    // capped so a busy week can't stretch the column. The month/week grids stay
    // one click away (calcard below), for when the bird's-eye is wanted.
    var AG_DAYS = 7, AG_CAP = 10;
    function agHeader(i, d) {
      if (i === 0) return "Today";
      if (i === 1) return "Tomorrow";
      return DOW[(d.getDay() + 6) % 7] + " " + d.getDate() + " " + MON[d.getMonth()];
    }
    function renderAgenda(d) {
      if (d === null) { agBody.innerHTML = '<div class="empty">Couldn’t load calendar.</div>'; return; }
      if (!d.connected) { agBody.innerHTML = '<div class="empty">Connect Google in <a href="/settings">Settings</a> to see your calendar.</div>'; return; }
      var list = d.events || [], today0 = startOfDay(new Date()), shown = 0, more = 0, html = "";
      for (var i = 0; i < AG_DAYS; i++) {
        var dd = addDays(today0, i);
        var evs = eventsOn(list, iso(dd)).sort(function (a, b) {
          if (a.all_day !== b.all_day) return a.all_day ? -1 : 1;      // all-day first
          return (a.start || "").localeCompare(b.start || "");
        });
        if (!evs.length) continue;
        var rows = "";
        evs.forEach(function (ev) {
          if (shown >= AG_CAP) { more += 1; return; }
          rows += '<div class="agev' + (ev.all_day ? " allday" : "") + '" title="' + esc(ev.summary) + '">'
            + '<span class="agt mono">' + (ev.all_day ? "all-day" : shortTime(timeOf(ev))) + '</span>'
            + '<span class="agn">' + esc(ev.summary) + '</span></div>';
          shown += 1;
        });
        if (!rows) continue;
        html += '<div class="agday' + (i === 0 ? " istoday" : "") + '"><div class="agdh mono">'
          + agHeader(i, dd) + '</div>' + rows + '</div>';
      }
      if (!html) { agBody.innerHTML = '<div class="empty">Nothing scheduled this week — enjoy the calm.</div>'; return; }
      if (more) html += '<div class="agmore mono">+' + more + ' more — open the full calendar</div>';
      agBody.innerHTML = html;
    }
    function loadAgenda(silent) {
      var s = startOfDay(new Date()), e = addDays(s, AG_DAYS - 1);
      if (!silent) agBody.innerHTML = '<div class="empty">Loading…</div>';
      fetch("/calendar/events?start=" + iso(s) + "&end=" + iso(e),
            { headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) { return r.json(); })
        .then(renderAgenda)
        .catch(function () { renderAgenda(null); });
    }

    function render(silent) {
      label.textContent = labelText();
      Array.prototype.forEach.call(views.querySelectorAll(".calview"), function (b) {
        b.classList.toggle("on", b.dataset.view === view);
      });
      if (!silent) body.innerHTML = '<div class="empty">Loading…</div>';
      fetchRange(function (list) {
        if (list === null) { body.innerHTML = '<div class="empty">Couldn’t load calendar.</div>'; return; }
        if (!connected) { body.innerHTML = '<div class="empty">Connect Google in <a href="/settings">Settings</a> to see your calendar.</div>'; return; }
        if (view === "day") renderDay(list);
        else if (view === "week") renderWeek(list);
        else renderMonth(list);
      });
    }
    function setView(v) { view = v; localStorage.setItem(VIEW_KEY, v); render(); }

    views.addEventListener("click", function (e) {
      var b = e.target.closest(".calview"); if (b) setView(b.dataset.view);
    });
    nav.addEventListener("click", function (e) {
      var b = e.target.closest("[data-cal]"); if (!b) return;
      var act = b.dataset.cal, dir = act === "next" ? 1 : -1;
      if (act === "today") anchor = startOfDay(new Date());
      else if (view === "day") anchor = addDays(anchor, dir);
      else if (view === "week") anchor = addDays(anchor, 7 * dir);
      else anchor = addMonths(anchor, dir);
      render();
    });

    // The full grid is a destination, not the default: it opens in a modal overlay (the app's
    // note/task-editor pattern) — centered over the page, closed by ✕ / backdrop / Esc — rather
    // than expanding inline at the bottom (which read as "weird" and buried the rest of the page).
    function openCal() { if (ov) { ov.classList.add("on"); render(); } }
    function closeCal() { if (ov) ov.classList.remove("on"); }
    if (calopen) calopen.addEventListener("click", openCal);
    if (calclose) calclose.addEventListener("click", closeCal);
    if (ov) ov.addEventListener("click", function (e) { if (e.target === ov) closeCal(); });  // backdrop
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && ov && ov.classList.contains("on")) closeCal();
    });

    // Default surface: the compact agenda. A ?calview= deep-link still opens the grid.
    loadAgenda();
    if (["day", "week", "month"].indexOf(qView) >= 0) openCal();

    // Auto-refresh when the tab regains focus — a calendar left open otherwise goes
    // stale (events added elsewhere never appear). Throttled so flicking between tabs
    // doesn't hammer Google; silent so there's no Loading flicker on return.
    var lastRefresh = Date.now();
    function maybeRefresh() {
      if (document.hidden || Date.now() - lastRefresh < 30000) return;
      lastRefresh = Date.now();
      cache = {};                          // drop the session cache → re-pull fresh
      loadAgenda(true);
      if (ov && ov.classList.contains("on")) render(true);
    }
    document.addEventListener("visibilitychange", maybeRefresh);
    window.addEventListener("focus", maybeRefresh);
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

  // ---- settings: run / restart background jobs (capture/triage/backup) ---------
  // After a job runs (async — takes a couple seconds to stamp its heartbeat), re-fetch
  // and refresh the health rows in place: honest new timestamp + dot, no fake "just now",
  // no full-page reload. Buttons keep their handlers (we swap only the dot class + text).
  function refreshHealthRows() {
    fetch("/settings").then(function (r) { return r.text(); }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      document.querySelectorAll(".setrow[data-health]").forEach(function (live) {
        var fresh = doc.querySelector('.setrow[data-health="' + live.dataset.health + '"]');
        if (!fresh) return;
        var fd = fresh.querySelector(".slabel .dot"), ld = live.querySelector(".slabel .dot");
        if (fd && ld) ld.className = fd.className;
        var fx = fresh.querySelector(".sdesc"), lx = live.querySelector(".sdesc");
        if (fx && lx) lx.innerHTML = fx.innerHTML;
      });
    });
  }
  document.querySelectorAll('.runbtn[data-run]:not([data-run="claude"])').forEach(function (b) {
    b.addEventListener("click", function () {
      var label = b.textContent;
      b.disabled = true; b.textContent = "…";
      post("/settings/run/" + b.dataset.run).then(function (res) {
        b.disabled = false; b.textContent = label;
        var ok = res.ok && res.data && res.data.status === "ok";
        toast((res.data && res.data.message) || (ok ? "Started" : "Could not run"));
        if (ok) setTimeout(refreshHealthRows, 2500);   // let the job finish, then refresh
      });
    });
  });

  // ---- settings: connection cards (Claude / Google / Dropbox / Telegram) --------
  // Every state change (save creds, connect a token, disconnect) updates the card IN
  // PLACE: POST, then fetch /settings and swap just this <form> for its freshly-rendered
  // version and re-bind it — no full-page reload, no scroll jump.
  function bindCopy(b) {
    b.addEventListener("click", function () {
      var txt = b.dataset.copy, done = function () { toast("Copied  " + txt); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(done, done);
      else {
        var ta = document.createElement("textarea");
        ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); } catch (e) {}
        document.body.removeChild(ta); done();
      }
    });
  }
  function swapCard(form) {
    fetch("/settings", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html").getElementById(form.id);
        if (!fresh) { window.location.reload(); return; }
        form.replaceWith(fresh);
        bindConnCard(fresh);
        fresh.querySelectorAll(".cmdcopy[data-copy]").forEach(bindCopy);
      })
      .catch(function () { window.location.reload(); });
  }
  function bindConnCard(form) {
    if (!form) return;
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = form.querySelector('button[type="submit"]');
      if (!btn) return;
      var lbl = btn.textContent;
      if (form.id === "aiform") {                        // Claude: validate the token, then save
        var field = form.querySelector('input[name="oauth_token"]');
        if (!field || !field.value.trim()) { toast("Paste a token first"); return; }
        btn.textContent = "…"; btn.disabled = true;
        post("/settings/run/claude", new FormData(form)).then(function (res) {
          if (!(res.ok && res.data && res.data.status === "ok")) { btn.textContent = lbl; btn.disabled = false; toast((res.data && res.data.message) || "That token didn't work"); return; }
          post("/settings/claude-token", new FormData(form)).then(function (r2) {
            if (r2.ok) { toast("Connected ✓"); swapCard(form); } else { btn.textContent = lbl; btn.disabled = false; toast("Couldn't save the token"); }
          });
        });
      } else {                                           // google / dropbox / telegram: save creds
        btn.textContent = "…"; btn.disabled = true;
        post(form.getAttribute("action"), new FormData(form)).then(function (res) {
          if (res.ok && (!res.data || res.data.status !== "error")) { toast("Saved ✓"); swapCard(form); }
          else { btn.textContent = lbl; btn.disabled = false; toast((res.data && res.data.message) || "Could not save"); }
        });
      }
    });
    // "Replace" toggles the credential inputs. While OPEN you're editing creds, so the
    // Sign-in action is irrelevant and hides entirely (one clear action: Save). While
    // CLOSED, Sign-in is the single primary action.
    form.querySelectorAll("[data-reveal]").forEach(function (b) {
      b.addEventListener("click", function () {
        var el = document.getElementById(b.dataset.reveal); if (!el) return;
        el.hidden = !el.hidden;
        var connect = form.querySelector("a[data-connect]"), save = el.querySelector('button[type="submit"]');
        if (el.hidden) { if (connect) connect.parentElement.hidden = false; if (save) save.classList.remove("primary"); }
        else { if (connect) connect.parentElement.hidden = true; if (save) save.classList.add("primary"); }
      });
    });
    // Test: live-ping the provider's API (catches an expired token that "Connected" hides).
    // Shows ✓/✗ inline; if it detected a new failure it swaps to surface the red note.
    form.querySelectorAll("[data-test]").forEach(function (b) {
      var orig = b.textContent;                         // stable label, captured once
      b.addEventListener("click", function () {
        if (b._revert) { clearTimeout(b._revert); b._revert = null; }   // re-click: cancel a pending revert
        b.disabled = true; b.textContent = "…";
        b.classList.remove("ok", "fail"); b.removeAttribute("aria-label");
        post("/settings/test/" + b.dataset.test).then(function (res) {
          var ok = res.ok && res.data && res.data.status === "ok";
          var msg = (res.data && res.data.message) || (ok ? "Reachable" : "Not reachable");
          b.disabled = false; b.textContent = ok ? "✓" : "✗"; b.classList.add(ok ? "ok" : "fail");
          b.setAttribute("aria-label", orig + ": " + msg);    // the glyph carries a screen-reader label
          toast(msg);
          b._revert = setTimeout(function () {
            b.textContent = orig; b.classList.remove("ok", "fail"); b.removeAttribute("aria-label"); b._revert = null;
          }, 8000);
          if (!ok) setTimeout(function () { swapCard(form); }, 1200);   // show the red failing note
        });
      });
    });
    // Disconnect: two-step arm-then-confirm (like deletes), then swap in place.
    form.querySelectorAll("[data-disconnect], [data-disconnect-claude]").forEach(function (b) {
      var isClaude = b.hasAttribute("data-disconnect-claude");
      confirmClick(b, function () {
        b.disabled = true; b.textContent = "…";
        var p;
        if (isClaude) { var fd = new FormData(); fd.append("ai_provider", "claude"); p = post("/settings/claude-token", fd); }
        else { p = post("/settings/" + b.dataset.disconnect + "/disconnect"); }
        p.then(function () { toast("Disconnected"); swapCard(form); });
      }, "Confirm disconnect?");
    });
  }
  ["aiform", "googleform", "dropboxform", "telegramform"].forEach(function (id) { bindConnCard(document.getElementById(id)); });
  document.querySelectorAll(".cmdcopy[data-copy]").forEach(bindCopy);

  // App URL autosaves on change. The Google/Dropbox "copy callback" chips are built from
  // it (a stale chip → redirect_uri_mismatch), so refresh just those cards in place —
  // no full-page reload, consistent with every other card here.
  (function () {
    var f = document.getElementById("appurlform"); if (!f) return;
    var i = f.querySelector("[data-autosave]"); if (!i) return;
    i.addEventListener("change", function () {
      post(f.getAttribute("action"), new FormData(f)).then(function (res) {
        var ok = res.ok && (!res.data || res.data.status !== "error");
        if (!ok) { toast((res.data && res.data.message) || "Could not save"); return; }
        toast("App URL saved ✓");
        ["googleform", "dropboxform"].forEach(function (id) {
          var card = document.getElementById(id); if (card) swapCard(card);
        });
      });
    });
  })();

  // ---- settings: on arrival, pulse + scroll to whatever needs attention ---------
  // (what the red Settings nav badge counts: the AI card if it's not connected, plus
  // any stale System-health row). Targets are collected in DOM order so we scroll to
  // the topmost issue.
  (function () {
    var targets = [];
    // AI card: its token dot is not 'ok' (stale OR off/not-connected) → the badge counts
    // it, so highlight it too. It sits above System health, so push it first.
    var aiform = document.getElementById("aiform");
    if (aiform && aiform.querySelector(".dot.stale, .dot.off")) targets.push(aiform);
    // System-health rows: only stale (red) counts, matching the badge (never-run 'off' jobs
    // auto-start and don't nag).
    Array.prototype.slice.call(document.querySelectorAll(".setrow")).forEach(function (r) {
      if (r.closest("#aiform")) return;                 // AI already handled above
      if (r.querySelector(".dot.stale")) targets.push(r);
    });
    if (!targets.length) return;
    targets.forEach(function (t) { t.classList.add("attn"); });
    targets[0].scrollIntoView({ behavior: "smooth", block: "center" });
  })();

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
        else toast((res.data && res.data.message) || "Could not save");   // 400 = validation error
      });
    }
    // toggles/selects fire on flip; text/number/time fire on blur/commit — all via change
    form.addEventListener("change", autosave);
    form.addEventListener("submit", function (e) { e.preventDefault(); autosave(); });
  })();

  // ---- captured-today feed: wire ONE row's actions (Change/refile, Delete, ⋯) so
  // a just-captured item spliced in without a reload behaves like a page-load row.
  function wireCap(cap) {
    var toggle = cap.querySelector(".chg-toggle");
    if (toggle) toggle.addEventListener("click", function () {
      var box = cap.querySelector(".refile");
      if (box) box.style.display = box.style.display === "none" ? "flex" : "none";
    });
    cap.querySelectorAll(".refile .chg").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var kind = cap.dataset.kind, ref = cap.dataset.ref, to = btn.dataset.to;
        post("/capture/refile", { kind: kind, ref: ref, to: to }).then(function (res) {
          if (!res.ok) { toast("Could not refile"); return; }
          toast("Refiled → " + (res.data.label || to));
          reloadSoon();
        });
      });
    });
    // Delete (soft-delete + undo): remove the row in place with a fade — no full
    // reload; only undo restores via a minimal reload.
    var del = cap.querySelector(".cap-del");
    if (del) confirmClick(del, function () {
      var kind = cap.dataset.kind, ref = cap.dataset.ref;
      var delUrl = kind === "task" ? "/tasks/" + ref + "/delete" : "/notes/" + ref + "/delete";
      var restore = kind === "task" ? "/tasks/" + ref + "/restore" : "/notes/" + ref + "/restore";
      post(delUrl).then(function (res) {
        if (!res.ok) { toast("Could not delete"); return; }
        removeWithUndo(cap, { label: (kind === "task" ? "Task" : "Note"), restore: restore });
      });
    });
    // ⋯ reveals row actions on touch (desktop reveals on hover via CSS)
    var more = cap.querySelector(".cap-more");
    if (more) more.addEventListener("click", function () { cap.classList.toggle("acts"); });
  }
  document.querySelectorAll(".cap").forEach(wireCap);
  window.LifeOS = window.LifeOS || {};
  window.LifeOS.wireCap = wireCap;

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
          el.textContent = Math.round(n) + "/" + Math.round(target) + (unit ? " " + unit : "");
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
        el.textContent = on ? "✓ Achieved" : "Mark achieved";
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
  var jAttach = document.getElementById("j-attach");
  if (jAttach) initAttach(jAttach);
  var jAdd = document.getElementById("j-add");
  var jBusy = false;
  if (jAdd) jAdd.addEventListener("click", function () {
    if (jBusy) return;                                 // guard against double-tap
    var box = document.getElementById("j-text"); var t = box.value.trim();
    var media = jAttach ? getAttach(jAttach).join(",") : "";
    if (!t && !media) { box.focus(); return; }
    jBusy = true;
    post("/journal/entry", { text: t, day: box.dataset.day || "", media: media })
      .then(function () { toast("Entry added"); reloadSoon(); })
      .catch(function () { jBusy = false; toast("Could not add"); });
  });
  // journal entry galleries → smart media modal on tap (images inline, files carded)
  document.querySelectorAll(".jgimg, .jgfile").forEach(function (im) {
    im.addEventListener("click", function () { openMedia(im.dataset.full || im.src, im.dataset.name); });
  });

  // ---- ⋯ reveals row actions on touch (desktop reveals on hover via CSS) -------
  // (.cap .cap-more is wired in wireCap; journal entries handled here)
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
