"""cmuxp up | down | status | dashboard [--reopen] | focus <thread|coordinator> | thread <title> <brief>"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

from . import agents, config
from .backend import get_backend
from .store import Store


def signed_in():
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config.CLAUDE_CONFIG)}
    r = subprocess.run(["claude", "-p", "--model", config.MODEL, "Reply with OK"], env=env,
                       capture_output=True, text=True, timeout=120, cwd=str(config.HOME))
    return r.returncode == 0 and "OK" in r.stdout


def up(store, cmux):
    if not signed_in():
        sys.exit("cmuxp's Claude is not signed in yet. Run: bin/cmuxp login")
    cmux.setup(store)
    c = agents.start_coordinator(store, cmux)
    pidf = store.dir / "daemon.pid"
    if not daemon_running(pidf):
        log = open(store.dir / "daemon.log", "ab")
        p = subprocess.Popen([sys.executable, "-m", "cmuxp.daemon"], stdout=log, stderr=log,
                             env={**os.environ, "PYTHONPATH": str(config.ROOT)}, start_new_session=True)
        pidf.write_text(str(p.pid))
    dashboard(store, cmux)
    print(f"project {store.project}: coordinator in workspace {c['workspace_id']}")
    print(f"dashboard: http://127.0.0.1:{config.PORT}/")


def daemon_running(pidf):
    try:
        os.kill(int(pidf.read_text().strip()), 0)   # works on macOS too (no /proc)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def dashboard(store, cmux, reopen=False):
    """Open the dashboard as a browser pane in every project workspace.

    Each workspace gets its own pane because selecting a thread's workspace
    hides the others; this way the dashboard stays on screen after a click."""
    url = f"http://127.0.0.1:{config.PORT}/"
    for _ in range(20):                           # let the daemon bind its port first
        try:
            urllib.request.urlopen(url + "api/state", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    st = store.read()
    done = {} if reopen else dict(st.get("dashboard_panes") or {})
    wss = [(st.get("coordinator") or {}).get("workspace_id")] + \
          [t["workspace_id"] for t in st["threads"].values()]
    for ws in filter(None, wss):
        if ws not in done:
            pane = cmux.open_dashboard(ws, url)
            if pane:
                done[ws] = pane
    with store.update() as st:
        st["dashboard_panes"] = done


def down(store, cmux):
    pidf = store.dir / "daemon.pid"
    if pidf.exists():
        try:
            os.kill(int(pidf.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass
        pidf.unlink()
    try:
        cmux.stop_server()
    except Exception as e:  # noqa: BLE001
        print(e)


def main(argv=sys.argv[1:]):
    store, cmux = Store(), get_backend()
    cmd = argv[0] if argv else "status"
    if cmd == "up":
        up(store, cmux)
    elif cmd == "dashboard":
        dashboard(store, cmux, reopen="--reopen" in argv)
    elif cmd == "down":
        down(store, cmux)
    elif cmd == "status":
        print(json.dumps(store.read(), indent=1))
    elif cmd == "focus":
        st = store.read()
        ws = st["coordinator"]["workspace_id"] if argv[1] == "coordinator" else st["threads"][argv[1]]["workspace_id"]
        cmux.focus_workspace(ws)
    elif cmd == "thread":
        print(agents.start_thread(store, cmux, argv[1], argv[2]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
