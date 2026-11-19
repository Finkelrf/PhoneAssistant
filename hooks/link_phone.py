#!/usr/bin/env python3
"""Link a phone to Remote's email and calendar voice access (standard library only, runs on the computer).

  python3 hooks/link_phone.py               print a one-time code (10 minutes, 5 tries) to type on the phone:
                                            Settings > Wake word and voice > Email and calendar
  python3 hooks/link_phone.py --unlink-all  remove every linked phone

It uses the hook key file, which only this computer can read, so a device on the network can't link itself.
Settings (environment): REMOTE_HOOK_URL (default http://127.0.0.1:8796),
REMOTE_TOKEN_FILE (default ~/Documents/home-dashboard/configs/remote/token).
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("REMOTE_HOOK_URL", "http://127.0.0.1:8796").rstrip("/")
TOKEN_FILE = os.path.expanduser(os.environ.get("REMOTE_TOKEN_FILE", "~/Documents/home-dashboard/configs/remote/token"))


def post(path):
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    req = urllib.request.Request(URL + path, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "X-Remote-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def main():
    try:
        if "--unlink-all" in sys.argv[1:]:
            n = post("/hook/phone-unlink-all")["removed"]
            print(f"Removed {n} linked phone(s).")
            return 0
        r = post("/hook/phone-code")
    except (OSError, urllib.error.URLError, ValueError, KeyError) as e:
        print(f"Couldn't reach the Remote server at {URL}: {e}", file=sys.stderr)
        return 1
    code = r["code"]
    print(f"Code: {code[:4]}-{code[4:]}")
    print(f"Type it on the phone within {r['expires_in'] // 60} minutes:")
    print("Settings > Wake word and voice > Email and calendar > Link.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
