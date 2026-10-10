"""MCP server on stdio for an agent. It reads and writes the same .archplan
files as the editor server, so the human and the agent talk through comment
threads anchored to model items and plan tasks."""

from __future__ import annotations

import json
import sys

from . import __version__, hotspots
from .model import ANCHORS, EDIT_OPS, dumps, find_items, get_item, violations
from .project import Project, ProjectError, counts, read_json, write_json
from .store import Store, find, mentions, stamp, needs, wait, who_next
from .suggestions import Suggestions, propose

DECISION = {"type": "boolean", "default": False,
            "description": "True when you are blocked until the human chooses. The editor then shows the "
                           "thread under Decisions. Leave false for reports, findings and answers that need no choice."}
MENTIONS = ("To point at a model item in the text, write @[Label](anchor), e.g. @[Orders](comp:shop.orders); "
            "the editor shows it as a link. Messages from the human use the same form.")
OPTIONS = {"type": "array", "items": {"type": "string"},
           "description": "With needs_decision: short choices the human can click, e.g. [\"Do it\", \"Not now\"]. "
                          "They can still answer in free text."}

TOOLS = [
    {"name": "status",
     "description": "Overview of the archplan project: editor URL, model size, plan size, and threads waiting on "
                    "you or on the human. Call it first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_threads",
     "description": "List comment threads between the human architect and you. Each has an anchor, "
                    "a status and waiting_on (agent, human or nobody). Start with waiting_on=agent. " + ANCHORS,
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["open", "resolved", "all"], "default": "open"},
         "waiting_on": {"type": "string", "enum": ["agent", "human", "any"], "default": "any"}}}},
    {"name": "read_thread",
     "description": "Read one thread with all its messages, plus the model item or plan task it is anchored to. "
                    "When the human started it from a graph, snapshot says what they were looking at. "
                    "Mentions appear as @Label (anchor).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "reply",
     "description": "Reply in a thread. Set resolve=true when the question is settled, for example after "
                    "you implemented what was asked. The human sees the reply in the editor at once. " + MENTIONS,
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "text": {"type": "string", "description": "Markdown"},
         "resolve": {"type": "boolean", "default": False},
         "needs_decision": DECISION, "options": OPTIONS}, "required": ["id", "text"]}},
    {"name": "open_thread",
     "description": "Start a thread on a model item or plan task, to report a finding or ask the human. "
                    + ANCHORS + " " + MENTIONS,
     "inputSchema": {"type": "object", "properties": {
         "anchor": {"type": "string"}, "text": {"type": "string", "description": "Markdown"},
         "needs_decision": DECISION, "options": OPTIONS}, "required": ["anchor", "text"]}},
    {"name": "get_plan",
     "description": "The current change plan as the editor computed it: intent, open questions and tasks. "
                    "Each task in the JSON form has a key; use task:<key> as an anchor. The editor recomputes "
                    "the plan while it is open in a browser.",
     "inputSchema": {"type": "object", "properties": {
         "format": {"type": "string", "enum": ["md", "json"], "default": "md"}}}},
    {"name": "get_item",
     "description": "The target model's entry for an anchor: a type, component (with its glossary: owned "
                    "concepts, uses, avoid and leaks), dependency, message, glossary concept or leak. " + ANCHORS,
     "inputSchema": {"type": "object", "properties": {"anchor": {"type": "string"}}, "required": ["anchor"]}},
    {"name": "find",
     "description": "Search components and types in the target model by id, name or path; returns anchors.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "kind": {"type": "string", "enum": ["any", "component", "type"], "default": "any"}}, "required": ["query"]}},
    {"name": "edit_target",
     "description": "Propose changes to the target architecture. This does not change the target: it saves the ops "
                    "as a pending suggestion. The human previews it in the editor and accepts all ops, some of them, "
                    "or none. You learn the outcome from the archplan monitor (`archplan watch` prints a line) or "
                    "status. Use it to model a refactor before implementing it, when the human asks for it. Every op "
                    "must apply to the current target, or nothing is saved. Pass thread to link the suggestion to "
                    "the thread it answers; a short note there points the human to it. Ops: "
                    + "; ".join(f"{k} {v}" for k, v in EDIT_OPS.items()),
     "inputSchema": {"type": "object", "properties": {
         "ops": {"type": "array", "items": {"type": "object", "properties": {"op": {"type": "string", "enum": list(EDIT_OPS)}},
                                            "required": ["op"]}},
         "note": {"type": "string", "description": "One or two sentences: what the change does and why."},
         "thread": {"type": "string", "description": "Id of the thread this suggestion answers (optional)."}},
         "required": ["ops"]}},
    {"name": "get_hotspots",
     "description": "Hotspots and coupling from git history: the components that change most, and pairs that change "
                    "in the same commits. hidden_coupling lists pairs with no dependency in the model; they often "
                    "share a concept, a data format or a duplicated rule. Use it to find coupling worth discussing "
                    "with the human, then open_thread on a group:comp:<a>|comp:<b> anchor with your finding. "
                    "degree is together / the commits of the less active component.",
     "inputSchema": {"type": "object", "properties": {
         "since_days": {"type": "integer", "default": 365},
         "limit": {"type": "integer", "default": 20, "maximum": 100}}}},
    {"name": "refresh_model",
     "description": "Re-export the as-is model from the code with the project's export command. Call it after you "
                    "change the code, so the editor shows what is left of the plan. Can take a minute.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "wait_for_comments",
     "description": "Block until the human adds or changes a thread, then return the threads waiting on you. "
                    "Use it to stay in the conversation while the human works in the editor.",
     "inputSchema": {"type": "object", "properties": {
         "timeout_seconds": {"type": "integer", "default": 120, "maximum": 600}}}},
]

