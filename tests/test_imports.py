"""Tests for the import tooling (scripts/import_todo.py + scripts/import_sheet.py).

These never call `claude -p` (classification is injected) and never hit the
network (sheet mapping is fed fixture rows). conftest.py already points the DB +
vault at throwaway dirs, so apply-path tests write into disposable state.
"""

import os
import sys

# make scripts/ importable
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import import_common  # noqa: E402
import import_todo  # noqa: E402
import import_sheet  # noqa: E402
from core.db import connect, today_iso  # noqa: E402
from domain import vault_store  # noqa: E402


def _db():
    return connect(os.environ["LIFEOS_DB_PATH"])


# ── 1. todo.txt PARSING ────────────────────────────────────────────────────────
def test_parse_day_header_is_not_an_item():
    items = import_todo.parse_todo("WEDNESDAY\n- what's up with banks\n")
    assert len(items) == 1
    assert items[0]["raw"] == "- what's up with banks"
    assert items[0]["day_context"] == "WEDNESDAY"


def test_parse_url_line_joins_prior_item():
    text = "how to retire early at 50\nhttps://youtube.com/watch?v=abc\n"
    items = import_todo.parse_todo(text)
    assert len(items) == 1
    assert "how to retire early at 50" in items[0]["raw"]
    assert "https://youtube.com/watch?v=abc" in items[0]["raw"]
    assert len(items[0]["lines"]) == 2  # multi-line item


def test_parse_blank_line_separates_items():
    text = "first idea\n\nsecond idea\n"
    items = import_todo.parse_todo(text)
    assert [it["raw"] for it in items] == ["first idea", "second idea"]


def test_parse_equals_and_dash_bullets_are_separate_items():
    text = "- bigfundr 3\n= bigfundr + adread 6\n"
    items = import_todo.parse_todo(text)
    assert [it["raw"] for it in items] == ["- bigfundr 3", "= bigfundr + adread 6"]


def test_parse_indented_line_is_continuation():
    text = "create ai tools\n  before handing me check sources\n"
    items = import_todo.parse_todo(text)
    assert len(items) == 1
    assert len(items[0]["lines"]) == 2


def test_parse_real_todo_file_nonzero():
    path = os.path.expanduser("~/Desktop/todo.txt")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        items = import_todo.parse_todo(f.read())
    assert len(items) > 50  # messy dump has plenty of items
    # UPPERCASE weekday day-headers are never items (title-case ones may slip
    # through as items per the uppercase-only rule — the classifier skips those).
    assert all(not import_todo.is_day_header(it["raw"].strip()) for it in items)


# ── 2. CLASSIFICATION-JSON APPLICATION (claude injected) ───────────────────────
def test_apply_task_lands_in_tasks_table(client):
    conn = _db()
    ledger = {}
    res = import_common.apply_item(
        conn, "reply to sponsor email",
        {"type": "task", "title": "Reply to sponsor email",
         "category": "business", "priority": "high", "due": None},
        ledger,
    )
    assert res["status"] == "created" and res["destination"] == "task"
    row = conn.execute("SELECT title, category, priority FROM tasks WHERE id=?",
                       (res["id"],)).fetchone()
    conn.close()
    assert row["title"] == "Reply to sponsor email"
    assert row["category"] == "business" and row["priority"] == "high"


def test_apply_note_writes_file_with_imported_tag(client):
    conn = _db()
    ledger = {}
    res = import_common.apply_item(
        conn, "3 REITs to watch",
        {"type": "note", "title": "3 REITs to watch", "tags": ["idea", "video"]},
        ledger,
    )
    conn.close()
    assert res["status"] == "created" and res["destination"] == "note"
    note = vault_store.read_note(res["slug"])
    assert note is not None
    assert import_common.IMPORTED_TAG in note["tags"]
    assert "idea" in note["tags"] and "video" in note["tags"]


