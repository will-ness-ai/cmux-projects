"""The project process: watches cmux, keeps thread states, delivers queued
messages into idle Claudes, and serves the dashboard on CMUXP_PORT."""
import json
import os
import shutil
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .backend import CmuxError, get_backend
from .store import Store

WEB = config.ROOT / "cmuxp" / "web"
BLOCKED = {"blocked", "needsInput", "needs_input", "waiting", "permission"}


class Daemon:
    def __init__(self, project=config.PROJECT):
        self.store = Store(project)
        self.cmux = get_backend(project)
        self.focused_ws = None
        self.agents = {}            # terminal_id -> agent record
        self.last_delivery = {}     # target -> ts
        self.idle_since_wait = set()
        self.colleague_turn = {}    # thread -> [phase, ts] while a colleague's message runs
        self.pane_tried = set()     # workspaces we already tried to give a dashboard pane
        self.lock = threading.Lock()

    # derived state ------------------------------------------------------------
    def agent_state(self, term):
        a = self.agents.get(term)
        return a["state"] if a else None

    @staticmethod
    def display_state(t, agent_state):
        if t.get("resolved"):
            return "resolved"
        if t.get("waiting") or (agent_state in BLOCKED):
            return "blocked"
        if agent_state == "working":
            return "working"
        if agent_state is None:
            return "starting"
        return "idle"

    def tick(self):
        snap = self.cmux.poll()
        with self.lock:
            self.agents = {k: {"state": v} for k, v in snap["agents"].items()}
            self.focused_ws = snap["focused"]
            live_terms = snap["live"]
        with self.store.update() as st:
            for t in st["threads"].values():
                s = self.agent_state(t["terminal_id"])
                ct = self.colleague_turn.get(t["id"])
                if ct:
                    # The turn a colleague's message started is not the user answering;
                    # sit it out: sent -> running -> idle again (or give up after 30s).
                    if s == "working":
                        ct[0] = "running"
                    elif s == "idle" and ct[0] == "running" or time.time() - ct[1] > 30 and ct[0] == "sent":
                        del self.colleague_turn[t["id"]]
                elif t.get("waiting"):
                    if s == "idle":
                        self.idle_since_wait.add(t["id"])
                    elif s == "working" and t["id"] in self.idle_since_wait:
                        t["waiting"] = None          # the user answered in the thread's terminal
                        self.idle_since_wait.discard(t["id"])
                t["agent_state"] = s
                t["alive"] = t["terminal_id"] in live_terms
                t["display_state"] = self.display_state(t, s)
            c = st.get("coordinator")
            if c:
                c["agent_state"] = self.agent_state(c["terminal_id"])
        self.ensure_panes()
        self.deliver()

    def ensure_panes(self):
        # The MCP server that starts a thread may run older code, so the daemon backfills panes.
        st = self.store.read()
        done = st.get("dashboard_panes") or {}
        wss = [(st.get("coordinator") or {}).get("workspace_id")] + \
              [t.get("workspace_id") for t in st["threads"].values()]
        new = {}
        for ws in filter(None, wss):
            if ws in done or ws in self.pane_tried:
                continue
            self.pane_tried.add(ws)
            try:
                pane = self.cmux.open_dashboard(ws, f"http://127.0.0.1:{config.PORT}/")
            except Exception as e:  # noqa: BLE001
                print(f"dashboard pane for {ws} failed: {e}", flush=True)
                continue
            if pane:
                new[ws] = pane
        if new:
            with self.store.update() as st:
                st.setdefault("dashboard_panes", {}).update(new)

    def deliver(self):
        st = self.store.read()
        by_target = {}
        for item in self.store.pending():
            by_target.setdefault((item["to"], item["from"] == "user"), []).append(item)
        for (target, from_user), items in by_target.items():
            term = (st["coordinator"] or {}).get("terminal_id") if target == "coordinator" \
                else st["threads"].get(target, {}).get("terminal_id")
            state = self.agent_state(term) if term else None
            if state is None or (self.cmux.delivery_needs_idle and state != "idle"):
                continue
            if time.time() - self.last_delivery.get(target, 0) < 4:
                continue
            text = "\n\n".join(i["text"] for i in items)
            try:
                self.cmux.deliver(term, text, from_user=from_user)
            except CmuxError as e:               # e.g. the user is mid-typing; retry next tick
                print(f"deliver to {target} deferred: {e}", flush=True)
                continue
            if not from_user and st["threads"].get(target, {}).get("waiting"):
                # A colleague's message wakes the thread too; that is not the
                # user answering its question, so don't clear "waiting" on it.
                self.idle_since_wait.discard(target)
                self.colleague_turn[target] = ["sent", time.time()]
            self.last_delivery[target] = time.time()
            self.store.mark_delivered({i["id"] for i in items})

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(1)

    # api -------------------------------------------------------------------
    def api_state(self):
        st = self.store.read()
        threads = sorted(st["threads"].values(), key=lambda t: -(t.get("last_reply_at") or t["created_at"]))
        coord = st.get("coordinator") or {}
        return {"project": st["project"], "focused_workspace": self.focused_ws,
                "coordinator": {**coord, "focused": coord.get("workspace_id") == self.focused_ws},
                "threads": [{**t, "focused": t["workspace_id"] == self.focused_ws} for t in threads],
                "timeline": self.store.timeline(40), "now": time.time()}

    def focus(self, target):
        st = self.store.read()
        ws = (st["coordinator"] or {}).get("workspace_id") if target == "coordinator" \
            else st["threads"][target]["workspace_id"]
        self.cmux.focus_workspace(ws)
        with self.lock:
            self.focused_ws = ws
        return ws

    def user_message(self, target, text):
        self.store.enqueue(target, text, "user")   # typed in as the user; the prompt hook logs it


def make_handler(d: Daemon):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, d.api_state())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            try:
                if self.path == "/api/focus":
                    return self._send(200, {"workspace_id": d.focus(body["id"])})
                if self.path == "/api/message":
                    d.user_message(body["to"], body["text"])
                    return self._send(200, {"ok": True})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})
            self._send(404, {"error": "not found"})
    return H


def main():
    d = Daemon()
    srv = ThreadingHTTPServer(("127.0.0.1", config.PORT), make_handler(d))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"cmuxp dashboard on http://127.0.0.1:{config.PORT}/ (project {d.store.project})", flush=True)
    if shutil.which("caffeinate"):               # macOS: idle sleep killed Claudes mid-reply
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    d.run()


if __name__ == "__main__":
    main()