INSTRUCTIONS = (
    "archplan connects you with a human architect who edits the target architecture of this repository in a "
    "browser editor and comments on components, types, dependencies, calls and plan tasks. Check "
    "list_threads(waiting_on=agent) before and after work, and reply in the thread you acted on. Mark a reply "
    "needs_decision=true (with options) only when you cannot continue without the human's choice; everything else "
    "is feedback. After changing code, call refresh_model so the editor shows the remaining plan. Use edit_target "
    "to propose a refactor of the target when asked, instead of describing it in prose; the human accepts or "
    "rejects it in the editor. Mention model items in messages as @[Label](anchor).")


class Agent:
    def __init__(self, p: Project):
        self.p = p
        self.store = Store(p.dir)

    def summary(self, th: dict) -> dict:
        last = th["messages"][-1]
        return {"id": th["id"], "anchor": th["anchor"], "label": th["label"], "status": th["status"],
                "waiting_on": who_next(th), "human_needs": needs(th), "messages": len(th["messages"]),
                "last": {"author": last["author"], "text": mentions(last["text"])}}

    def status(self):
        p = self.p
        srv = read_json(p.path("server.json"))
        model, target = read_json(p.model_path), read_json(p.target_path)
        plan = read_json(p.path("plan.json")) or {}
        ths = [t for t in self.store.read()["threads"] if t["status"] == "open"]
        sgs = Suggestions(p.dir).newest()
        return {"project": p.name, "root": str(p.root),
                "editor": srv["url"] if srv else "not running (run `archplan up`)",
                "model": counts(model) if model else "none yet (call refresh_model)",
                "target": {"edited": bool(target), "violations": len(violations(target)) if target else 0},
                "export_approved": p.trusted(),
                "intent": p.state().get("intent", ""),
                "plan_tasks": len(plan.get("tasks", [])),
                "suggestions": {"pending": sum(1 for x in sgs if x["status"] == "pending"),
                                "recent": [{k: x[k] for k in ("id", "status", "note", "accepted")} | {"ops": len(x["ops"])}
                                           for x in sgs[:5]]},
                "threads": {"waiting_on_agent": [self.summary(t) for t in ths if who_next(t) == "agent"],
                            "waiting_on_human": sum(1 for t in ths if who_next(t) == "human")}}

    def list_threads(self, status="open", waiting_on="any"):
        return [self.summary(th) for th in self.store.read()["threads"]
                if (status == "all" or th["status"] == status) and (waiting_on == "any" or who_next(th) == waiting_on)]

    def read_thread(self, id):
        th = dict(find(self.store.read(), id))
        th["messages"] = [{**m, "text": mentions(m["text"])} for m in th["messages"]]
        if "snapshot" in th:
            th["snapshot"] = snapshot_text(th["snapshot"])
        return {**th, "waiting_on": who_next(th), "item": get_item(self.p, th["anchor"])}

    def reply(self, id, text, resolve=False, needs_decision=False, options=None):
        th = self.store.reply(id, text, "agent", "resolved" if resolve else None,
                              "decision" if needs_decision else "feedback", options)
        return self.summary(th)

    def open_thread(self, anchor, text, needs_decision=False, options=None):
        return self.summary(self.store.create(anchor, anchor, text, "agent",
                                              "decision" if needs_decision else "feedback", options))

    def get_plan(self, format="md"):
        f = self.p.path("plan.json" if format == "json" else "plan.md")
        if not f.exists():
            return "No plan yet: the editor writes one once the target model differs from the code."
        return f.read_text()

    def get_item(self, anchor):
        return get_item(self.p, anchor)

    def find(self, query, kind="any"):
        return find_items(self.p, query, kind)

    def edit_target(self, ops, note="", thread=None):
        if thread:
            find(self.store.read(), thread)  # fail before saving when the thread does not exist
        out = propose(self.p, ops, note, thread)
        if thread:
            self.store.reply(thread, f"I proposed {len(ops)} change(s) to the target ({out['suggestion']})"
                             + (f": {note}" if note else ".") + " Review them in the editor.", "agent")
        return {**out, "message": "The human reviews this in the editor and accepts all, some or none of the ops. "
                                  "The target is unchanged until then. `archplan watch` reports the outcome."}

    def get_hotspots(self, since_days=365, limit=20):
        return hotspots.summary(hotspots.hotspots(self.p, int(since_days)), min(int(limit), 100))

    def refresh_model(self):
        return {"counts": self.p.export(require_trust=True), "note": "The editor reloads the as-is model and rebases the target."}

    def wait_for_comments(self, timeout_seconds=120):
        v = self.store.version()
        wait(self.store.version, v, min(int(timeout_seconds), 600))
        return self.list_threads("open", "agent")