def test_apply_journal_appends_with_imported_source(client):
    conn = _db()
    ledger = {}
    res = import_common.apply_item(
        conn, "grateful for a good week",
        {"type": "journal", "title": "grateful for a good week"},
        ledger,
    )
    conn.close()
    assert res["status"] == "created" and res["destination"] == "journal"
    page = vault_store.read_journal(today_iso())
    assert page is not None
    assert any(e["source"] == "imported" for e in page["entries"])


def test_apply_skip_and_uncertain_write_nothing(client):
    conn = _db()
    ledger = {}
    r1 = import_common.apply_item(conn, "old done thing",
                                  {"type": "skip", "reason": "looks-done"}, ledger)
    r2 = import_common.apply_item(conn, "??",
                                  {"type": "uncertain", "note": "unsure"}, ledger)
    conn.close()
    assert r1["status"] == "skipped" and r2["status"] == "skipped"
    assert ledger == {}  # nothing recorded


# ── 3. LEDGER IDEMPOTENCY ──────────────────────────────────────────────────────
def test_ledger_idempotency_note(client):
    conn = _db()
    ledger = {}
    raw = "how to invest in S&P 500"
    result = {"type": "note", "title": "How to invest in S&P 500", "tags": ["idea"]}
    first = import_common.apply_item(conn, raw, result, ledger)
    second = import_common.apply_item(conn, raw, result, ledger)
    conn.close()
    assert first["status"] == "created"
    assert second["status"] == "already"
    # only one note on disk
    notes = [n for n in vault_store.list_notes() if n["title"] == "How to invest in S&P 500"]
    assert len(notes) == 1


def test_ledger_idempotency_task(client):
    conn = _db()
    ledger = {}
    raw = "book dentist"
    result = {"type": "task", "title": "Book dentist", "category": "personal",
              "priority": "low", "due": None}
    import_common.apply_item(conn, raw, result, ledger)
    import_common.apply_item(conn, raw, result, ledger)
    n = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE title='Book dentist'").fetchone()["c"]
    conn.close()
    assert n == 1


def test_ledger_role_lets_one_row_fan_out(client):
    """A tracker row → task + companion note must both apply from the same raw."""
    conn = _db()
    ledger = {}
    raw = "Best malaysia broker | editing | gdoc | Jimnuel (SGD 90)"
    task_res = import_common.apply_item(
        conn, raw, {"type": "task", "title": "Best malaysia broker",
                    "category": "content", "priority": "med", "due": None},
        ledger, role="task")
    note_res = import_common.apply_item(
        conn, raw, {"type": "note", "title": "Best malaysia broker",
                    "tags": ["video", "wip"], "body": raw},
        ledger, role="note")
    conn.close()
    assert task_res["status"] == "created" and note_res["status"] == "created"
    assert len(ledger) == 2  # two distinct keys from one raw row


# ── 4. SHEET ROW MAPPING (pure, no network) ────────────────────────────────────
def test_sheet_tracker_inflight_row_makes_task_and_note():
    results = import_sheet.map_tracker_row(
        ["Best malaysia broker", "editing", "https://docs.google.com/doc", "Jimnuel (SGD 90)"],
        in_done_section=False)
    kinds = [r["type"] for r in results]
    assert kinds == ["task", "note"]
    task = results[0]
    assert task["category"] == "content" and task["priority"] == "med"
    note = results[1]
    assert note["tags"] == ["video", "wip"]
    assert "Jimnuel (SGD 90)" in note["body"]


def test_sheet_tracker_done_section_row_makes_release_note_only():
    results = import_sheet.map_tracker_row(
        ["Finished video", "REVIEWING", "https://docs.google.com/doc2", "Editor X"],
        in_done_section=True)
    assert len(results) == 1
    assert results[0]["type"] == "note"
    assert results[0]["tags"] == ["video", "release-queue"]


def test_sheet_tracker_blank_status_title_only_is_idea_note():
    results = import_sheet.map_tracker_row(["Some raw video idea", "", "", ""],
                                           in_done_section=False)
    assert len(results) == 1
    assert results[0]["type"] == "note"
    assert results[0]["tags"] == ["video", "idea"]


