#!/usr/bin/env python3
"""Life OS web server — thin entry point.

Mirrors youtube-assistant/executors/invoicing/server.py: argparse --port/--db,
point web_core._DB_PATH at the chosen DB, init/migrate the schema, register each
routes_*.bp blueprint, run.

Run: python3 server.py [--port 5070] [--db data/app.db]
"""

from __future__ import annotations

import argparse

from web_core import app, DB_PATH
import db_init
import routes_main, routes_tasks, routes_notes, routes_journal, routes_goals

for _bpmod in (routes_main, routes_tasks, routes_notes, routes_journal, routes_goals):
    app.register_blueprint(_bpmod.bp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5070)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    # db() in web_core reads module-level _DB_PATH — set it there so --db is honoured.
    import web_core as _wc
    _wc._DB_PATH = args.db
    db_init.init_db(args.db)

    print(f"Life OS running at http://localhost:{args.port}")
    print(f"Database: {args.db}")
    app.run(port=args.port, debug=True, use_reloader=False)


if __name__ == "__main__":
    main()
