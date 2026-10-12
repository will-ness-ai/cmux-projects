"""Thin wrapper over the cmux-tui CLI (`cmux --session <project> ...`).

Every call goes through the sandbox env wrapper: cmux-tui gives new terminals
the *client's* environment, so an unscrubbed caller would leak its variables
into every Claude it launches.
"""
import base64
import json
import os
import subprocess
import time

from . import config


class CmuxError(RuntimeError):
    pass


class CmuxTui:
    """cmux-tui: one headless server per project, one workspace per thread."""
    name = "tui"
    delivery_needs_idle = True   # we type into the prompt, so wait for an idle agent

    def __init__(self, session: str = config.PROJECT):
        self.session = session

    def setup(self, store):
        self.start_server(store.dir / "cmux-server.log")
        subprocess.run([config.SANDBOX, config.CMUX_BIN, "agent", "hook", "install", "claude"],
                       capture_output=True, check=True)

    def launch_workspace(self, name, command, group=None):
        ws, term = self.create_workspace(name)
        self.type_line(term, command)
        return ws, term

    def poll(self):
        snap = self.snapshot()
        return {
            "focused": next((w["id"] for w in snap.get("workspaces", []) if w.get("focused")), None),
            "agents": {a["terminal_id"]: a["state"] for a in snap.get("agents", [])},
            "live": {t["id"] for t in snap.get("terminals", [])},
        }

    def deliver(self, term_id, text, from_user=False):
        self.paste_and_submit(term_id, text)

    def ensure_group(self, project, anchor_ws):
        return None

    def open_dashboard(self, ws_id, url):
        return None                              # cmux-tui has no built-in browser

    def _argv(self, args):
        return [config.SANDBOX, config.CMUX_BIN, "--session", self.session, *args]

    def run(self, *args, as_json=True, timeout=20):
        argv = self._argv((["--json"] if as_json else []) + list(args))
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise CmuxError((r.stderr or r.stdout).strip())
        if as_json:
            out = r.stdout.strip()
            return json.loads(out) if out else None
        return r.stdout

    # server ---------------------------------------------------------------
    def server_running(self) -> bool:
        try:
            self.run("server", "status", as_json=False, timeout=5)
            return True
        except Exception:
            return False

    def start_server(self, log_path):
        if self.server_running():
            return
        with open(log_path, "ab") as log:
            subprocess.Popen(self._argv(["server", "start"]), stdout=log, stderr=log,
                             start_new_session=True)
        for _ in range(50):
            if self.server_running():
                return
            time.sleep(0.1)
        raise CmuxError("cmux server did not start")

    def stop_server(self):
        self.run("server", "stop", as_json=False)

    # resources ------------------------------------------------------------
    def snapshot(self):
        return self.run("session", "current", "snapshot")

    def create_workspace(self, name):
        v = self.run("workspace", "create", "--name", name)["value"]
        return v["workspace_id"], v["terminal_id"]

    def focus_workspace(self, ws_id):
        """Move the shared default focus, then bounce attached TUI viewers.

        cmux-tui keeps focus per client by design, so `workspace focus` only moves
        the default that a *fresh* attach opens on. Viewers started with
        `cmuxp view` re-attach in a loop, so detaching them lands them on ws_id.
        (The macOS app has one window and `cmux select-workspace` moves it directly.)
        """
        self.run("workspace", ws_id, "focus", as_json=False)
        for c in self.run("client", "list") or []:
            if c.get("client_kind") == "tui" and not c.get("self"):
                try:
                    self.run("client", c["id"], "detach", as_json=False)
                except CmuxError:
                    pass

    def rename_workspace(self, ws_id, name):
        self.run("workspace", ws_id, "rename", "--name", name, as_json=False)

    def close_workspace(self, ws_id, term_id=None):
        if term_id:
            try:
                self.run("terminal", term_id, "close", as_json=False)
            except CmuxError:
                pass
        try:
            self.run("workspace", ws_id, "close", as_json=False)
        except CmuxError:
            pass

    def type_line(self, term_id, text):
        self.run("terminal", term_id, "write", "--text", text, as_json=False)
        self.run("terminal", term_id, "keys", "enter", as_json=False)

    def paste_and_submit(self, term_id, text):
        """Bracketed paste so multi-line text lands as one prompt, then Enter."""
        payload = b"\x1b[200~" + text.encode() + b"\x1b[201~"
        self.run("terminal", term_id, "write", "--bytes-base64",
                 base64.b64encode(payload).decode(), as_json=False)
        time.sleep(0.4)
        self.run("terminal", term_id, "keys", "enter", as_json=False)

    def screen_text(self, term_id):
        v = self.run("terminal", term_id, "screen", "read")
        v = v.get("value", v) if isinstance(v, dict) else v
        return v.get("text", "") if isinstance(v, dict) else str(v)