def test_sheet_tracker_blank_row_is_skip():
    results = import_sheet.map_tracker_row(["", "", "", ""])
    assert results[0]["type"] == "skip"


def test_sheet_ideas_row_maps_to_idea_note_with_links_in_body():
    row = ["How to quit job retire in 30s",
           "https://www.youtube.com/watch?v=xZqBsLm7TuM", "", "",
           "https://www.youtube.com/@JarradMorrow", ""]
    r = import_sheet.map_ideas_row(row)
    assert r["type"] == "note"
    assert r["tags"] == ["video", "idea"]
    assert "youtube.com/watch?v=xZqBsLm7TuM" in r["body"]
    assert "JarradMorrow" in r["body"]


def test_sheet_ideas_blank_title_row_is_skipped():
    r = import_sheet.map_ideas_row(["", "", "", "", "https://youtube.com/@ref", ""])
    assert r["type"] == "skip"


def test_sheet_detect_shape_ideas_vs_unknown():
    ideas = [["Epf dividend", "Need by", "Script", "Project Files", "Extra Notes", "Type"]]
    assert import_sheet.detect_shape(ideas) == "ideas"
    finance = [["", "OLD MODEL", "1 min video", "Pay", "3 min video", "Pay"]]
    assert import_sheet.detect_shape(finance) == "unknown"


def test_sheet_map_tab_tracker_tracks_done_section():
    rows = [
        ["LONG VIDEO", "", "", ""],
        ["Active vid", "editing", "gdoc", "Ed (SGD 90)"],
        ["image-2", "", "", ""],
        ["Done vid", "", "gdoc2", "Ed2"],
    ]
    mapped = import_sheet.map_tab(rows, "tracker")
    # row 0: section divider skip; row1: task+note; row2 divider skip; row3 release note
    assert mapped[0]["results"][0]["type"] == "skip"
    assert [r["type"] for r in mapped[1]["results"]] == ["task", "note"]
    assert mapped[3]["results"][0]["tags"] == ["video", "release-queue"]


# ── cluster tagging (vault_store.add_tag / remove_tag — surgical, byte-safe) ────
def test_add_tag_rewrites_only_tags_line_byte_identical_otherwise():
    n = vault_store.create_note(
        "Best REITs 2026", body="https://insta.gr/x\n\nGreat SG REIT breakdown",
        tags=["ig", "link", "idea", "imported"])
    path = os.path.join(vault_store.notes_dir(), n["slug"] + ".md")
    before = open(path).read()
    assert vault_store.add_tag(n["slug"], "market-investing") is True
    after = open(path).read()
    b, a = before.split("\n"), after.split("\n")
    changed = [i for i, (x, y) in enumerate(zip(b, a)) if x != y]
    assert len(b) == len(a) and len(changed) == 1        # ONLY the tags line moved
    assert a[changed[0]] == "tags: [ig, link, idea, imported, market-investing]"
    assert "market-investing" in vault_store.read_note(n["slug"])["tags"]


def test_add_tag_idempotent_and_remove_restores():
    n = vault_store.create_note("Note", body="body text\n", tags=["imported"])
    path = os.path.join(vault_store.notes_dir(), n["slug"] + ".md")
    original = open(path).read()
    assert vault_store.add_tag(n["slug"], "personal-admin") is True
    assert vault_store.add_tag(n["slug"], "personal-admin") is False   # idempotent no-op
    assert vault_store.remove_tag(n["slug"], "personal-admin") is True
    assert open(path).read() == original                 # byte-identical round-trip
    assert vault_store.remove_tag(n["slug"], "personal-admin") is False


def test_add_tag_missing_note_is_safe():
    assert vault_store.add_tag("does-not-exist", "x") is False
    assert vault_store.remove_tag("does-not-exist", "x") is False
