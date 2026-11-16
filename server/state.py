"""Claude sessions and permission requests, kept in memory and saved to a small JSON file.

Hooks from Claude Code (hooks/claude_hook.py) report what each session does; the phone page reads a snapshot of
it over Server-Sent Events and answers permission requests.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

ENDED_KEEP = 30 * 60        # an ended session stays on the list this long
IDLE_FORGET = 24 * 3600     # sessions with no news for this long are dropped
MAX_TEXT = 4000


def _clip(text, n=MAX_TEXT):
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def describe_tool(tool: str, data: dict) -> tuple[str, str]:
    """A short title and a longer detail for a tool call, for the phone."""
    data = data if isinstance(data, dict) else {}
    t = tool or "Tool"
    if t == "Bash":
        cmd = data.get("command", "")
        return (_clip(data.get("description") or "Run a command", 120), _clip(cmd))
    if t in ("Edit", "Write", "Read", "NotebookEdit", "MultiEdit"):
        path = data.get("file_path") or data.get("notebook_path") or ""
        verb = {"Edit": "Edit", "MultiEdit": "Edit", "Write": "Write", "Read": "Read", "NotebookEdit": "Edit notebook"}[t]
        detail = ""
        if t == "Edit":
            detail = f"- {_clip(data.get('old_string', ''), 1500)}\n+ {_clip(data.get('new_string', ''), 1500)}"
        elif t == "Write":
            detail = _clip(data.get("content", ""), 2000)
        return (f"{verb} {os.path.basename(path) or path}", _clip(path + ("\n\n" + detail if detail else "")))
    if t in ("WebFetch", "WebSearch"):
        return (t, _clip(data.get("url") or data.get("query") or ""))
    if t.startswith("mcp__"):
        parts = t.split("__")
        name = parts[-1] if len(parts) > 2 else t
        return (name.replace("_", " "), _clip(json.dumps(data, ensure_ascii=False, indent=1)))
    return (t, _clip(json.dumps(data, ensure_ascii=False, indent=1)))


class Hub:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.sessions: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.futures: dict[str, asyncio.Future] = {}
        self.settings = {"away": False, "wait": 600}
        self.alert = {"seq": 0, "kind": "", "session": ""}
        self.version = 0
        self._listeners: set[asyncio.Queue] = set()
        self._load()

    # ---------- persistence ----------
    def _load(self):
        try:
            raw = json.loads(self.state_file.read_text())
            self.sessions = raw.get("sessions", {})
            self.settings.update(raw.get("settings", {}))
            for s in self.sessions.values():
                if s.get("state") == "permission":
                    s["state"] = "waiting"
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessions": self.sessions, "settings": self.settings}))
        os.replace(tmp, self.state_file)

    # ---------- change notification ----------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q):
        self._listeners.discard(q)

    def changed(self, save=True):
        self.version += 1
        if save:
            try:
                self._save()
            except OSError:
                pass
        for q in list(self._listeners):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(self.version)

    def ring(self, kind: str, session_id: str = ""):
        self.alert = {"seq": self.alert["seq"] + 1, "kind": kind, "session": session_id}

    def snapshot(self) -> dict:
        self.prune()
        sessions = sorted(self.sessions.values(), key=lambda s: (s.get("state") == "ended", -s.get("updated", 0)))
        return {
            "version": self.version,
            "now": time.time(),
            "settings": self.settings,
            "alert": self.alert,
            "sessions": sessions,
            "pending": sorted(self.pending.values(), key=lambda p: p["created"]),
        }

    def prune(self):
        now = time.time()
        gone = [k for k, s in self.sessions.items()
                if (s.get("state") == "ended" and now - s.get("updated", 0) > ENDED_KEEP)
                or now - s.get("updated", 0) > IDLE_FORGET]
        for k in gone:
            if not any(p["session_id"] == k for p in self.pending.values()):
                del self.sessions[k]

    # ---------- hook events ----------
    def _session(self, ev: dict) -> dict:
        sid = str(ev.get("session_id") or "unknown")[:100]
        s = self.sessions.get(sid)
        now = time.time()
        if s is None:
            s = self.sessions[sid] = {"id": sid, "cwd": "", "name": "", "prompt": "", "state": "idle",
                                      "activity": "", "started": now, "updated": now}
        cwd = ev.get("cwd")
        if cwd:
            s["cwd"] = _clip(cwd, 300)
            s["name"] = os.path.basename(cwd.rstrip("/")) or cwd
        s["updated"] = now
        return s

    def event(self, ev: dict):
        name = ev.get("hook_event_name", "")
        s = self._session(ev)
        if name == "SessionStart":
            s["state"] = "idle"
            s["activity"] = ""
        elif name == "UserPromptSubmit":
            s["state"] = "working"
            s["prompt"] = _clip(ev.get("prompt", ""), 300)
            s["activity"] = "Thinking"
            self._drop_pending(s["id"])
        elif name == "PreToolUse":
            title, _ = describe_tool(ev.get("tool_name", ""), ev.get("tool_input", {}))
            s["state"] = "working"
            s["activity"] = title
        elif name == "PostToolUse":
            s["state"] = "working"
            s["activity"] = "Thinking"
            self._drop_pending(s["id"])
        elif name == "Notification":
            kind = ev.get("notification_type", "")
            msg = _clip(ev.get("message", ""), 300)
            if kind == "idle_prompt":
                if s["state"] != "permission":
                    s["state"] = "idle"
            elif kind in ("auth_success",):
                pass
            else:
                if not any(p["session_id"] == s["id"] for p in self.pending.values()):
                    s["state"] = "waiting"
                    s["activity"] = msg or "Needs you"
                    self.ring("attention", s["id"])
        elif name == "Stop":
            s["state"] = "idle"
            s["activity"] = "Done"
            self._drop_pending(s["id"])
            self.ring("done", s["id"])
        elif name == "SessionEnd":
            s["state"] = "ended"
            s["activity"] = ""
            self._drop_pending(s["id"])
        self.changed()

    def _drop_pending(self, session_id: str):
        for rid, p in list(self.pending.items()):
            if p["session_id"] == session_id:
                self.resolve(rid, None, notify=False)

    # ---------- permission requests ----------
    def add_request(self, ev: dict) -> str:
        s = self._session(ev)
        tool = ev.get("tool_name", "")
        title, detail = describe_tool(tool, ev.get("tool_input", {}))
        rid = secrets.token_urlsafe(9)
        self.pending[rid] = {"id": rid, "session_id": s["id"], "session": s["name"], "tool": tool,
                             "title": title, "detail": detail, "created": time.time(),
                             "remote": bool(self.settings.get("away"))}
        self.futures[rid] = asyncio.get_running_loop().create_future()
        s["state"] = "permission"
        s["activity"] = f"Asks: {title}"
        self.ring("permission", s["id"])
        self.changed()
        return rid

    def resolve(self, rid: str, decision, notify=True) -> bool:
        p = self.pending.pop(rid, None)
        fut = self.futures.pop(rid, None)
        if p is None:
            return False
        if fut and not fut.done():
            fut.set_result(decision)
        s = self.sessions.get(p["session_id"])
        if s and s.get("state") == "permission" and not any(q["session_id"] == s["id"] for q in self.pending.values()):
            s["state"] = "working" if decision == "allow" else ("idle" if decision == "deny" else "working")
            s["activity"] = {"allow": "Allowed from phone", "deny": "Denied from phone"}.get(decision, s["activity"])
        if notify:
            self.changed()
        return True
