"""Drive the cmux macOS app through its CLI: project = workspace group, thread = workspace."""
import json
import os
import subprocess

from . import config


class CmuxError(RuntimeError):
    pass


class CmuxApp:
    """The cmux macOS app, driven by its CLI.

    Project = workspace group, thread = workspace in it. Messages go through
    `cmux agent message`, which cmux delivers via Claude's own hooks, so they
    never collide with text in the prompt and need no idle check. The app's
    socket defaults to cmux-only access, so run `cmuxp up` from a terminal
    inside that app.
    """
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
        if config.APP_PATH:                      # bring that app build to the front
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
    return CmuxApp(session)