def snapshot_text(snap) -> str:
    """A thread's graph snapshot (opaque JSON from the editor) as one line."""
    if not isinstance(snap, dict):
        return "snapshot: (unreadable)"

    def names(xs):
        out = []
        for x in xs if isinstance(xs, list) else []:
            if isinstance(x, dict):
                x = x.get("label") or x.get("name") or x.get("id") or (
                    f"{x['from']} -> {x['to']}" if "from" in x and "to" in x else None)
            if x:
                out.append(str(x))
        return out
    parts = []
    if snap.get("view"):
        parts.append(f"{snap['view']} view")
    if snap.get("mode"):
        parts.append(str(snap["mode"]))
    if snap.get("focus"):
        parts.append(f"focus: {', '.join(names(snap['focus'])) if isinstance(snap['focus'], list) else snap['focus']}")
    flt = snap.get("filters")
    if isinstance(flt, dict):
        on = [k if v is True else f"{k}={v}" for k, v in flt.items() if v not in (None, False, "", [], {})]
        if on:
            parts.append("filters: " + ", ".join(on))
    elif flt:
        parts.append(f"filters: {flt}")
    for key in ("nodes", "edges"):
        xs = names(snap.get(key))
        if xs:
            more = f" and {len(xs) - 30} more" if len(xs) > 30 else ""
            parts.append(f"{key} ({len(xs)}): " + ", ".join(xs[:30]) + more)
    return "snapshot: " + ("; ".join(parts) or "empty")


ACTIVITY = {
    "status": "Checking the project status",
    "list_threads": "Reading the threads",
    "read_thread": "Reading thread {id}",
    "reply": "Replied in thread {id}",
    "open_thread": "Opened a thread on {anchor}",
    "get_plan": "Reading the plan",
    "get_item": "Looking at {anchor}",
    "find": "Searching the model for {query}",
    "edit_target": "Proposed {n} change(s)",
    "refresh_model": "Re-exporting the model",
    "wait_for_comments": "Waiting for comments",
    "get_hotspots": "Analysing git history for hotspots",
}
DONE = {"refresh_model": "Re-exported the model"}  # long calls: a second entry when they finish
AFTER = ("reply", "open_thread", "edit_target")  # recorded only when they succeed


def record(p: Project, store: Store, tool: str, args: dict, done: bool = False) -> None:
    """Write what the agent does now to .archplan/activity.json for the editor."""
    a = {k: str(v)[:80] for k, v in args.items() if isinstance(v, (str, int))}
    a["n"] = len(args.get("ops") or []) if isinstance(args.get("ops"), list) else 0
    try:
        text = (DONE if done else ACTIVITY).get(tool, tool).format_map(a)
    except (KeyError, ValueError):
        text = tool
    act = {"at": stamp(), "tool": tool, "summary": text}
    anchor = args.get("anchor") if isinstance(args.get("anchor"), str) else None
    tid = args.get("id") if tool in ("read_thread", "reply") else args.get("thread")
    if isinstance(tid, str):
        act["thread"] = tid
        try:
            anchor = find(store.read(), tid)["anchor"]
        except KeyError:
            pass
    if anchor and anchor.split(":", 1)[0] in ("task", "step"):
        act["task"] = anchor.split(":", 1)[1].split("#")[0]
    try:
        write_json(p.path("activity.json"), act)
    except OSError:
        pass


def serve(p: Project) -> None:
    agent = Agent(p)
    names = {t["name"] for t in TOOLS}

    def send(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method = req.get("id"), req.get("method")
        if rid is None:
            continue  # a notification
        if method == "initialize":
            result = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "archplan", "version": __version__},
                      "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            prm = req.get("params", {})
            name = prm.get("name", "")
            if name not in names:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}})
                continue
            args = prm.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            if name not in AFTER:
                record(p, agent.store, name, args)
            try:
                out = getattr(agent, name)(**args)
                if name in AFTER or name in DONE:
                    record(p, agent.store, name, args, done=name in DONE)
                result = {"content": [{"type": "text", "text": dumps(out)}]}
            except (KeyError, TypeError, ValueError, ProjectError) as e:
                result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}})
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": result})


def serve_unconfigured(err: str) -> None:
    """Without a project, answer every tool call with how to set one up, so the
    plugin's MCP server does not fail in repositories that do not use archplan."""
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method = req.get("id"), req.get("method")
        if rid is None:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "archplan", "version": __version__},
                "instructions": "This repository has no archplan project yet. The status tool explains how to start one."}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOLS[0]]}})
        elif method == "tools/call":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text":
                f"{err}. Start one with `archplan init` (then `archplan export` and `archplan up`), or use the /archplan skill."}],
                "isError": True}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
