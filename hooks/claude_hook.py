#!/usr/bin/env python3
"""Claude Code hook that reports to the Remote server (standard library only, runs on the host).

  claude_hook.py event       SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Notification, Stop, SessionEnd
  claude_hook.py permission  PermissionRequest: waits for Allow / Deny from the phone

It never gets in the way: if the server is down, slow or answers nothing, it exits 0 with no output, and Claude
Code carries on as if there were no hook (the normal prompt shows on the computer).

Settings (environment): REMOTE_HOOK_URL (default http://127.0.0.1:8796),
REMOTE_TOKEN_FILE (default ~/Documents/home-dashboard/configs/remote/token).
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get("REMOTE_HOOK_URL", "http://127.0.0.1:8796").rstrip("/")
TOKEN_FILE = os.path.expanduser(os.environ.get("REMOTE_TOKEN_FILE", "~/Documents/home-dashboard/configs/remote/token"))


def post(path, payload, timeout):
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    req = urllib.request.Request(URL + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Remote-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "event"
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return
    keep = ("session_id", "cwd", "hook_event_name", "tool_name", "tool_input", "prompt", "message",
            "notification_type", "source", "reason", "permission_mode")
    ev = {k: ev[k] for k in keep if k in ev}
    if mode == "permission":
        ans = post("/hook/permission", ev, timeout=3700)
        decision = ans.get("decision")
        if decision in ("allow", "deny"):
            d = {"behavior": decision}
            if decision == "deny":
                d["message"] = ans.get("message") or "Denied from the phone"
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}))
    else:
        post("/hook/event", ev, timeout=2)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # never block Claude Code
        pass
    sys.exit(0)
