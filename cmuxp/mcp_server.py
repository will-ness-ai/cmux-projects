"""Stdio MCP server giving each Claude its project tools.

CMUXP_ROLE=coordinator -> start_thread, message_thread, list_threads, read_thread, resolve_thread
CMUXP_ROLE=thread      -> reply, update_status, ask_user, request_thread, message_coordinator
"""
import json
import os
import sys
import time

from . import agents
from .backend import get_backend
from .store import Store

ROLE = os.environ.get("CMUXP_ROLE", "thread")
AGENT_ID = os.environ.get("CMUXP_AGENT_ID", "")
store = Store()
cmux = get_backend(store.project)


def S(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


STR = {"type": "string"}

COORDINATOR_TOOLS = {
    "start_thread": ("Start a new thread: a new cmux workspace running its own Claude, briefed with `brief`. "
                     "Returns the thread id.",
                     S({"title": {**STR, "description": "A few plain words naming the thread"},
                        "brief": {**STR, "description": "The user's message verbatim, plus only a memory fact or a default you picked"}},
                       ["title", "brief"])),
    "message_thread": ("Send a note to a running thread (delivered into its Claude when it is idle). "
                       "Use it to forward the user's follow-ups.",
                       S({"thread_id": STR, "text": STR}, ["thread_id", "text"])),
    "list_threads": ("List threads with their state (working/blocked/idle/resolved), checklist and last reply.",
                     S({})),
    "read_thread": ("Read one thread: brief, checklist, replies, and the tail of its terminal screen.",
                    S({"thread_id": STR}, ["thread_id"])),
    "resolve_thread": ("Mark a thread resolved (or reopen it with resolved=false).",
                       S({"thread_id": STR, "resolved": {"type": "boolean"}}, ["thread_id"])),
}

THREAD_TOOLS = {
    "reply": ("Deliver a result, blocker or answer for this thread. Shown on the project dashboard. "
              "Use for milestones only. Set finished=true only when the user has what they asked for and "
              "owes no next step (no decision, review, merge or answer pending); that resolves the thread.",
              S({"text": STR, "finished": {"type": "boolean"}}, ["text"])),
    "update_status": ("Replace this thread's progress checklist. Line 1 is a header, then one step per line "
                      "prefixed with ✓ (done), ✱ (in progress) or ○ (not started).",
                      S({"text": STR}, ["text"])),
    "ask_user": ("Ask the user a question only they can answer. Marks the thread 'waiting on you' on the "
                 "dashboard until the user answers in this thread's terminal. End your turn after calling it.",
                 S({"question": STR}, ["question"])),
    "request_thread": ("Start a separate thread, when the user asks for one or for "
                       "work outside this thread's job. Pass the user's words verbatim.",
                       S({"title": {**STR, "description": "A few plain words naming the new thread"},
                          "user_words": {**STR, "description": "The user's request, verbatim"},
                          "context": {**STR, "description": "Anything the new thread needs from this one"}},
                         ["title", "user_words"])),
    "message_coordinator": ("Send the coordinator a note the user does not need to see.",
                            S({"text": STR}, ["text"])),
}

TOOLS = COORDINATOR_TOOLS if ROLE == "coordinator" else THREAD_TOOLS


def thread_view(t):
    return {k: t.get(k) for k in ("id", "title", "agent_state", "display_state", "status",
                                  "waiting", "resolved", "last_reply")}


def call(name, a):
    if ROLE == "coordinator":
        if name == "start_thread":
            tid = agents.start_thread(store, cmux, a["title"], a["brief"])
            return f"Started thread {tid} ({a['title']}) in its own cmux workspace."
        if name == "message_thread":
            st = store.read()
            if a["thread_id"] not in st["threads"]:
                raise ValueError(f"no thread {a['thread_id']}")
            text = f'<cmuxp-message from="coordinator" kind="relay">\n{a["text"]}\n</cmuxp-message>'
            store.enqueue(a["thread_id"], text, "coordinator")
            store.log("relay", thread=a["thread_id"], author="coordinator", text=a["text"])
            return "Queued; it is delivered when the thread is idle."
        if name == "list_threads":
            st = store.read()
            return json.dumps([thread_view(t) for t in st["threads"].values()], indent=1)
        if name == "read_thread":
            st = store.read()
            t = st["threads"][a["thread_id"]]
            replies = [e for e in store.timeline() if e["thread"] == t["id"] and e["kind"] == "reply"]
            screen = ""
            try:
                screen = cmux.screen_text(t["terminal_id"])[-2500:]
            except Exception as e:  # noqa: BLE001
                screen = f"(screen unavailable: {e})"
            return json.dumps({**thread_view(t), "brief": t["brief"],
                               "replies": [r["text"] for r in replies[-5:]], "screen_tail": screen}, indent=1)
        if name == "resolve_thread":
            resolved = a.get("resolved", True)
            with store.update() as st:
                st["threads"][a["thread_id"]]["resolved"] = resolved
            store.log("resolved" if resolved else "reopened", thread=a["thread_id"], author="coordinator")
            return "ok"
    else:
        if name == "reply":
            with store.update() as st:
                st["threads"][AGENT_ID].update(last_reply=a["text"], last_reply_at=time.time())
            store.log("reply", thread=AGENT_ID, author=AGENT_ID, text=a["text"])
            # Like Projects, the coordinator observes every thread reply (to keep memory
            # current and follow through), but the thread resolves itself.
            title = store.read()["threads"][AGENT_ID]["title"]
            store.enqueue("coordinator",
                          f'<cmuxp-message from="{AGENT_ID}" kind="thread-reply" thread="{title}" '
                          f'finished="{str(bool(a.get("finished"))).lower()}">\n{a["text"]}\n</cmuxp-message>',
                          AGENT_ID)
            if a.get("finished"):
                with store.update() as st:
                    st["threads"][AGENT_ID]["resolved"] = True
                store.log("resolved", thread=AGENT_ID, author=AGENT_ID)
            return "Posted; thread resolved." if a.get("finished") else "Posted."
        if name == "update_status":
            with store.update() as st:
                st["threads"][AGENT_ID]["status"] = a["text"]
            store.log("status", thread=AGENT_ID, author=AGENT_ID, text=a["text"])
            return "Status updated."
        if name == "ask_user":
            with store.update() as st:
                st["threads"][AGENT_ID]["waiting"] = {"question": a["question"], "ts": time.time()}
            store.log("ask_user", thread=AGENT_ID, author=AGENT_ID, text=a["question"])
            return "The user sees this thread as waiting on them. End your turn now; their answer arrives as your next prompt."
        if name == "request_thread":
            # Started here, not by the coordinator: the brief stays exactly the user's
            # words plus this thread's context, and nothing waits on a coordinator turn.
            src = store.read()["threads"][AGENT_ID]["title"]
            brief = f'The user asked (in "{src}"): "{a["user_words"]}"'
            if a.get("context"):
                brief += f'\n\nContext from "{src}": {a["context"]}'
            tid = agents.start_thread(store, cmux, a["title"], brief, by=AGENT_ID)
            store.log("thread_request", thread=AGENT_ID, author=AGENT_ID, text=f'{tid}: {a["title"]}')
            return f'Started "{a["title"]}". Tell the user in one line that it is starting.'
        if name == "message_coordinator":
            store.enqueue("coordinator",
                          f'<cmuxp-message from="{AGENT_ID}" kind="thread-note">\n{a["text"]}\n</cmuxp-message>',
                          AGENT_ID)
            return "Queued for the coordinator."
    raise ValueError(f"unknown tool {name}")


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cmuxp", "version": "0.1"}}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (d, s) in TOOLS.items()]}
    if method == "tools/call":
        p = msg["params"]
        try:
            text, err = call(p["name"], p.get("arguments") or {}), False
        except Exception as e:  # noqa: BLE001
            text, err = f"error: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": err}
    if method == "ping":
        return {}
    if mid is not None:
        raise LookupError(method)
    return None


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        try:
            result = handle(msg)
            if mid is None:
                continue
            out = {"jsonrpc": "2.0", "id": mid, "result": result}
        except LookupError as e:
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {e}"}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
