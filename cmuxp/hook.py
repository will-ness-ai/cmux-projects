"""Claude Code hooks for every cmuxp Claude: `python3 -m cmuxp.hook prompt|stop`.

The user can type straight into a thread's terminal. Projects shows every
message in a thread, so these hooks put that conversation on the record:
UserPromptSubmit logs the user's words (and reopens a resolved thread),
Stop logs the answer the thread gave in its terminal. Hooks must never
block Claude, so every failure is swallowed and the exit code is always 0.
"""
import json
import os
import sys
import time

from .store import Store

AGENT_ID = os.environ.get("CMUXP_AGENT_ID", "")
ROLE = os.environ.get("CMUXP_ROLE", "")


def last_assistant_text(transcript_path):
    """The text of the last assistant message in a Claude Code transcript."""
    text = ""
    with open(transcript_path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "assistant":
                continue
            parts = (e.get("message") or {}).get("content") or []
            t = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
            if t.strip():
                text = t.strip()
    return text


def on_prompt(store, ev):
    prompt = (ev.get("prompt") or "").strip()
    # Colleague messages are not the user talking: pasted as-is on cmux-tui, and
    # wrapped in a <task-notification> by cmux.app's hook delivery.
    if not prompt or "<cmuxp-message" in prompt:
        return
    thread = None if ROLE == "coordinator" else AGENT_ID
    with store.update() as st:
        if thread and thread in st["threads"]:
            t = st["threads"][thread]
            t["user_turn_at"] = time.time()
            if t.get("resolved"):
                t["resolved"] = False             # new work in a finished thread reopens it
                reopened = True
            else:
                reopened = False
        else:
            reopened = False
            if st.get("coordinator"):
                st["coordinator"]["user_turn_at"] = time.time()
    store.log("user", thread=thread, author="user", text=prompt)
    if reopened:
        store.log("reopened", thread=thread, author="user")
    # The coordinator isn't woken for this (a turn per message, and it raced the thread's own request_thread); it sees it through list_threads/read_thread.


def on_stop(store, ev):
    st = store.read()
    rec = st.get("coordinator") if ROLE == "coordinator" else st["threads"].get(AGENT_ID)
    if not rec or not rec.get("user_turn_at"):
        return                                    # only mirror turns the user started
    text = ""
    if ev.get("transcript_path"):
        text = last_assistant_text(ev["transcript_path"])
    with store.update() as st:
        rec = st.get("coordinator") if ROLE == "coordinator" else st["threads"].get(AGENT_ID)
        started = rec.pop("user_turn_at", None)
        if text and ROLE != "coordinator" and not (rec.get("last_reply_at") or 0) > (started or 0):
            rec.update(last_reply=text, last_reply_at=time.time())
    if text:
        store.log("answer", thread=None if ROLE == "coordinator" else AGENT_ID,
                  author=ROLE if ROLE == "coordinator" else AGENT_ID, text=text)


def main():
    try:
        ev = json.loads(sys.stdin.read() or "{}")
        store = Store()
        {"prompt": on_prompt, "stop": on_stop}[sys.argv[1]](store, ev)
    except Exception as e:  # noqa: BLE001
        print(f"cmuxp hook: {e}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
