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
    function filterNotes() {
      var q = (search ? search.value.trim() : "").toLowerCase();
      // Split the query into words and require EACH to appear in the haystack, so
      // multi-word searches ("retirement planning", "moomoo promo") match a note that
      // contains both words in any order — a single whole-phrase substring wouldn't.
      var terms = q ? q.split(/\s+/) : [];
      document.querySelectorAll(".note").forEach(function (n) {
        var spaces = (n.dataset.spaces || "").split(" ");
        var spaceOk = activeSpace === "all" || spaces.indexOf(activeSpace) !== -1;
        // Match the rendered card (title + snippet) OR the hidden haystack (tags +
        // body excerpt), so a note that's about "trading" — or merely tagged
        // options-trading — matches even though neither shows on the card.
        var hay = n.textContent.toLowerCase() + " " + (n.dataset.search || "");
        var qOk = terms.every(function (t) { return hay.indexOf(t) !== -1; });
        n.classList.toggle("hide", !(spaceOk && qOk));
      });
    }
    document.querySelectorAll(".sp[data-space]").forEach(function (b) {
      b.addEventListener("click", function () {
        activeSpace = b.dataset.space;
        document.querySelectorAll(".sp[data-space]").forEach(function (x) { x.classList.toggle("active", x === b); });
        filterNotes();
      });
    });
    if (search) search.addEventListener("input", filterNotes);
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
    var elAttach = document.getElementById("ed-attach");
    if (elAttach) initAttach(elAttach);
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
        if (elAttach) setAttach(elAttach, (n.media || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean));
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
      if (elAttach) setAttach(elAttach, []);
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
          title: elTitle.value, tags: elTags.value, body: elBody.value,
          media: elAttach ? getAttach(elAttach).join(",") : ""
        }).then(function () { flashSaved(); patchNoteCard(); });
      }
      // no note yet: only create once there is real content (guard against a
      // double-click creating two notes before the POST resolves).
      if (creating) return Promise.resolve();
      if (!elTitle.value.trim() && !elBody.value.trim()) return Promise.resolve();
      creating = true; elSaved.textContent = "saving…";
      return post("/notes/new", {
        title: elTitle.value, tags: elTags.value, body: elBody.value,
        media: elAttach ? getAttach(elAttach).join(",") : ""
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