class CmuxApp:
    """The cmux macOS app (meant for an isolated tagged build), driven by its CLI.

    Project = workspace group, thread = workspace in it. Messages go through
    `cmux agent message`, which cmux delivers via Claude's own hooks, so they
    never collide with text in the prompt and need no idle check. The app's
    socket defaults to cmux-only access, so run `cmuxp up` from a terminal
    inside that app.
    """
    name = "app"
    delivery_needs_idle = False

    STATE = {"running": "working", "needs input": "blocked", "idle": "idle"}

    def __init__(self, session: str = config.PROJECT):
        self.session = session

    def run(self, *args, as_json=True, stdin=None, timeout=20):
        env = dict(os.environ)
        if config.APP_SOCKET:
            env["CMUX_SOCKET_PATH"] = config.APP_SOCKET
        sock = ["--socket", config.APP_SOCKET] if config.APP_SOCKET else []
        argv = [config.CMUX_BIN, *sock, *(["--json"] if as_json else []), "--id-format", "uuids", *args]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, input=stdin)
        if r.returncode != 0:
            raise CmuxError((r.stderr or r.stdout).strip())
        out = r.stdout.strip()
        return json.loads(out) if as_json and out else out

    def _rpc(self, method, params):
        out = self.run("rpc", method, json.dumps(params), as_json=False)
        return json.loads(out) if out else {}

    def setup(self, store):
        self._rpc("workspace.list", {})          # fails fast if the socket refuses us

    def stop_server(self):
        pass                                     # never quit the app from here

    def server_running(self):
        try:
            self._rpc("workspace.list", {})
            return True
        except Exception:
            return False

    def ensure_group(self, project, anchor_ws):
        out = self.run("workspace-group", "create", "--name", project, "--from", anchor_ws,
                       "--idempotency-key", f"cmuxp-{project}")
        return out.get("group_id") or out.get("id") or (out.get("group") or {}).get("id")

    def open_dashboard(self, ws_id, url):
        out = self.run("new-pane", "--type", "browser", "--direction", "right", "--workspace", ws_id,
                       "--url", url, "--focus", "false", as_json=False)
        return out.strip() or "opened"

    def launch_workspace(self, name, command, group=None):
        params = {"title": name, "initial_input": command + "\r", "focus": False,
                  "cwd": str(config.project_dir(self.session) / "files")}
        if group:
            params.update(group_id=group, group_placement="end")
        res = self._rpc("workspace.create", params)
        ws = res.get("workspace_id") or res.get("id")
        return ws, ws                            # agents are tracked per workspace

    def poll(self):
        # Plain workspace.list has no terminals; the mobile-shaped list carries
        # terminals[].agent_state, so prefer it and fall back if refused.
        try:
            res = self._rpc("mobile.workspace.list", {})
        except CmuxError:
            res = self._rpc("workspace.list", {})
        focused, agents, live = None, {}, set()
        for w in res.get("workspaces", []):
            live.add(w["id"])
            if w.get("is_selected") or w.get("selected"):
                focused = w["id"]
            if "terminals" not in w:
                agents[w["id"]] = "idle"         # no agent state here; hook delivery is safe anyway
                continue
            states = [str(t.get("agent_state") or "").strip().lower() for t in w.get("terminals", [])]
            states = [self.STATE.get(x, x) for x in states if x]
            agents[w["id"]] = ("blocked" if "blocked" in states else
                               "working" if "working" in states else
                               states[0] if states else "idle")
        return {"focused": focused, "agents": agents, "live": live}

    def focus_workspace(self, ws_id):
        self._rpc("workspace.select", {"workspace_id": ws_id})
        if config.APP_PATH:                      # bring the sandbox app to the front
            subprocess.run(["open", "-a", config.APP_PATH], check=False)

    def deliver(self, ws_id, text, from_user=False):
        if from_user:
            self.run("send", "--workspace", ws_id, "--paste", "--", text, as_json=False)
            self.run("send-key", "--workspace", ws_id, "enter", as_json=False)
        else:
            self.run("agent", "message", ws_id, "--from", "cmuxp", "-", as_json=False, stdin=text)

    def screen_text(self, ws_id):
        return self.run("read-screen", "--workspace", ws_id, "--lines", "80", as_json=False)

    def close_workspace(self, ws_id, term_id=None):
        try:
            self._rpc("workspace.close", {"workspace_id": ws_id})
        except CmuxError:
            pass


def get_backend(session: str = config.PROJECT):
    return CmuxApp(session) if config.BACKEND == "app" else CmuxTui(session)
