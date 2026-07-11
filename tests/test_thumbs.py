"""Thumbnail classification + negative-cache logic (offline, deterministic)."""

from domain import thumbs


def test_youtube_id_forms():
    assert thumbs.youtube_id("https://www.youtube.com/watch?v=1vxEdadW5Ds") == "1vxEdadW5Ds"
    assert thumbs.youtube_id("https://youtu.be/1vxEdadW5Ds?si=x") == "1vxEdadW5Ds"
    assert thumbs.youtube_id("https://youtube.com/shorts/1vxEdadW5Ds") == "1vxEdadW5Ds"
    assert thumbs.youtube_id("https://www.instagram.com/reel/Dakeki8vd0E/") is None


def test_note_kind():
    yt = {"url": "https://youtu.be/1vxEdadW5Ds", "domain": "youtu.be"}
    reel = {"url": "https://www.instagram.com/reel/Dakeki8vd0E/", "domain": "instagram.com"}
    post = {"url": "https://www.instagram.com/p/Cg1s_f4ukzp/", "domain": "instagram.com"}
    art = {"url": "https://malaymail.com/x", "domain": "malaymail.com"}
    text = {"url": None, "domain": None}
    photo = {"url": None, "media": "vault/.media/x.jpg"}
    assert thumbs.note_kind(yt) == "video"
    assert thumbs.note_kind(reel) == "video"
    assert thumbs.note_kind(post) == "photo"
    assert thumbs.note_kind(art) == "link"
    assert thumbs.note_kind(text) == "text"
    assert thumbs.note_kind(photo) == "photo"


def test_text_note_resolves_to_none_and_negative_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbs.vault_store, "VAULT_DIR", str(tmp_path))
    note = {"url": None, "domain": None}
    assert thumbs.resolve("plain-note", note) is None
    # a second call must NOT hit the network — the miss marker short-circuits it
    assert thumbs.resolve("plain-note", note) is None
    assert (tmp_path / ".thumbs" / "plain-note.miss").exists()


def test_has_thumb():
    assert thumbs.has_thumb({"url": "https://youtu.be/1vxEdadW5Ds", "domain": "youtu.be"})
    assert not thumbs.has_thumb({"url": None, "domain": None})
