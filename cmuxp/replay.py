"""Replay coordinator test cases: `python3 -m cmuxp.replay [cases.json] [--only id,id] [--runs N]`.

Each case seeds a throwaway project with threads, types the user's turns into a
headless coordinator (same prompt and tools as the real one, CMUXP_DRYRUN so
start_thread launches nothing), then checks what it did against `expect`.
The cases (tests/coordinator_cases.json) are synthetic scenarios for one fictional project.
Writes tests/replay-report.md and prints a summary.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import agents

ROOT = Path(__file__).resolve().parent.parent
TOOL_NAMES = ("start_thread", "message_thread", "list_threads", "read_thread", "resolve_thread")


def seed(home, project, threads):
    os.environ["CMUXP_HOME"] = str(home)
    from . import config
    config.HOME = Path(home)
    from .store import Store
    store = Store(project)
    with store.update() as st:
        for t in threads or []:
            tid = f"t{st['next_id']}"
            st["next_id"] += 1
            st["threads"][tid] = {
                "id": tid, "title": t["title"], "brief": t.get("brief", ""), "created_at": time.time(),
                "workspace_id": None, "terminal_id": None, "status": t.get("status", ""),
                "waiting": t.get("waiting"), "resolved": t.get("resolved", False),
                "last_reply": t.get("last_reply"), "last_reply_at": None,
                "agent_state": t.get("agent_state", "Idle"),
            }
        st["coordinator"] = {"workspace_id": None, "terminal_id": None}
    return store


def launch_files(store):
    from . import agents
    agents._write_launcher(store, "coordinator", "coordinator", "Project chat", None)
    adir = store.dir / "agents"
    mcp = json.loads((adir / "coordinator.mcp.json").read_text())
    mcp["mcpServers"]["cmuxp"]["env"].update(CMUXP_DRYRUN="1", CMUXP_HOME=str(store.dir.parent.parent))
    (adir / "coordinator.mcp.json").write_text(json.dumps(mcp))
    return adir


def run_turn(adir, workdir, text, session=None, model="sonnet"):
    argv = ["claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
            "--append-system-prompt-file", str(adir / "coordinator.prompt.md"),
            "--mcp-config", str(adir / "coordinator.mcp.json"), "--strict-mcp-config",
            "--disallowedTools", *agents.COORDINATOR_DENY,
            "--allowedTools", "mcp__cmuxp",
            f"Edit(/{adir.parent / 'memory'}/**)"]
    if session:
        argv += ["--resume", session]
    env = dict(os.environ, ENABLE_TOOL_SEARCH="false")
    # cmuxp's signed-in Claude config, never the user's own ~/.claude.
    from . import config
    env["CLAUDE_CONFIG_DIR"] = os.environ.get("CMUXP_CLAUDE_CONFIG", str(config.CLAUDE_CONFIG))
    # stdin, not a trailing argument: --allowedTools is variadic and swallows a trailing prompt.
    r = subprocess.run(argv, input=text, cwd=workdir, env=env, capture_output=True, text=True, timeout=300)
    calls, final, sid = [], "", session
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        sid = e.get("session_id") or sid
        if e.get("type") == "assistant":
            for p in e["message"].get("content", []):
                if p.get("type") == "tool_use":
                    calls.append({"name": p["name"].split("__")[-1], "input": p.get("input", {})})
        if e.get("type") == "result":
            final = e.get("result") or ""
    if r.returncode and not final:
        final = f"(claude exited {r.returncode}: {r.stderr.strip()[-300:]})"
    return calls, final, sid


def check(case, store, turns):
    ex, fails = case["expect"], []
    st = store.read()
    title = {tid: t["title"] for tid, t in st["threads"].items()}
    calls = [c for t in turns for c in t["calls"]]
    n = lambda name: sum(c["name"] == name for c in calls)
    for name in ("start_thread", "message_thread"):
        if name in ex and n(name) != ex[name]:
            fails.append(f"{name} called {n(name)}x, expected {ex[name]}")
    if ex.get("list_threads") and not n("list_threads"):
        fails.append("never called list_threads")
    relays = [c["input"] for c in calls if c["name"] == "message_thread"]
    briefs = [c["input"].get("brief", "") for c in calls if c["name"] == "start_thread"]
    if "to" in ex and relays and not any(title.get(r.get("thread_id")) == ex["to"] for r in relays):
        fails.append(f"forwarded to {[title.get(r.get('thread_id'), r.get('thread_id')) for r in relays]}, expected {ex['to']}")
    if "relay_quotes" in ex and relays and not any(ex["relay_quotes"].lower() in r.get("text", "").lower() for r in relays):
        fails.append(f"forward doesn't quote {ex['relay_quotes']!r}")
    if "brief_quotes" in ex and briefs and not any(ex["brief_quotes"].lower() in b.lower() for b in briefs):
        fails.append(f"brief doesn't quote {ex['brief_quotes']!r}")
    for word in ex.get("brief_lacks", []):
        if any(word.lower() in b.lower() for b in briefs):
            fails.append(f"brief adds {word!r}")
    if ex.get("brief_verbatim"):                 # one thread per turn, brief = the user's words
        for t in turns:
            for c in (c for c in t["calls"] if c["name"] == "start_thread"):
                brief, said = " ".join(c["input"].get("brief", "").split()), " ".join(t["user"].split())
                if said.lower() not in brief.lower():
                    fails.append(f"brief isn't the user's words verbatim: {brief[:120]!r}")
                elif len(brief) - len(said) > 150:
                    fails.append(f"brief adds {len(brief) - len(said)} characters to the user's words")
    if ex.get("reopen"):
        names = [c["name"] for c in calls]
        ok = any(c["name"] == "resolve_thread" and c["input"].get("resolved") is False for c in calls)
        if not ok or (names.index("resolve_thread") > names.index("message_thread") if "message_thread" in names else False):
            fails.append("didn't reopen the resolved thread before forwarding")
    for i, t in enumerate(turns):
        text = t["final"].strip()
        if ex.get("silent_last") and i == len(turns) - 1 and text not in ("·", ""):
            fails.append(f"spoke on the last turn when it should be silent: {text[:80]!r}")
        lines = [l for l in text.splitlines() if l.strip()]
        if ex.get("silent") and text not in ("·", ""):
            fails.append(f"spoke when it should be silent: {text[:80]!r}")
        if "max_lines" in ex and len(lines) > ex["max_lines"]:
            fails.append(f"{len(lines)} lines, max {ex['max_lines']}")
        if "—" in text:
            fails.append("uses an em-dash")
        if re.search(r"\bt\d+\b", text):
            fails.append("shows a thread id")
        if any(name in text for name in TOOL_NAMES):
            fails.append("names a tool")
    for m in ex.get("mentions", []):
        if not any(m.lower() in t["final"].lower() for t in turns):
            fails.append(f"doesn't mention {m!r}")
    return fails


def run_case(case, model):
    with tempfile.TemporaryDirectory(prefix="cmuxp-replay-") as home:
        project = f"replay-{case['id']}"
        store = seed(home, project, case.get("threads"))
        adir = launch_files(store)
        turns, sid = [], None
        for text in case["turns"]:
            calls, final, sid = run_turn(adir, store.files, text, sid, model)
            turns.append({"user": text, "calls": calls, "final": final})
        fails = check(case, store, turns)
    return {"id": case["id"], "about": case.get("about", ""), "turns": turns, "fails": fails}


def report(results, path):
    out = ["# Coordinator replay", "",
           f"{sum(not r['fails'] for r in results)}/{len(results)} runs passed.", ""]
    for r in results:
        out += [f"## {'PASS' if not r['fails'] else 'FAIL'} · {r['id']} ({r['about']})", ""]
        out += [f"- {f}" for f in r["fails"]] + ([""] if r["fails"] else [])
        for t in r["turns"]:
            out.append(f"**User:** {t['user'][:300]}")
            out.append("")
            for c in t["calls"]:
                out.append(f"- `{c['name']}` {json.dumps(c['input'], ensure_ascii=False)[:600]}")
            out += ["", "**Coordinator:**", "", "> " + t["final"].strip().replace("\n", "\n> "), ""]
    Path(path).write_text("\n".join(out))


def main():
    args = sys.argv[1:]
    cases_path = next((a for a in args if a.endswith(".json")), str(ROOT / "tests" / "coordinator_cases.json"))
    only = args[args.index("--only") + 1].split(",") if "--only" in args else None
    runs = int(args[args.index("--runs") + 1]) if "--runs" in args else 1
    model = args[args.index("--model") + 1] if "--model" in args else "sonnet"
    cases = [c for c in json.loads(Path(cases_path).read_text()) if not only or c["id"] in only]
    results = []
    for case in cases:
        for _ in range(runs):
            r = run_case(case, model)
            results.append(r)
            print(("PASS " if not r["fails"] else "FAIL ") + r["id"] + "".join(f"\n    {f}" for f in r["fails"]), flush=True)
    report(results, ROOT / "tests" / "replay-report.md")
    print(f"{sum(not r['fails'] for r in results)}/{len(results)} passed; report in tests/replay-report.md")


if __name__ == "__main__":
    main()
