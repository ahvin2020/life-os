"""Notes archive lifecycle + on-this-day resurfacing."""

import vault_store


def test_archive_roundtrip_and_preserves_fields():
    n = vault_store.create_note("Watch later reel", "body here",
                                ["market-investing"], pinned=True)
    assert n["archived"] is False
    a = vault_store.set_archived(n["slug"], True)
    assert a["archived"] is True
    assert a["pinned"] is True and a["tags"] == ["market-investing"]
    assert a["body"] == "body here"           # body survives archive
    back = vault_store.set_archived(n["slug"], False)
    assert back["archived"] is False


def test_archive_route_and_grid_excludes_archived(client):
    vault_store.create_note("Keep me visible", "x", ["inspiration"])
    hidden = vault_store.create_note("Archive me", "y", ["inspiration"])
    client.post("/notes/" + hidden["slug"] + "/archive", data={"archived": "1"})
    html = client.get("/notes").data.decode()
    assert "Keep me visible" in html
    assert "Archive me" not in html            # archived drops out of the main grid
    # …but shows under the archived view
    arch = client.get("/notes?archived=1").data.decode()
    assert "Archive me" in arch


def test_on_this_day_matches_anniversaries():
    vault_store.write_note("a-year-ago", "A year ago note", ["x"], "b", False,
                           "2025-07-10T09:00:00+08:00")
    vault_store.write_note("a-month-ago", "A month ago note", ["x"], "b", False,
                           "2026-06-10T09:00:00+08:00")
    vault_store.write_note("unrelated", "Unrelated", ["x"], "b", False,
                           "2026-03-03T09:00:00+08:00")
    spans = vault_store.notes_on_this_day("2026-07-10")
    titles = {f["note"]["title"] for f in spans}
    assert "A year ago note" in titles
    assert "A month ago note" in titles
    assert "Unrelated" not in titles
