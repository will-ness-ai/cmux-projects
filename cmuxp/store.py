"""Project state on disk: state.json (threads), timeline.jsonl, queue.jsonl.

Several processes (daemon, one MCP server per Claude) touch these files, so
every read-modify-write holds an flock on <project>/.lock.
"""
import contextlib
import fcntl
import json
import time
import uuid
from pathlib import Path

from . import config


def now():
    return time.time()


class Store:
    def __init__(self, project: str = config.PROJECT):
        self.project = project
        self.dir = config.project_dir(project)
        self.files = self.dir / "files"
        for d in (self.dir, self.files, self.dir / "agents", self.dir / "memory"):
            d.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.timeline_path = self.dir / "timeline.jsonl"
        self.queue_path = self.dir / "queue.jsonl"

    @contextlib.contextmanager
    def locked(self):
        with open(self.dir / ".lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # state ----------------------------------------------------------------
    def _read(self):
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"project": self.project, "coordinator": None, "threads": {}, "next_id": 1}

    def _write(self, st):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(self.state_path)

    def read(self):
        with self.locked():
            return self._read()

    @contextlib.contextmanager
    def update(self):
        with self.locked():
            st = self._read()
            yield st
            self._write(st)

    def new_thread(self, title, brief, by="coordinator"):
        with self.update() as st:
            tid = f"t{st['next_id']}"
            st["next_id"] += 1
            st["threads"][tid] = {
                "id": tid, "title": title, "brief": brief, "created_at": now(),
                "workspace_id": None, "terminal_id": None, "status": "",
                "waiting": None, "resolved": False, "last_reply": None,
                "last_reply_at": None, "agent_state": None,
            }
        self.log("thread_started", thread=tid, author=by, text=title)
        return tid

    # timeline and queue -----------------------------------------------------
    def _append(self, path, rec):
        with self.locked(), open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def log(self, kind, thread=None, author=None, text=""):
        self._append(self.timeline_path, {"ts": now(), "kind": kind, "thread": thread,
                                          "author": author, "text": text})

    def timeline(self, limit=200):
        if not self.timeline_path.exists():
            return []
        lines = self.timeline_path.read_text().splitlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]

    def enqueue(self, to, text, sender):
        self._append(self.queue_path, {"id": uuid.uuid4().hex[:8], "ts": now(), "to": to,
                                       "from": sender, "text": text, "delivered": False})

    def pending(self):
        if not self.queue_path.exists():
            return []
        with self.locked():
            items = [json.loads(l) for l in self.queue_path.read_text().splitlines() if l.strip()]
        return [i for i in items if not i["delivered"]]

    def mark_delivered(self, ids):
        with self.locked():
            items = [json.loads(l) for l in self.queue_path.read_text().splitlines() if l.strip()]
            for i in items:
                if i["id"] in ids:
                    i["delivered"] = True
                    i["delivered_at"] = now()
            self.queue_path.write_text("".join(json.dumps(i) + "\n" for i in items))
