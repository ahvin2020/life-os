"""One-time Google OAuth for Life OS (run on the Mac, where a browser can open).

Prereqs (Kelvin, in Google Cloud Console):
  1. Create a project; enable the Gmail API and the Google Calendar API.
  2. Create an OAuth client of type "Desktop app"; download the JSON.
  3. Save it as data/google_client_secret.json in this repo.

Then run:  python3 scripts/google_auth.py
It opens a browser for consent and writes data/google_token.json (chmod 600).

NAS note: data/ is NOT synced to the NAS, so after authorising here, copy
data/google_token.json to the NAS data volume manually for the container to use it.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from ai.google_client import SCOPES, _SECRET, _TOKEN


def main():
    if not os.path.exists(_SECRET):
        print(f"Missing {_SECRET}\nDownload your OAuth Desktop-client JSON there first "
              "(see this script's docstring).")
        return 1
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(_SECRET, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(_TOKEN, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    os.chmod(_TOKEN, 0o600)
    print(f"Authorised ✓  wrote {_TOKEN}")
    print("If deploying to the NAS, copy that token file to the NAS data/ volume.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
