"""Print one line per change an agent should act on, for use as an event stream
(a Claude Code monitor). It reads the .archplan files; no server is needed.

Events:
  comment   a thread whose newest message is from the human (new or updated)
  resolved  a thread the human resolved or reopened
  plan      the plan changed (tasks added or removed)
"""

from __future__ import annotations

import time

from .project import Project, read_json
from .store import Store


def tasks(p: Project):
    plan = read_json(p.path("plan.json"))
    if plan is None:
        return None
    return {t.get("key") or t.get("title"): t.get("title", "") for t in plan.get("tasks", [])}


def watch(p: Project, interval: float = 2, emit=print) -> None:
    store = Store(p.dir)
    seen = {t["id"]: (len(t["messages"]), t["status"]) for t in store.read()["threads"]}
    last_tasks = tasks(p)
    out = lambda s: emit(s, flush=True) if emit is print else emit(s)
    while True:
        time.sleep(interval)
        for t in store.read()["threads"]:
            n, st = len(t["messages"]), t["status"]
            old = seen.get(t["id"])
            seen[t["id"]] = (n, st)
            last = t["messages"][-1]
            if (old is None or n > old[0]) and last["author"] != "agent":
                text = " ".join(last["text"].split())
                out(f"comment {t['id']} on {t['anchor'][:120]}: {text[:300]}")
            elif old and st != old[1] and n == old[0]:  # status alone: the human (the agent resolves with a reply)
                out(f"{'resolved' if st != 'open' else 'reopened'} {t['id']} on {t['anchor'][:120]}")
        cur = tasks(p)
        if cur is not None and last_tasks is not None and cur != last_tasks:
            added = [cur[k] for k in cur if k not in last_tasks]
            removed = [last_tasks[k] for k in last_tasks if k not in cur]
            if added or removed:
                parts = [f"+{x}" for x in added[:5]] + [f"-{x}" for x in removed[:5]]
                out(f"plan {len(cur)} tasks: " + "; ".join(parts)[:400])
        if cur is not None:
            last_tasks = cur
