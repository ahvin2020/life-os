/* Life OS — notes page: tag/space filter, live search, library Ask, note editor.
   Loads AFTER core.js; calls its globals (post, toast, confirmClick,
   removeWithUndo) by bare name. Every block is null-guarded so it no-ops on
   pages without notes / the note-editor overlay. */
(function () {
  "use strict";

  // ---- notes: tag filter + live search ---------------------------------------
  (function () {
    var search = document.getElementById("nsearch");
    if (!search && !document.querySelector(".sp[data-space]")) return;
    var activeSpace = "all";
    var updateAskHint = function () {};   // wired up by the Ask block below
    function filterNotes() {
      var raw = search ? search.value.trim() : "";
      var q = raw.toLowerCase();
      var vis = 0;
      document.querySelectorAll(".note").forEach(function (n) {
        var spaces = (n.dataset.spaces || "").split(" ");
        var spaceOk = activeSpace === "all" || spaces.indexOf(activeSpace) !== -1;
        // Match the rendered card (title + snippet) OR the hidden haystack (tags +
        // body excerpt), so a note that's about "trading" — or merely tagged
        // options-trading — matches even though neither shows on the card.
        var hay = n.textContent.toLowerCase() + " " + (n.dataset.search || "");
        var qOk = !q || hay.indexOf(q) !== -1;
        var show = spaceOk && qOk;
        n.classList.toggle("hide", !show);
        if (show) vis++;
      });
      updateAskHint(raw, vis);
    }
    document.querySelectorAll(".sp[data-space]").forEach(function (b) {
      b.addEventListener("click", function () {
        activeSpace = b.dataset.space;
        document.querySelectorAll(".sp[data-space]").forEach(function (x) { x.classList.toggle("active", x === b); });
        filterNotes();
      });
    });
    if (search) search.addEventListener("input", filterNotes);

    // ---- Ask: semantic library question (reuses the bot's library engine) ------
    // Typing still live-filters (input handler above); Ask is the DELIBERATE action —
    // a clicked button or Enter — because the answer costs a 5-15s Claude call and must
    // not fire on every keystroke. Enter (not IME-composition) commits the query to Ask;
    // live-filter already happened as you typed, so plain Enter had nothing else to do.
    var askBtn = document.getElementById("note-ask");
    var panel = document.getElementById("ask-panel");
    var grid = document.getElementById("notes-grid");
    if (askBtn && panel && grid && search) {
      function askHead(caption, thinking) {
        var head = document.createElement("div"); head.className = "askhead";
        var lbl = document.createElement("span");
        lbl.className = "askfor" + (thinking ? " asking" : "");
        lbl.textContent = caption;
        head.appendChild(lbl);
        if (!thinking) {
          var clr = document.createElement("button");
          clr.className = "mini"; clr.type = "button"; clr.textContent = "Clear";
          clr.addEventListener("click", clearAsk);
          head.appendChild(clr);
        }
        return head;
      }
      function clearAsk() {
        panel.classList.add("hide"); panel.innerHTML = "";
        grid.classList.remove("hide");
        filterNotes();   // answers gone → re-show the live hint if text remains
      }

      // ---- live hint (#2/#3): teach the two paths as you type, nudge Ask for
      // question-shaped input, and on zero literal matches offer Ask as the exit.
      var hint = document.getElementById("ask-hint");
      var QSHAPE = /\?\s*$|^(what|how|why|who|whom|whose|where|when|which|find|should|is|are|was|were|can|could|would|do|does|did|any)\b/i;
      updateAskHint = function (raw, vis) {
        if (!hint) return;
        // No hint when nothing typed, or when Ask answers already fill the panel.
        if (!raw || !panel.classList.contains("hide")) {
          hint.classList.add("hide"); hint.innerHTML = "";
          askBtn.classList.remove("nudge");
          return;
        }
        askBtn.classList.toggle("nudge", QSHAPE.test(raw));
        hint.innerHTML = "";
        var ask = document.createElement("button");
        ask.type = "button"; ask.className = "hintask";
        ask.textContent = "✦ Ask your library";
        ask.addEventListener("click", askLibrary);
        var lead = document.createElement("span"); lead.className = "hintlead";
        if (vis === 0) {
          lead.textContent = "No note contains that — ";
          hint.appendChild(lead); hint.appendChild(ask);
          var tail = document.createElement("span");
          tail.className = "hintlead"; tail.textContent = " instead?";
          hint.appendChild(tail);
        } else {
          var num = document.createElement("span");
          num.className = "hintnum"; num.textContent = String(vis);
          lead.appendChild(num);
          lead.appendChild(document.createTextNode(" match" + (vis === 1 ? "" : "es") + " · "));
          hint.appendChild(lead); hint.appendChild(ask);
        }
        hint.classList.remove("hide");
      };

      function askLibrary() {
        var q = search.value.trim();
        if (!q) { search.focus(); return; }
        // Entering Ask mode: drop the hint + nudge, the panel takes over.
        if (hint) { hint.classList.add("hide"); hint.innerHTML = ""; }
        askBtn.classList.remove("nudge");
        // Immediate thinking state (Doherty: instant feedback before the slow call).
        panel.innerHTML = "";
        panel.appendChild(askHead("Reading your library for “" + q + "”…", true));
        panel.classList.remove("hide"); grid.classList.add("hide");
        post("/notes/ask", { q: q }).then(function (res) {
          var data = (res && res.data) || {};
          panel.innerHTML = "";
          if (!data.count) {
            panel.appendChild(askHead("No matches for “" + q + "”", false));
            var e = document.createElement("div"); e.className = "empty";
            e.textContent = "Nothing in your saved library answers that yet — try a broader topic, or Clear to browse everything.";
            panel.appendChild(e);
            return;
          }
          var cap = data.fallback
            ? "recent saves for “" + q + "”"
            : "showing answers for “" + q + "”";
          panel.appendChild(askHead(cap, false));
          // Server-rendered with the SAME note_card macro as the grid → identical panels.
          var g = document.createElement("div");
          g.innerHTML = data.html;
          var grid2 = g.firstElementChild;   // the .ngrid
          if (grid2) {
            grid2.querySelectorAll(".note[data-slug]").forEach(function (n) {
              n.addEventListener("click", function (ev) {
                if (ev.target.closest("a")) return;   // domain/source link navigates itself
                if (window.openNote) window.openNote(n.dataset.slug);
              });
            });
            panel.appendChild(grid2);
          }
        });
      }
      askBtn.addEventListener("click", askLibrary);
      search.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); askLibrary(); }
      });
      document.addEventListener("keydown", function (e) {
        // Esc clears the answers — but never steal Esc from the note editor overlay.
        var ov = document.getElementById("noteoverlay");
        if (e.key === "Escape" && !panel.classList.contains("hide") &&
            !(ov && ov.classList.contains("on"))) clearAsk();
      });
    }
  })();

  // ---- note editor modal ------------------------------------------------------
  (function () {
    var ov = document.getElementById("noteoverlay");
    if (!ov) return;
    var elTitle = document.getElementById("ed-title"), elTags = document.getElementById("ed-tags"),
        elBody = document.getElementById("ed-body"), elSaved = document.getElementById("ed-saved"),
        elAudio = document.getElementById("ed-audio"), elAudioRow = document.getElementById("ed-audio-row");
    // Show the original voice recording (if this note came from one) so you can hear
    // back exactly what you said — the transcription isn't always perfect.
    function setAudio(hasAudio, slug) {
      if (!elAudioRow) return;
      // The player reveals the row itself once the recording's metadata loads (and
      // keeps it hidden if the file 404s), so we only set the src here.
      if (hasAudio) { elAudioRow.hidden = true; elAudio.src = "/notes/" + slug + "/audio"; }
      else { elAudio.removeAttribute("src"); elAudioRow.hidden = true; }
    }
    var current = null, creating = false, savedTimer = null;
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
        elBody.value = n.body;
        elSaved.textContent = "";
        setAudio(!!n.audio, slug);
        var d = document.getElementById("ed-delete"); if (d._disarm) d._disarm();
        ov.classList.add("on"); elBody.focus();
      });
    }
    // Open a blank editor without persisting anything — the note file is only
    // created on the first save that carries a title or body (lazy creation, so
    // opening then closing an empty editor leaves no orphan "Untitled" note).
    function openBlank() {
      current = null; creating = false;
      elTitle.value = ""; elTags.value = ""; elBody.value = "";
      elSaved.textContent = "";
      setAudio(false);
      var d = document.getElementById("ed-delete"); if (d._disarm) d._disarm();
      ov.classList.add("on"); elTitle.focus();
    }
    window.openNote = open;
    // Reflect a save back onto the grid card in place, so "saved ✓" never lies
    // about what the page behind the editor shows.
    var BULK_TAGS = ["idea", "imported", "link", "ig", "saved", "video", "note"];
    function patchNoteCard() {
      var card = document.querySelector('.note[data-slug="' + current + '"]');
      if (!card) return;
      var title = card.querySelector(".ntitle");
      if (title) title.textContent = elTitle.value;
      var snip = card.querySelector(".nsnip");
      if (snip) snip.textContent = elBody.value.replace(/https?:\/\/\S+/g, " ").replace(/\s+/g, " ").trim().slice(0, 220);
      var tags = elTags.value.split(",").map(function (s) { return s.trim().replace(/^#/, ""); }).filter(Boolean);
      card.dataset.tags = tags.join(" ");
      // card shows the single topic tag (the bulk idea/imported/link/ig set is noise)
      var topic = tags.filter(function (t) { return BULK_TAGS.indexOf(t) === -1; });
      var tag = topic[0] || tags[0] || "";
      var tagEl = card.querySelector(".ntag");
      if (tagEl && tag) tagEl.textContent = tag;
      else if (tagEl) tagEl.remove();
    }
    // Explicit save only — nothing persists until the user clicks Save (no
    // typing/blur autosave). Returns a promise so the button can chain feedback.
    function save() {
      if (current) {
        elSaved.textContent = "saving…";
        return post("/notes/" + current + "/save", {
          title: elTitle.value, tags: elTags.value, body: elBody.value
        }).then(function () { flashSaved(); patchNoteCard(); });
      }
      // no note yet: only create once there is real content (guard against a
      // double-click creating two notes before the POST resolves).
      if (creating) return Promise.resolve();
      if (!elTitle.value.trim() && !elBody.value.trim()) return Promise.resolve();
      creating = true; elSaved.textContent = "saving…";
      return post("/notes/new", {
        title: elTitle.value, tags: elTags.value, body: elBody.value
      }).then(function (res) {
        creating = false;
        if (res.data && res.data.slug) {
          current = res.data.slug; flashSaved();
        } else { elSaved.textContent = ""; }
      });
    }
    document.getElementById("ed-save").addEventListener("click", save);
    confirmClick(document.getElementById("ed-delete"), function () {
      var slug = current;
      ov.classList.remove("on");
      if (!slug) { current = null; return; }   // blank unsaved note — nothing to delete
      post("/notes/" + slug + "/delete").then(function () {
        // Remove the card in place (quick fade via the motion system) instead of
        // reloading the whole page; only undo does a minimal reload to restore it.
        var card = document.querySelector('.note[data-slug="' + slug + '"]');
        removeWithUndo(card, { label: "Note", restore: "/notes/" + slug + "/restore" });
      });
      current = null;
    });
    function close() { ov.classList.remove("on"); current = null; }
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
})();
