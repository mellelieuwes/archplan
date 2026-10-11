"""Target edits the agent proposes, in .archplan/suggestions.json.

The agent does not change the target directly. It saves its ops as a pending
suggestion. The human previews it in the editor and accepts all ops, some of
them, or none. Accepted ops are applied to the target as it is then.

kind is "edit" (edit_target), "glossary" (propose_glossary) or "refactor"
(propose_refactor, with title, rationale, fixes, verified, effort, risk and
opportunity). Rejecting a refactor needs a reason; the refactor brief lists
it so the agent does not propose the same idea again."""

from __future__ import annotations

import uuid

from .model import edited, save_edited, violations
from .project import Project
from .store import Conflict, JsonStore, NotFound, stamp

STATUSES = ("pending", "accepted", "rejected", "partial")
REASON_MAX = 1000


class Suggestions(JsonStore):
    name = "suggestions"
    empty = {"suggestions": []}

    def create(self, ops: list, note: str = "", thread: str | None = None, kind: str = "edit", **extra) -> dict:
        sg = {"id": "sg-" + uuid.uuid4().hex[:8], "kind": kind, "thread": thread, "note": note or "", "ops": ops,
              "created": stamp(), "status": "pending", "accepted": [], "decided": None, **extra}
        self.update(lambda d: d["suggestions"].append(sg))
        return sg

    def newest(self) -> list:
        """All suggestions, newest first."""
        return self.read()["suggestions"][::-1]

    def get(self, sid: str) -> dict:
        return find(self.read(), sid)

    def decide(self, p: Project, sid: str, indices: list | None, reason: str | None = None) -> dict:
        """Apply the chosen ops (all when indices is None) to the target and
        record the outcome. An empty list rejects the suggestion."""
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string")
        reason = (reason or "").strip()[:REASON_MAX]

        def fn(d):
            sg = find(d, sid)
            if sg["status"] != "pending":
                raise Conflict(f"suggestion {sid} is already {sg['status']}")
            idx = pick(sg, indices)
            if not idx and sg.get("kind") == "refactor" and not reason:
                raise ValueError("rejecting a refactor needs a reason, so the agent does not propose it again")
            if reason:
                sg["reason"] = reason
            if idx:
                m, _ = preview(p, sg, idx)
                save_edited(p, m, "agent-suggestion")
            sg["accepted"] = idx
            sg["status"] = ("rejected" if not idx else "accepted" if len(idx) == len(sg["ops"]) else "partial")
            sg["decided"] = stamp()
            return sg
        return self.update(fn)


def find(d: dict, sid: str) -> dict:
    for sg in d["suggestions"]:
        if sg["id"] == sid:
            return sg
    raise NotFound(f"no suggestion {sid}")


def pick(sg: dict, indices: list | None) -> list:
    """Sorted, unique op indices; all ops when indices is None."""
    n = len(sg["ops"])
    if indices is None:
        return list(range(n))
    idx = sorted({int(i) for i in indices})
    if any(i < 0 or i >= n for i in idx):
        raise ValueError(f"op index out of range 0..{n - 1}")
    return idx


def preview(p: Project, sg: dict, indices: list | None = None) -> tuple:
    """The current target with the chosen ops applied, and one line per op."""
    ops = [sg["ops"][i] for i in pick(sg, indices)]
    try:
        return edited(p, ops)
    except (KeyError, ValueError) as e:
        raise Conflict(f"no longer applies to the target: {e}") from e


def propose(p: Project, ops: list, note: str = "", thread: str | None = None, kind: str = "edit") -> dict:
    """Check ops against the current target, then store them as a pending
    suggestion. Raises ValueError when an op does not apply."""
    m, done = edited(p, ops)
    sg = Suggestions(p.dir).create(ops, note, thread, kind)
    return {"suggestion": sg["id"], "status": "pending", "ops": len(ops), "would_apply": done,
            "violations_after": len(violations(m))}
