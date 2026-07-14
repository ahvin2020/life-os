"""Serve a document from a configured root over the Tailscale-only web app — the 'link'
delivery mode for document retrieval, and the private alternative to pushing a sensitive
file through Telegram's cloud.

GET-only, single-user (Tailscale is the perimeter, same trust as /notes/<slug>/audio).
Every path goes through docs.resolve_doc — the one traversal guard — so '..', absolute
paths, and out-of-root symlinks 404. There is deliberately NO directory-listing endpoint.
"""

from __future__ import annotations

from flask import Blueprint, send_file, abort

from core.web_core import db
from domain import docs

bp = Blueprint("docs", __name__)


@bp.route("/docs/<root_key>/<path:rel>")
def serve_doc(root_key, rel):
    path = docs.resolve_doc(db(), root_key, rel)
    if not path:
        abort(404)
    return send_file(path)          # mimetype guessed; inline so PDFs render on the phone
