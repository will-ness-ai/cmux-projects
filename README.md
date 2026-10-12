# cmux-projects

A small project manager for teams of Claude Code agents, built on the [cmux](https://cmux.com) macOS app. The command and Python package are called `cmuxp`. One **coordinator** Claude takes your messages and starts **thread** Claudes, each in its own cmux workspace, working on one task. A dashboard beside every workspace shows which threads are working, waiting on you, idle or resolved, and clicking one switches cmux to it.

The workflow is inspired by Claude's Projects (a project chat plus focused threads). This is an independent, experimental project and is not affiliated with Anthropic or with cmux.

> Status: experimental. It depends on cmux CLI features (`rpc`, `workspace-group`, `agent message`) that may change.

## How it works

| Piece | File | What it does |
| --- | --- | --- |
| Coordinator | `prompts/coordinator.md`, `cmuxp/mcp_server.py` | An interactive `claude` in the project's first workspace, with an appended system prompt and an MCP server: `start_thread`, `message_thread`, `list_threads`, `read_thread`, `resolve_thread`. It routes work and keeps project memory; it can't run shell commands. |
| Threads | `prompts/thread.md`, `cmuxp/agents.py` | `start_thread` creates a cmux workspace in the project's workspace group, launches a Claude in it, and queues the brief. Thread tools: `reply`, `update_status`, `ask_user`, `message_coordinator`, `request_thread`. |
| cmux backend | `cmuxp/backend.py` | Drives the app through the `cmux` CLI: project = workspace group, thread = workspace. |
| Daemon + dashboard | `cmuxp/daemon.py`, `cmuxp/web/index.html` | Polls cmux every second, derives each thread's state, delivers queued messages, and serves the dashboard on `:8765`. The dashboard opens as a browser split in every project workspace. |
| CLI | `bin/cmuxp login / up / down / status / dashboard / focus / thread` | `up` launches the coordinator, starts the daemon and opens the dashboard panes. |
| Coordinator tests | `cmuxp/replay.py`, `tests/coordinator_cases.json` | Replays scripted user turns into a headless coordinator and checks what it did. |

State lives in `$CMUXP_HOME/projects/<project>/` (default `~/.cmuxp-sandbox`): `state.json` (threads), `timeline.jsonl`, `queue.jsonl`, `files/` (the shared folder and every Claude's working directory), `memory/`, and `agents/` (generated launchers, prompts and MCP configs).

### How the pieces talk

| cmuxp call | cmux app |
| --- | --- |
| project | a workspace group (`workspace-group create`) |
| create thread | `rpc workspace.create {title, cwd, initial_input, group_id}` |
| focus (dashboard click) | `rpc workspace.select` |
| colleague message | `cmux agent message`, delivered through Claude's hooks, so it never types into your prompt |
| your message from the dashboard | typed into the thread with `cmux send --paste` |
| agent state | `rpc mobile.workspace.list` → `terminals[].agent_state` |

- **Thread state** on the dashboard is: resolved, then blocked (an `ask_user` question or a permission prompt), then working, then idle.
- **Messages** go into `queue.jsonl` first, wrapped in `<cmuxp-message from=… kind=…>` so the receiver knows they come from a colleague, and the daemon delivers them.
- **Waiting on you**: `ask_user` sets the thread's question. It clears when the Claude goes idle and then starts working again, meaning you answered, either in the thread's terminal or from the dashboard's message box.
- **Thread replies** also reach the coordinator as `kind="thread-reply"` notes, so it can keep memory current and follow through on work that waits on a result.

## Requirements

macOS with the [cmux app](https://cmux.com), Python 3 (standard library only) and [Claude Code](https://claude.com/claude-code).

cmuxp's Claudes use their own config directory (`~/.cmuxp-sandbox/claude-config`) with their own login, so your `~/.claude` settings, hooks and login are never changed. That keeps configuration apart; it is not a security sandbox. Agents run as your user, with your file and network access, limited only by Claude Code's own permission settings.

## Quick start

Run these from a terminal **inside cmux**, because the app's socket only accepts cmux's own processes by default:

```bash
git clone https://github.com/will-ness-ai/cmux-projects.git && cd cmux-projects
bin/cmuxp login     # sign cmuxp's Claude in, then /exit (once)
bin/cmuxp up        # coordinator workspace + dashboard at http://127.0.0.1:8765/
bin/cmuxp down      # stop the daemon (workspaces stay open)
```

Type to the coordinator in the "project chat" workspace. Threads appear as workspaces in the same group.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CMUXP_PROJECT` | `demo` | Project name; each project has its own state and workspace group. |
| `CMUXP_PORT` | `8765` | Dashboard port. |
| `CMUXP_MODEL` | `sonnet` | Model for every Claude. |
| `CMUXP_HOME` | `~/.cmuxp-sandbox` | Where project state and cmuxp's Claude config live. |
| `CMUXP_REPO` | unset | A git repo (URL, `owner/name`, or local path). The coordinator gets a clone; each thread gets its own worktree on branch `cmuxp/<thread>` and may push that branch and open a draft PR. Merging stays with you. |
| `CMUXP_CMUX` | `cmux` | The cmux CLI to call. |
| `CMUXP_APP_SOCKET` | the CLI's default | Socket of the cmux app to drive. |
| `CMUXP_APP_PATH` | unset | App to bring to the front on dashboard clicks. |

To keep cmuxp away from the cmux you use every day, point it at a tagged debug build of the app, which has its own bundle id and socket: `CMUXP_CMUX=<tagged cmux CLI> CMUXP_APP_SOCKET=/tmp/cmux-debug-<tag>.sock CMUXP_APP_PATH=<tagged app path>`.

## Coordinator tests

```bash
PYTHONPATH=. python3 -m cmuxp.replay [--runs N] [--only id,id] [--model sonnet]
```

Each case in `tests/coordinator_cases.json` seeds a throwaway project with threads, types the user's turns into a headless coordinator with the real prompt and tools (`CMUXP_DRYRUN`, so nothing launches), and checks the tool calls and reply against `expect`. The cases are synthetic scenarios for a fictional recipe app. The report goes to `tests/replay-report.md`. It uses cmuxp's signed-in Claude config (or `CMUXP_CLAUDE_CONFIG`). These tests don't need cmux.

## Design notes

1. **Colleague messages go through `cmux agent message`, your messages are typed.** Claude treats hook-delivered text as feedback rather than as you talking, so answers you give from the dashboard are pasted into the thread instead.
2. **The dashboard is a browser split in every workspace**, because selecting a thread's workspace hides the others; the daemon backfills panes for new threads.
3. **`claude` swallows a trailing prompt after variadic flags** (`--add-dir`, `--allowedTools`), so briefs go through the delivery queue and replay turns go on stdin.
4. Claude Code's first-run screens are skipped by seeding `.claude.json` in cmuxp's config (onboarding and folder trust for the project's directories).
5. The daemon runs `caffeinate -i` so idle sleep doesn't stop agents mid-turn.

## Known gaps

- The daemon polls once a second; cmux's event stream could replace polling.
- No PR watching, and no Stop hook that enforces replies yet.

## License

MIT. See [LICENSE](LICENSE).
