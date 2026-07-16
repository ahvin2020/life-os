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
    // File attachments: shared widget + a 📎 that opens the picker. Drag/drop and paste
    // land anywhere on the bar (initAttach binds the .qcap scope).
    var qatt = document.getElementById("q-attach");
    var qclip = document.getElementById("qclip");
    if (qatt) initAttach(qatt);
    if (qclip && qatt) qclip.addEventListener("click", function () {
      var inp = qatt.querySelector('input[type="file"]'); if (inp) inp.click();
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
    // Freeze/unfreeze the composer for the duration of an in-flight capture: the textarea
    // goes readOnly (no edits to already-sent text) and the wrapper carries .sending so the
    // field can dim while it works.
    function setSending(on) {
      qin.readOnly = on;
      var wrap = qin.closest(".qcap");
      if (wrap) wrap.classList.toggle("sending", on);
    }
    function add() {
      if (busy) return;                                // guard against double-tap / rapid Enter
      var text = qin.value.trim();
      var media = qatt ? getAttach(qatt).join(",") : "";
      if (!text && !media) { qin.focus(); return; }   // a lone attachment is enough
      busy = true;
      // Lock the field while the capture is in flight: the text has already been sent, so
      // editing it here would either do nothing or be silently wiped when the reply lands.
      // readOnly keeps the caret but blocks typing/paste; unlocked on every exit path.
      setSending(true);
      // Pure text runs the AI router server-side (~1-2s), which infers the kind (task, note,
      // idea, journal, reminder) and acts — show a thinking state so the wait reads as work,
      // not a hang. An attachment (or a pasted URL) stays on the instant deterministic path.
      var aiLikely = !!text && !media;
      if (aiLikely) { qgo.classList.add("spin"); }       // spinner while the router thinks
      post("/capture", { text: text, type: "auto", media: media }).then(function (res) {
        busy = false; setSending(false); qgo.classList.remove("spin");
        if (!res.ok) { qgo.textContent = "Add"; toast("Could not add"); return; }
        qgo.textContent = "✓ Added"; qgo.classList.add("did");
        qin.value = ""; autogrow(qin);                 // shrink back to one row
        if (qatt) setAttach(qatt, []);
        var d = res.data || {};
        // A new reminder slots straight into the strip — watching it land there IS the
        // confirmation, so it skips the bottom toast. Hoisted above the branch because a
        // reminder now arrives on BOTH paths: parsed deterministically ("remind me in 10
        // minutes to call mum" — instant, no d.ai) or resolved by the router.
        var remSpliced = false;
        if (d.reminder && window.LifeOS && window.LifeOS.remAdd) {
          window.LifeOS.remAdd(d.reminder); remSpliced = true;
        }
        if (d.ai) {                                       // AI router: its reply IS the confirmation
          if (!remSpliced) toast(d.reply || "Done");
          if (d.week_html) insertCapture(d);              // a new task splices in place — no reload
          // Tasks the router CHANGED ("mark X done", "push X to friday", "drop X") come back
          // as re-rendered cards — swap each in place. Empty html means the row is gone from
          // this surface (deleted / no longer on Today), so drop the node instead.
          (d.cards || []).forEach(function (c) {
            if (c.html) { swapCard(c.id, c.html); return; }
            document.querySelectorAll('[data-task-id="' + c.id + '"][data-title]')
              .forEach(function (el) { removeNode(el); });
          });
          setTimeout(function () {
            qgo.textContent = "Add"; qgo.classList.remove("did");
            if (d.reload) reloadSoon();                   // changed an existing item we can't patch yet
          }, 900);
          return;
        }
        if (!remSpliced) toast("Added → " + (d.label || "filed"));
        var handled = remSpliced || insertCapture(d);
        setTimeout(function () {
          qgo.textContent = "Add"; qgo.classList.remove("did");
          if (!handled) reloadSoon();
        }, 900);
      }).catch(function () { busy = false; setSending(false); qgo.classList.remove("spin"); qgo.textContent = "Add"; toast("Could not add"); });
    }
    if (qgo) qgo.addEventListener("click", add);
    // Enter submits; Shift+Enter (and mobile return) inserts a newline — the composer is a
    // textarea, so multi-line captures (a note with line breaks) work like Todoist/Notion.
    qin.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); add(); }
    });
  })();

  // ---- first-run welcome: step 1 captures the name (required), step 2 hands off to Settings ----
  (function () {
    var el = document.getElementById("onboard");
    if (!el) return;
    document.body.appendChild(el);                            // escape .main's transform, then reveal
    el.classList.add("on");
    var input = document.getElementById("onboard-name");
    var save = document.getElementById("onboard-save");
    var s1 = document.getElementById("welstep1");
    var s2 = document.getElementById("welstep2");
    var hi = document.getElementById("welhi");
    var back = document.getElementById("welback");
    var dots = [].slice.call(el.querySelectorAll(".weldot"));

    function setStep(n) {                                      // toggle the visible step + progress dots
      if (s1) s1.hidden = n !== 1;
      if (s2) s2.hidden = n !== 2;
      if (back) back.hidden = n === 1;                        // back arrow only on step 2
      dots.forEach(function (d, i) { d.classList.toggle("on", i === n - 1); });
    }
    function greetInPlace(name) {                             // slip the name into the greeting eyebrow live
      var greet = document.querySelector(".greet-eb");
      if (!greet || greet.textContent.indexOf(",") >= 0) return;
      greet.textContent = greet.textContent.trim() + ", " + name;
    }
    function close() { post("/onboarding/dismiss", {}); el.remove(); }
    function doSave() {
      var name = (input.value || "").trim();
      if (!name) { input.focus(); return; }
      post("/onboarding/name", { name: name }).then(function (res) {
        if (!res.ok) { toast("Could not save"); return; }
        greetInPlace(name);
        if (hi) hi.textContent = "You’re all set, " + name + ".";
        setStep(2);                                           // advance to the reassurance step
      });
    }
    if (save) save.addEventListener("click", doSave);
    if (input) {
      var sync = function () { if (save) save.disabled = !input.value.trim(); };  // Get started enables only when named
      input.addEventListener("input", sync);
      input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); doSave(); } });
      sync();
      setTimeout(function () { input.focus(); }, 60);
    }
    if (back) back.addEventListener("click", function () {    // return to the name step (input keeps its value)
      setStep(1);
      if (input) { input.focus(); if (save) save.disabled = !input.value.trim(); }
    });
    var fin = document.getElementById("welfinish");
    if (fin) fin.addEventListener("click", close);
  })();

  // ---- Today calendar: view-only day/week/month, lazy-loaded (Google events) ---
  (function () {
    // Two surfaces share this renderer: the /calendar page (the full grid, host #calcard)
    // and Today's 'Up next' agenda (host #agbody). Either may be present; bail if neither.
    var card = document.getElementById("calcard");
    var agBody = document.getElementById("agbody");
    if (!card && !agBody) return;
    var body = document.getElementById("calbody");
    var label = document.getElementById("callabel");
    var views = document.getElementById("calviews");
    var nav = card ? card.querySelector(".calnav") : null;

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
        evs.slice(0, 4).forEach(function (ev) {
          // Time prefix shows on desktop; phone hides .cmt (CSS) so the title gets the
          // full narrow cell — GCal's mobile month is title-only for the same reason.
          html += '<div class="cmev' + (ev.all_day ? " allday" : "") + '" title="' + esc(ev.summary) + '">'
            + (ev.all_day ? "" : '<span class="cmt mono">' + shortTime(timeOf(ev)) + '</span> ')
            + '<span class="cmname">' + esc(ev.summary) + '</span></div>';
        });
        if (evs.length > 4) html += '<div class="cmmore">+' + (evs.length - 4) + ' more</div>';
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

    // Grid nav/view controls live only on the /calendar page (host #calcard).
    if (views) views.addEventListener("click", function (e) {
      var b = e.target.closest(".calview"); if (b) setView(b.dataset.view);
    });
    if (nav) nav.addEventListener("click", function (e) {
      var b = e.target.closest("[data-cal]"); if (!b) return;
      var act = b.dataset.cal, dir = act === "next" ? 1 : -1;
      if (act === "today") anchor = startOfDay(new Date());
      else if (view === "day") anchor = addDays(anchor, dir);
      else if (view === "week") anchor = addDays(anchor, 7 * dir);
      else anchor = addMonths(anchor, dir);
      render();
    });

    // Initial paint: Today shows the compact agenda; /calendar IS the full grid.
    if (agBody) loadAgenda();
    if (card) render();

    // Auto-refresh when the tab regains focus — a calendar left open otherwise goes
    // stale (events added elsewhere never appear). Throttled so flicking between tabs
    // doesn't hammer Google; silent so there's no Loading flicker on return.
    var lastRefresh = Date.now();
    function maybeRefresh() {
      if (document.hidden || Date.now() - lastRefresh < 30000) return;
      lastRefresh = Date.now();
      cache = {};                          // drop the session cache → re-pull fresh
      if (agBody) loadAgenda(true);
      if (card) render(true);
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
  // These jobs are fire-and-forget subprocesses that stamp their heartbeat only when they
  // FINISH — a fixed timer can't know when that is. So after triggering we hold the button
  // in a "running…" state and poll /settings until THIS row's timestamp advances past the
  // pre-click value (the only honest "done" signal), refreshing every row's dot+stamp in
  // place as we go, then flash the button green. No fake "just now", no full-page reload.
  var POLL_EVERY = 1500, POLL_MAX = 10;                 // ~15s ceiling, then give up quietly
  // Heartbeat stamps read as relative recency ("just now" / "3 min ago" / "2 hr ago") — the
  // question this card answers — falling back to the absolute date past ~18h, where a relative
  // count stops meaning anything. The raw ISO lives in data-ago, the exact time in the title
  // (server-rendered absolute is the no-JS fallback); a 60s ticker keeps relatives from silently
  // rotting while the tab sits open. Skips empty (never-ran) and the schedule "next …" mono.
  function relAgo(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return null;
    var s = (Date.now() - t) / 1000;
    if (s < 45) return "just now";
    if (s < 90) return "1 min ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 5400) return "1 hr ago";
    if (s < 64800) return Math.round(s / 3600) + " hr ago";
    return null;                                          // >~18h → caller keeps the absolute
  }
  function paintAgo(root) {
    (root || document).querySelectorAll(".mono.ago[data-ago]").forEach(function (el) {
      var iso = el.getAttribute("data-ago");
      if (!iso) return;                                   // never ran → leave "never"
      var rel = relAgo(iso);
      el.textContent = rel || (el.getAttribute("title") || el.textContent);
    });
  }
  paintAgo();
  setInterval(function () { paintAgo(); }, 60000);
  // Change-detection reads the raw ISO (data-ago), NOT the rendered text — second-resolution,
  // so a re-run in the same minute still registers, and relative-vs-absolute never confuses it.
  function healthStamp(key, root) {
    var el = (root || document).querySelector('.setrow[data-health="' + key + '"] .sdesc .mono.ago[data-ago]');
    return el ? (el.getAttribute("data-ago") || "") : "";
  }
  function applyHealthRows(doc) {                        // swap dot class + desc from a fresh doc
    document.querySelectorAll(".setrow[data-health]").forEach(function (live) {
      var fresh = doc.querySelector('.setrow[data-health="' + live.dataset.health + '"]');
      if (!fresh) return;
      var fd = fresh.querySelector(".slabel .dot"), ld = live.querySelector(".slabel .dot");
      if (fd && ld) ld.className = fd.className;
      var fx = fresh.querySelector(".sdesc"), lx = live.querySelector(".sdesc");
      if (fx && lx) lx.innerHTML = fx.innerHTML;
    });
    paintAgo();                                           // re-render the freshly-swapped stamps
  }
  function pollHealth(btn, key, label, before, tries) {
    fetch("/settings").then(function (r) { return r.text(); }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      applyHealthRows(doc);
      if (healthStamp(key, doc) !== before) {           // heartbeat moved → the job ran
        btn.textContent = "Done ✓"; btn.classList.add("ok");
        setTimeout(function () {
          btn.classList.remove("ok"); btn.disabled = false; btn.textContent = label;
        }, 1600);
      } else if (tries + 1 >= POLL_MAX) {                // slow/same-minute: leave row current
        btn.disabled = false; btn.textContent = label;
      } else {
        setTimeout(function () { pollHealth(btn, key, label, before, tries + 1); }, POLL_EVERY);
      }
    });
  }
  document.querySelectorAll('.runbtn[data-run]:not([data-run="claude"])').forEach(function (b) {
    b.addEventListener("click", function () {
      var key = b.dataset.run, label = b.textContent, before = healthStamp(key);
      b.disabled = true;
      b.textContent = /restart/i.test(label) ? "Restarting…" : "Running…";  // verb matches the action
      post("/settings/run/" + key).then(function (res) {
        if (res.ok && res.data && res.data.status === "ok") {
          pollHealth(b, key, label, before, 0);         // button state carries success; no toast
        } else {
          b.disabled = false; b.textContent = label;
          toast((res.data && res.data.message) || "Could not run");
        }
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
  function swapSettingsCard(form) {
    fetch("/settings", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html").getElementById(form.id);
        if (!fresh) { window.location.reload(); return; }
        form.replaceWith(fresh);
        bindConnCard(fresh);
        bindRootsTest(fresh);            // else Test goes dead after a save swaps the card
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
            if (r2.ok) { toast("Connected ✓"); swapSettingsCard(form); } else { btn.textContent = lbl; btn.disabled = false; toast("Couldn't save the token"); }
          });
        });
      } else {                                           // google / dropbox / telegram: save creds
        btn.textContent = "…"; btn.disabled = true;
        post(form.getAttribute("action"), new FormData(form)).then(function (res) {
          if (res.ok && (!res.data || res.data.status !== "error")) { toast("Saved ✓"); swapSettingsCard(form); }
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
          if (!ok) setTimeout(function () { swapSettingsCard(form); }, 1200);   // show the red failing note
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
        p.then(function () { toast("Disconnected"); swapSettingsCard(form); });
      }, "Confirm disconnect?");
    });
  }
  ["aiform", "googleform", "dropboxform", "telegramform", "docrootsform"].forEach(function (id) { bindConnCard(document.getElementById(id)); });
  document.querySelectorAll(".cmdcopy[data-copy]").forEach(bindCopy);

  // Documents card: its Test probes the PASTED paths, so it posts the form body — unlike
  // the provider [data-test] buttons, which ping an already-SAVED connection with no body.
  // Testing before saving is the point: you find out a folder isn't mounted (Cloud Sync not
  // set up yet) without first committing a broken path. The per-folder report is the answer,
  // so it lands inline; the toast only carries the headline.
  function bindRootsTest(form) {
    if (!form) return;
    var b = form.querySelector("[data-test-roots]"); if (!b) return;
    var out = form.querySelector("[data-roots-detail]");
    var orig = b.textContent;
    b.addEventListener("click", function () {
      b.disabled = true; b.textContent = "…";
      post("/settings/test-doc-roots", new FormData(form)).then(function (res) {
        var ok = res.ok && res.data && res.data.status === "ok";
        b.disabled = false; b.textContent = orig;
        toast((res.data && res.data.message) || "Couldn't check those folders");
        if (!out) return;
        var detail = (res.data && res.data.detail) || "";
        out.textContent = detail;
        out.hidden = !detail;
        out.classList.toggle("err", !ok);
      });
    });
  }
  bindRootsTest(document.getElementById("docrootsform"));

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
          var card = document.getElementById(id); if (card) swapSettingsCard(card);
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

  // ---- goals: wire ONE card's actions (number edit, achieve, delete) -----------
  // Bound at page load AND on every card spliced in without a reload (create / Undo),
  // so a live-added card behaves exactly like a page-load one.
  function wireGoal(card) {
    // manual number inline edit (styled input in place — never the native prompt,
    // per the no-browser-default-controls rule)
    var el = card.querySelector(".gnum.edit");
    if (el) el.addEventListener("click", function () {
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
          var bar = card.querySelector(".bar");
          if (bar) {                                          // fill sweeps in place
            var pct = target ? Math.min(100, n / target * 100) : 0;
            bar.classList.toggle("full", pct >= 100);
            card.classList.toggle("achieved", pct >= 100);
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

    // mark achieved — toggles in place (a completion beat, not a reload)
    var ach = card.querySelector(".gachieve");
    if (ach) ach.addEventListener("click", function () {
      post("/goals/" + ach.dataset.goalId + "/achieve", {}).then(function (res) {
        if (!res.ok) { toast("Could not update"); return; }
        var on = res.data && res.data.achieved;
        ach.classList.toggle("done", !!on);
        ach.textContent = on ? "✓ Achieved" : "Mark achieved";
        ach.setAttribute("aria-pressed", on ? "true" : "false");
        card.classList.toggle("achieved", !!on);       // the card recedes too, not just the button
      });
    });

    // delete — arm-then-confirm + Undo toast (parity with tasks/notes)
    var del = card.querySelector(".gdel");
    if (del) confirmClick(del, function () {
      var id = del.dataset.goalId;
      // remember the slot BEFORE the node goes, so Undo can put the card back exactly
      // where it was rather than reloading the page
      var parent = card.parentNode, anchor = card.nextElementSibling;
      post("/goals/" + id + "/delete").then(function (res) {
        if (!res.ok) { toast("Could not delete"); return; }
        removeWithUndo(card, {
          label: "Goal", restore: "/goals/" + id + "/restore",
          onRestore: function (r) {
            var fresh = htmlToNode(r.data && r.data.card_html);
            if (!fresh || !parent.isConnected) return false;   // grid gone → let it reload
            parent.insertBefore(fresh, anchor && anchor.isConnected ? anchor : null);
            wireGoal(fresh);
            return true;
          }
        });
      });
    });
  }
  document.querySelectorAll(".goal").forEach(wireGoal);

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
    // Submit in place: the new card splices into its timeframe's grid instead of the
    // form's native POST bouncing the whole page through /goals.
    goalForm.addEventListener("submit", function (e) {
      e.preventDefault();
      post("/goals/new", new FormData(goalForm)).then(function (res) {
        if (!res.ok) { toast((res.data && res.data.message) || "Could not create goal"); return; }
        var d = res.data || {};
        var grid = document.querySelector('.goalgrid[data-section="' + d.section + '"]');
        var card = d.card_html ? htmlToNode(d.card_html) : null;
        // Reload only when there's no slot to splice into: the first goal of a timeframe
        // has no section yet, and By-date grids are ordered by target date (not created),
        // so a fresh card there can't just be appended. (No toast — the reload wipes it,
        // and the card landing in its section is the confirmation.)
        if (!grid || !card || d.section === "by_date") { reloadSoon(); return; }
        grid.appendChild(card);
        wireGoal(card);
        goalForm.reset();
        // reset() snaps the hidden timeframe back to "week" — put the chips + date field
        // in sync with it, and re-collapse the measure box.
        goalForm.querySelectorAll("#gtimeframe .qt").forEach(function (c) {
          c.classList.toggle("active", c.dataset.tf === "week");
        });
        if (dateField) dateField.classList.add("hide");
        if (mBox && mToggle) { mBox.classList.add("hide"); mToggle.classList.remove("hide"); }
        closeGoalForm();
        toast("Goal created");
      });
    });
  }

  // ---- journal: per-entry wiring (galleries, ⋯, edit, delete) ------------------
  // One function for a page-load entry AND a freshly spliced one, so an entry added
  // without a reload gets the same actions.

  // Same-minute entries are disambiguated by occurrence idx, so removing (or restoring)
  // one shifts every later sibling's index in the FILE — mirror that on the page or the
  // next edit/delete targets a stale idx (404). delta -1 on delete, +1 on undo.
  function shiftSiblings(entry, delta) {
    var day = entry.dataset.day, time = entry.dataset.time;
    var idx = parseInt(entry.dataset.idx, 10);
    document.querySelectorAll(".jentry").forEach(function (e) {
      if (e === entry || e.dataset.day !== day || e.dataset.time !== time) return;
      var ei = parseInt(e.dataset.idx, 10);
      if (delta < 0 ? ei > idx : ei >= idx) e.dataset.idx = ei + delta;
    });
  }

  function wireEntry(entry) {
    // galleries → smart media modal on tap (images inline, files carded)
    entry.querySelectorAll(".jgimg, .jgfile").forEach(function (im) {
      im.addEventListener("click", function () { openMedia(im.dataset.full || im.src, im.dataset.name); });
    });
    // ⋯ reveals row actions on touch (desktop reveals on hover via CSS)
    var more = entry.querySelector(".jmore");
    if (more) more.addEventListener("click", function () { entry.classList.toggle("acts"); });

    // delete — two-step arm-then-confirm (like tasks/notes) + Undo toast as the net.
    // The server restores the whole day from prev_raw; the page only lost this one
    // node, so Undo slots the same node back rather than reloading.
    var del = entry.querySelector(".jdel");
    if (del) confirmClick(del, function () {
      var day = entry.dataset.day, time = entry.dataset.time, idx = entry.dataset.idx;
      var parent = entry.parentNode, anchor = entry.nextElementSibling;
      post("/journal/" + day + "/entry/" + time + "/delete", { idx: idx }).then(function (res) {
        if (!res.ok) { toast("Could not delete"); return; }
        var prev = res.data.prev_raw;
        shiftSiblings(entry, -1);
        removeWithUndo(entry, {
          label: "Entry", restore: "/journal/" + day + "/save", restoreData: { raw: prev },
          onRestore: function () {
            if (!parent.isConnected) return false;
            shiftSiblings(entry, +1);
            entry.classList.remove("removing");
            parent.insertBefore(entry, anchor && anchor.isConnected ? anchor : null);
            return true;
          }
        });
      });
    });

    var edit = entry.querySelector(".jedit");
    if (edit) edit.addEventListener("click", function () {
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
        var prevText = p.textContent;
        post("/journal/" + day + "/entry/" + time + "/save", { idx: idx, text: area.value })
          .then(function (res) {
            if (!res.ok) { toast("Could not save"); return; }
            var prev = res.data.prev_raw;
            p.textContent = area.value.trim();
            teardown();
            toast("Entry updated", function () {
              // prev_raw rewrites the whole day's file, but only this one paragraph
              // changed on the page — put its text back instead of reloading.
              post("/journal/" + day + "/save", { raw: prev }).then(function () {
                p.textContent = prevText;
              });
            });
          });
      });
    });
  }
  document.querySelectorAll(".jentry").forEach(wireEntry);

  // ---- journal: add entry -----------------------------------------------------
  // The first entry of a day fills today's dot in the month cadence graph — the old
  // reload was the only thing that used to light it.
  function markCadenceToday() {
    var dot = document.querySelector(".ghgraph .cd.today");
    if (!dot || dot.classList.contains("w")) return;
    dot.classList.add("w");
    if (dot.dataset.tip) dot.dataset.tip = dot.dataset.tip.replace("no entry", "wrote");
    var count = document.querySelector(".cadcount");
    var n = count ? parseInt(count.textContent, 10) : NaN;
    if (!isNaN(n)) count.textContent = (n + 1) + " days";
  }
  // Splice one freshly-rendered entry above the composer. False → no slot to splice into
  // (or a raw-markdown-only page, which a load re-renders as entries) → caller reloads.
  function addEntry(node, box) {
    var card = box.parentNode;
    if (!node || !card) return false;
    var empty = card.querySelector(".empty");
    if (!card.querySelector(".jentry") && !empty) return false;
    if (empty) empty.remove();
    card.insertBefore(node, box);
    wireEntry(node);
    markCadenceToday();
    return true;
  }
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
      .then(function (res) {
        jBusy = false;
        if (!res.ok) { toast("Could not add"); return; }
        box.value = ""; autogrow(box);
        if (jAttach) setAttach(jAttach, []);
        toast("Entry added");
        var html = res.data && res.data.entry_html;
        if (!addEntry(html ? htmlToNode(html) : null, box)) reloadSoon();
      })
      .catch(function () { jBusy = false; toast("Could not add"); });
  });

  // ---- reminders card: pure display of pending pushes + dismiss ----------------
  // Adding happens in the top composer (Reminder mode); it calls window.LifeOS.remAdd
  // to splice a new one in here without a reload — same live-update feel as a capture.
  (function () {
    var card = document.getElementById("remcard");
    if (!card) return;
    var list = document.getElementById("remlist");
    var empty = card.querySelector(".remempty");

    function syncEmpty() { if (empty) empty.hidden = !!list.querySelector(".remitem"); }
    function makeItem(r) {
      var el = document.createElement("div");
      el.className = "remitem";
      el.dataset.id = r.id; el.dataset.text = r.text; el.dataset.fire = r.fire_at;
      var rt = document.createElement("span"); rt.className = "rt"; rt.textContent = r.text;
      var wt = document.createElement("time"); wt.className = "rwhen"; wt.textContent = r.label;
      var x = document.createElement("button");
      x.className = "remx"; x.type = "button"; x.textContent = "✕";
      x.setAttribute("aria-label", "Cancel reminder");
      el.appendChild(rt); el.appendChild(wt); el.appendChild(x);
      return el;
    }
    function insertSorted(el) {
      // List is soonest-first; fire_at is UTC ISO, so lexical compare = chronological.
      var fire = el.dataset.fire, before = null;
      list.querySelectorAll(".remitem").forEach(function (it) {
        if (!before && it.dataset.fire > fire) before = it;
      });
      list.insertBefore(el, before);
    }
    // Exposed for the composer's Reminder mode. Adding is a real user gesture, so it's
    // the right moment to ask for notification permission (browsers may auto-deny on load).
    window.LifeOS = window.LifeOS || {};
    window.LifeOS.remAdd = function (r) { askNotify(); insertSorted(makeItem(r)); syncEmpty(); };

    // ---- browser-side firing: no Telegram, so the open tab delivers due reminders ------
    // A reminder's data-fire is UTC ISO; once now passes it we notify and drop it off the
    // strip, POSTing /fire to stamp fired_at (mirrors scheduler.maybe_fire_reminders). Uses
    // the Notifications API when granted (works backgrounded), else an in-page toast.
    function askNotify() {
      if (!("Notification" in window) || Notification.permission !== "default") return;
      try { Notification.requestPermission(); } catch (e) {}
    }
    // Background surface: a PINNED OS notification (requireInteraction ⇒ it won't fade like a
    // toast) for when the tab isn't focused. The in-page surface is the modal below.
    function osNotify(text) {
      if ("Notification" in window && Notification.permission === "granted") {
        try { new Notification("⏰ Reminder", { body: text, requireInteraction: true }); } catch (e) {}
      }
    }
    // Alarm chime: a two-note ding repeating until dismissed. Synthesised with WebAudio so
    // there's no asset to ship (no build step). Best-effort — if the browser blocks audio
    // (no prior gesture on this tab) it stays silent and the modal still does its job.
    var actx = null, chimeTimer = null, chimeCount = 0;
    // Tuned by ear (2026-07-15): full volume, 2s apart, 790→1160Hz — carries across a room
    // without being shrill. CHIME_MAX × CHIME_EVERY ⇒ the ~60s self-silence.
    var CHIME_VOL = 1, CHIME_EVERY = 2000, CHIME_MAX = 30;
    function blip(at, freq) {
      var o = actx.createOscillator(), g = actx.createGain();
      o.connect(g); g.connect(actx.destination);
      o.type = "sine"; o.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, at);            // ramp, never a click
      g.gain.exponentialRampToValueAtTime(CHIME_VOL, at + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, at + 0.3);
      o.start(at); o.stop(at + 0.32);
    }
    function chime() {
      try {
        if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
        if (actx.state === "suspended") actx.resume();
        var t = actx.currentTime;
        blip(t, 790); blip(t + 0.22, 1160);         // ding-dong
      } catch (e) { stopChime(); }
    }
    function startChime() {
      stopChime(); chimeCount = 0; chime();
      chimeTimer = setInterval(function () {
        if (++chimeCount >= CHIME_MAX) { stopChime(); return; }
        chime();
      }, CHIME_EVERY);
    }
    function stopChime() { if (chimeTimer) { clearInterval(chimeTimer); chimeTimer = null; } }
    // Browsers refuse audio until the page has had a user gesture — an untouched dashboard
    // would fire a SILENT alarm. Prime the context on the first interaction (any click/key)
    // so a chime hours later can actually sound.
    function primeAudio() {
      try {
        if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
        if (actx.state === "suspended") actx.resume();
      } catch (e) {}
    }
    ["click", "keydown", "touchstart"].forEach(function (ev) {
      document.addEventListener(ev, primeAudio, { once: true });
    });

    // In-page alarm: a centred modal that stacks each due reminder and stays until dismissed.
    var alertOv = document.getElementById("remalert"),
        alertList = document.getElementById("ra-list"),
        okBtn = document.getElementById("ra-ok");
    function popAlert(text, when) {
      if (!alertOv || !alertList) { toast("⏰ " + text); return; }   // safety net
      var item = document.createElement("div"); item.className = "ra-item";
      var tx = document.createElement("div"); tx.className = "ra-text"; tx.textContent = text;
      item.appendChild(tx);
      if (when) { var w = document.createElement("div"); w.className = "ra-when"; w.textContent = when; item.appendChild(w); }
      alertList.appendChild(item);
      alertOv.classList.add("on");
      startChime();
    }
    function closeAlert() { stopChime(); if (alertOv) { alertOv.classList.remove("on"); alertList.innerHTML = ""; } }
    if (okBtn) okBtn.addEventListener("click", closeAlert);
    if (alertOv) alertOv.addEventListener("click", function (e) { if (e.target === alertOv) closeAlert(); });

    function fireDue() {
      var now = Date.now();
      list.querySelectorAll(".remitem").forEach(function (it) {
        if (it.dataset.firing) return;                 // a POST is already in flight for this one
        var t = Date.parse(it.dataset.fire);
        if (isNaN(t) || t > now) return;
        it.dataset.firing = "1";
        var text = it.dataset.text, when = (it.querySelector(".rwhen") || {}).textContent || "";
        // Always alarm — the POST's `fired` only says who stamped fired_at FIRST, not whether
        // THIS dashboard has shown it. The daemon polls faster than we do, so gating on it
        // meant a Telegram push silently swallowed the on-screen alarm. Telegram and the
        // dashboard are separate surfaces; a row is only here if it was pending at load, and
        // it's removed once shown, so this can't double-pop or resurrect an old reminder.
        post("/reminders/" + it.dataset.id + "/fire").then(function () {
          popAlert(text, when); osNotify(text);
          it.remove(); syncEmpty();
        }).catch(function () { delete it.dataset.firing; });       // let a later tick retry
      });
    }
    fireDue();                       // catch anything already overdue at load
    setInterval(fireDue, 20000);     // then every 20s — within a poll of the stated time

    list.addEventListener("click", function (e) {
      var x = e.target.closest(".remx");
      if (!x) return;
      var item = x.closest(".remitem");
      post("/reminders/" + item.dataset.id + "/dismiss").then(function (res) {
        if (!res.ok) { toast("Could not cancel"); return; }
        removeWithUndo(item, {
          msg: "Reminder cancelled",
          restore: "/reminders/restore",
          restoreData: { text: res.data.text, fire_at: res.data.fire_at },
          after: syncEmpty,
        });
      });
    });
  })();
})();
