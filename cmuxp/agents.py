"""Launch the coordinator and thread Claudes inside cmux workspaces."""
import json
import os
import shlex
import subprocess

from . import config
from .store import Store

PROMPTS = config.ROOT / "prompts"
COORDINATOR_DENY = ["Bash", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"]


def _claude_config_dir():
    return config.CLAUDE_CONFIG


def trust_dir(path):
    """Pre-accept the folder trust dialog in cmuxp's Claude config."""
    p = _claude_config_dir() / ".claude.json"
    d = json.loads(p.read_text()) if p.exists() else {}
    d.setdefault("hasCompletedOnboarding", True)
    d.setdefault("theme", "dark")
    proj = d.setdefault("projects", {}).setdefault(str(path), {})
    if proj.get("hasTrustDialogAccepted"):
        return  # running Claudes also write this file; don't race them
    proj.update(hasTrustDialogAccepted=True, hasCompletedProjectOnboarding=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1))


def _git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def ensure_repo(store: Store):
    """Clone CMUXP_REPO into files/repo once; returns its path or None."""
    if not config.REPO:
        return None
    dest = store.files / "repo"
    if not (dest / ".git").exists():
        src = config.REPO
        if os.path.isdir(os.path.expanduser(src)):
            _git("clone", os.path.expanduser(src), str(dest))
        elif "://" in src or src.startswith("git@"):
            _git("clone", src, str(dest))
        else:                                    # owner/name: let gh pick the right auth
            r = subprocess.run(["gh", "repo", "clone", src, str(dest)], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"gh repo clone {src}: {(r.stderr or r.stdout).strip()}")
    return dest


def thread_worktree(store: Store, tid):
    """A worktree per thread so parallel threads never edit the same checkout."""
    repo = store.read().get("repo")
    if not repo:
        return None
    wt = store.files / "worktrees" / tid
    if not wt.exists():
        try:
            _git("fetch", "origin", cwd=repo)
        except RuntimeError:
            pass                                 # offline: branch from what we have
        base = "origin/HEAD"
        try:
            _git("rev-parse", "--verify", base, cwd=repo)
        except RuntimeError:
            base = "HEAD"
        _git("worktree", "add", "-b", f"cmuxp/{tid}", str(wt), base, cwd=repo)
    return wt


