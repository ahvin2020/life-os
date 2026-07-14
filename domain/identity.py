"""Auto-derived identity (suggest-then-confirm).

Scans the connected sources for signals of WHO Kelvin is and who his family are — his
Google account email + the names on his personal documents (passports/ICs reveal
people) — then a single Claude synthesis drafts an identity block for profile.md. It is
NEVER written silently: the router proposes it and only Kelvin's "yes" saves it (via the
pending-action infra). Everything gathered is DATA (tools="" synthesis).
"""

from domain import docs

# document keywords whose FILENAMES tend to carry a person's real name
_PERSON_DOC_KWS = ("passport", "nric", "identity card", "birth certificate", "ic")


def person_doc_names(conn, limit: int = 30) -> list:
    names, seen = [], set()
    for kw in _PERSON_DOC_KWS:
        try:
            for h in docs.search_documents(conn, kw, limit=10):
                key = h["name"].lower()
                if key not in seen:
                    seen.add(key)
                    names.append(h["name"])
        except Exception:
            pass
    return names[:limit]


def gather_signals(conn) -> dict:
    sig = {"email": "", "doc_names": person_doc_names(conn)}
    try:
        from ai import google_client
        if google_client.is_configured():
            sig["email"] = google_client.gmail_address()
    except Exception:
        pass
    return sig


def propose(conn, claude_fn=None) -> str:
    """Draft an identity block from the gathered signals. "" if there's nothing to go on."""
    sig = gather_signals(conn)
    if not sig["email"] and not sig["doc_names"]:
        return ""
    docs_txt = "\n".join(f"- {n}" for n in sig["doc_names"]) or "(none found)"
    prompt = (
        "You are drafting a short IDENTITY block for Kelvin's assistant profile, so it can "
        "tell 'my passport' from a family member's. Use ONLY this evidence — invent no one:\n\n"
        f"Google account email: {sig['email'] or '(unknown)'}\n"
        f"Personal-document filenames found (passports/ICs carry people's real names):\n{docs_txt}\n\n"
        "Infer Kelvin's own full name, and each OTHER person as a family member with a "
        "LIKELY relationship (wife / son / daughter — append '?' to any relationship you're "
        "guessing). Output 3-8 short markdown lines and nothing else, e.g.:\n"
        "Name: Lee Jun Kai (me)\nWife: Ong Mei Fang?\nChild: Lee Xin Yi?\n")
    from ai.claude_cli import call_claude
    fn = claude_fn or call_claude
    return (fn(prompt) or "").strip()
