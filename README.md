# cmux-projects

A small project manager for teams of Claude Code agents, built on [cmux](https://cmux.com). The command and Python package are called `cmuxp`. One **coordinator** Claude takes your messages and starts **thread** Claudes, each in its own cmux workspace, working on one task. A local dashboard shows which threads are working, waiting on you, idle or resolved, and clicking one switches cmux to it.

The workflow is inspired by Claude's Projects (a project chat plus focused threads). This is an independent, experimental project and is not affiliated with Anthropic or with cmux.

> Status: experimental. The cmux-tui backend is the tested path; the cmux.app backend works but depends on cmux CLI features that may change.

## How it works

| Piece | File | What it does |
| --- | --- | --- |
| Sandboxed cmux | `bin/sandbox-env.sh` | Runs `cmux` (the cross-platform **cmux-tui** build, `npm i cmux`) with a scrubbed environment and its own `CLAUDE_CONFIG_DIR`. One cmux session per project. |
| Coordinator | `prompts/coordinator.md`, `cmuxp/mcp_server.py` | An interactive `claude` in the project's first workspace, with an appended system prompt and an MCP server: `start_thread`, `message_thread`, `list_threads`, `read_thread`, `resolve_thread`. It routes work and keeps project memory; it can't run shell commands. |
| Threads | `prompts/thread.md`, `cmuxp/agents.py` | `start_thread` creates a cmux workspace, launches a Claude in it, and queues the brief. Thread tools: `reply`, `update_status`, `ask_user`, `message_coordinator`, `request_thread`. |
| Daemon + dashboard | `cmuxp/daemon.py`, `cmuxp/web/index.html` | Polls cmux every second, derives each thread's state, delivers queued messages into idle Claudes, and serves the dashboard on `:8765`. |
| Click to focus | `cmuxp/backend.py`, `cmuxp view` | Clicking a row calls `POST /api/focus`, which moves cmux to that thread's workspace. |
| CLI | `bin/cmuxp setup / login / up / down / status / focus / view / thread` | `up` starts the cmux server, installs hooks into the sandboxed Claude config, launches the coordinator, and starts the daemon. |
| Coordinator tests | `cmuxp/replay.py`, `tests/coordinator_cases.json` | Replays scripted user turns into a headless coordinator and checks what it did. |

State lives in `$CMUXP_HOME/projects/<project>/` (default `~/.cmuxp-sandbox`): `state.json` (threads), `timeline.jsonl`, `queue.jsonl`, `files/` (the shared folder and every Claude's working directory), `memory/`, and `agents/` (generated launchers, prompts and MCP configs).

### How the pieces talk

- **Thread state** comes from cmux's own agent hooks. `cmux agent hook install claude`, run with the sandboxed `CLAUDE_CONFIG_DIR`, writes hooks into the sandbox `settings.json` only. The dashboard state is: resolved, then blocked (an `ask_user` question or a permission prompt), then working, then idle.
- **Messages** (coordinator to thread, thread to coordinator, you to a thread from the dashboard) go into `queue.jsonl`. The daemon delivers them when the target Claude is idle, wrapped in `<cmuxp-message from=… kind=…>` so the receiver knows they come from a colleague.
- **Waiting on you**: `ask_user` sets the thread's question. It clears when the Claude goes idle and then starts working again, meaning you answered, either in the thread's terminal or from the dashboard's message box.
- **Thread replies** also reach the coordinator as `kind="thread-reply"` notes, so it can keep memory current and follow through on work that waits on a result.

## Requirements

macOS or Linux, Node.js, Python 3 (standard library only) and [Claude Code](https://claude.com/claude-code). Nothing touches an installed `cmux.app` or your own `~/.claude`: the sandbox lives in `~/.cmuxp-sandbox` with its own cmux-tui install, Claude config and login.

The sandbox keeps configuration apart; it is not a security sandbox. Agents run as your user, with your file and network access, limited only by Claude Code's own permission settings. `sandbox-env.sh` starts cmux with an empty environment plus a short allowlist (HOME, PATH, proxy settings and optional Claude credentials), so your other environment variables, such as API keys, don't reach the agents.

## Quick start

```bash
git clone https://github.com/will-ness-ai/cmux-projects.git && cd cmux-projects
bin/cmuxp setup     # installs cmux-tui into ~/.cmuxp-sandbox (once)
bin/cmuxp login     # sign the sandboxed Claude in, then /exit (once)
bin/cmuxp up        # cmux server + coordinator + dashboard at http://127.0.0.1:8765/
bin/cmuxp view      # in its own terminal window: the cmux view that follows dashboard clicks
bin/cmuxp down      # stop everything
```

Type to the coordinator in the "project chat" workspace of the view.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CMUXP_PROJECT` | `demo` | Project name; each project has its own state and cmux session. |
| `CMUXP_PORT` | `8765` | Dashboard port. |
| `CMUXP_MODEL` | `sonnet` | Model for every Claude. |
| `CMUXP_HOME` | `~/.cmuxp-sandbox` | Where state, the sandboxed Claude config and cmux-tui live. |
| `CMUXP_REPO` | unset | A git repo (URL, `owner/name`, or local path). The coordinator gets a clone; each thread gets its own worktree on branch `cmuxp/<thread>` and may push that branch and open a draft PR. Merging stays with you. |
| `CMUXP_BACKEND` | `tui` | `tui` for sandboxed cmux-tui, `app` for the cmux macOS app. |

## Using the cmux macOS app (`CMUXP_BACKEND=app`)

`CmuxApp` in `cmuxp/backend.py` drives the macOS app through its CLI. Use a tagged debug build of cmux.app so it has its own bundle id and socket (`/tmp/cmux-debug-<tag>.sock`) and never touches your installed app.

| cmuxp call | cmux-tui | cmux.app |
| --- | --- | --- |
| project | one cmux-tui session | a workspace group |
| create thread | `workspace create` + type the launcher | `rpc workspace.create {title, cwd, initial_input, group_id}` |
| focus | `workspace focus` + viewer re-attach | `rpc workspace.select`, then bring the app forward |
| deliver message | paste when idle | colleague notes via `cmux agent message`; your messages are typed |
| agent state | `agent list` | `rpc mobile.workspace.list` → `terminals[].agent_state` |

Run it from a terminal inside that app, because its socket only accepts cmux's own processes by default:

```bash
export CMUXP_BACKEND=app CMUXP_CMUX=<tagged cmux CLI> CMUXP_APP_SOCKET=/tmp/cmux-debug-<tag>.sock CMUXP_APP_PATH=<tagged app path>
bin/cmuxp up
```

In the app, the dashboard opens as a browser split beside every thread.

## Coordinator tests

```bash
PYTHONPATH=. python3 -m cmuxp.replay [--runs N] [--only id,id] [--model sonnet]
```

Each case in `tests/coordinator_cases.json` seeds a throwaway project with threads, types the user's turns into a headless coordinator with the real prompt and tools (`CMUXP_DRYRUN`, so nothing launches), and checks the tool calls and reply against `expect`. The cases are synthetic scenarios for a fictional recipe app. The report goes to `tests/replay-report.md`. It uses the sandboxed Claude login (`~/.cmuxp-sandbox/claude-config`, or `CMUXP_CLAUDE_CONFIG`).

## Design notes

1. **cmux-tui hands new terminals the calling client's environment**, so every cmux call goes through `sandbox-env.sh`; otherwise the caller's variables leak into the agents.
2. **Focus is per client in cmux-tui.** `workspace focus` only moves the default a fresh attach opens on, so `cmuxp view` attaches in a loop and the dashboard detaches viewers after moving focus.
3. **`claude` swallows a trailing prompt after variadic flags** (`--add-dir`, `--allowedTools`), so briefs go through the delivery queue and replay turns go on stdin.
4. Claude Code's first-run screens are skipped by seeding the sandbox `.claude.json` (onboarding and folder trust for the project's directories).
5. On macOS the daemon runs `caffeinate -i` so idle sleep doesn't stop agents mid-turn.

## Known gaps

- On cmux-tui, delivery types into the target's prompt, so a message landing while you're typing in that Claude's input collides with it.
- The daemon polls once a second; cmux's event stream could replace polling.
- No PR watching, and no Stop hook that enforces replies yet.

## License

MIT. See [LICENSE](LICENSE).