def _write_launcher(store: Store, agent_id, role, title, initial_prompt, workdir=None):
    adir = store.dir / "agents"
    env = {
        "PYTHONPATH": str(config.ROOT), "CMUXP_ROLE": role, "CMUXP_AGENT_ID": agent_id,
        "CMUXP_PROJECT": store.project, "CMUXP_HOME": str(config.HOME),
        "CMUXP_CMUX": config.CMUX_BIN, "CMUXP_PORT": str(config.PORT),
        "CMUXP_APP_SOCKET": config.APP_SOCKET, "CMUXP_APP_PATH": config.APP_PATH,
    }
    mcp = {"mcpServers": {"cmuxp": {"command": "python3", "args": ["-m", "cmuxp.mcp_server"], "env": env}}}
    (adir / f"{agent_id}.mcp.json").write_text(json.dumps(mcp, indent=1))

    template = (PROMPTS / f"{role}.md").read_text()
    workdir = workdir or store.files
    repo = store.read().get("repo")
    prompt = template.format(project=store.project, agent_id=agent_id, title=title,
                             files=store.files, memory=store.dir / "memory", workdir=workdir,
                             dashboard=f"http://127.0.0.1:{config.PORT}/")
    if repo:
        prompt += "\n\n" + (PROMPTS / f"{role}-repo.md").read_text().format(
            repo=repo, workdir=workdir, agent_id=agent_id, remote=config.REPO)
    (adir / f"{agent_id}.prompt.md").write_text(prompt)
    (adir / f"{agent_id}.initial.md").write_text(initial_prompt or "")

    # Hooks put what the user types in a terminal on the project record (see hook.py).
    hook = lambda ev: [{"hooks": [{"type": "command", "timeout": 10,
                                   "command": f"python3 -m cmuxp.hook {ev}"}]}]
    settings = {"hooks": {"UserPromptSubmit": hook("prompt"), "Stop": hook("stop")}}
    (adir / f"{agent_id}.settings.json").write_text(json.dumps(settings, indent=1))

    argv = ["claude", "--model", config.MODEL, "--name", f"{store.project}: {title}",
            "--settings", str(adir / f"{agent_id}.settings.json"),
            "--append-system-prompt-file", str(adir / f"{agent_id}.prompt.md"),
            "--mcp-config", str(adir / f"{agent_id}.mcp.json"), "--strict-mcp-config",
            "--allowedTools", "mcp__cmuxp", "--add-dir", str(store.dir / "memory")]
    if role == "coordinator":                    # it routes; only threads run commands or browse
        argv[-2:-2] = ["--disallowedTools", *COORDINATOR_DENY]
        mem = store.dir / "memory"                # memory notes save without a prompt; Edit(//abs/**) also covers Write
        argv[argv.index("mcp__cmuxp") + 1:argv.index("mcp__cmuxp") + 1] = [f"Edit(/{mem}/**)"]
    script = "#!/usr/bin/env bash\n" + f"cd {shlex.quote(str(workdir))}\n"
    # Pin cmuxp's own Claude config so nothing touches your ~/.claude.
    script += f"export CLAUDE_CONFIG_DIR={shlex.quote(str(_claude_config_dir()))}\n"
    script += "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())   # for the hooks
    # Load the few cmuxp tool schemas up front; lazy loading made first calls guess argument names.
    script += "export ENABLE_TOOL_SEARCH=false\n"
    script += " ".join(shlex.quote(a) for a in argv)
    if initial_prompt:
        script += f' "$(cat {shlex.quote(str(adir / f"{agent_id}.initial.md"))})"'
    script += "\n"
    path = adir / f"{agent_id}.sh"
    path.write_text(script)
    os.chmod(path, 0o755)
    return path


def launch(store: Store, cmux, agent_id, role, title, initial_prompt, ws_name, group=None, workdir=None):
    trust_dir(workdir or store.files)
    launcher = _write_launcher(store, agent_id, role, title, initial_prompt, workdir)
    return cmux.launch_workspace(ws_name, f"clear; bash {shlex.quote(str(launcher))}", group)


def start_coordinator(store: Store, cmux):
    st = store.read()
    if st.get("coordinator"):
        return st["coordinator"]
    repo = ensure_repo(store)
    if repo:
        with store.update() as st:
            st["repo"] = str(repo)
    ws, term = launch(store, cmux, "coordinator", "coordinator", "Project chat", None,
                      f"{store.project} · project chat", workdir=repo)
    group = cmux.ensure_group(store.project, ws)
    with store.update() as st:
        st["coordinator"] = {"workspace_id": ws, "terminal_id": term}
        st["group"] = group
    return st["coordinator"]


def start_thread(store: Store, cmux, title, brief, by="coordinator"):
    tid = store.new_thread(title, brief, by)
    if os.environ.get("CMUXP_DRYRUN"):           # replay tests: record the thread, launch nothing
        return tid
    initial = (f'<cmuxp-message from="{by}" kind="brief" thread="{tid}">\n'
               f"{brief}\n</cmuxp-message>")
    # The brief goes through the delivery queue rather than as claude's positional
    # prompt: --add-dir/--allowedTools are variadic and swallow a trailing prompt.
    ws, term = launch(store, cmux, tid, "thread", title, None, f"{tid} · {title}",
                      store.read().get("group"), workdir=thread_worktree(store, tid))
    store.enqueue(tid, initial, by)
    try:                                         # dashboard beside the thread (cmux.app)
        if cmux.open_dashboard(ws, f"http://127.0.0.1:{config.PORT}/"):
            with store.update() as st:
                st.setdefault("dashboard_panes", {})[ws] = True
    except Exception:  # noqa: BLE001
        pass
    with store.update() as st:
        st["threads"][tid].update(workspace_id=ws, terminal_id=term)
    return tid
