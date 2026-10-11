"""Helpers every detector shares: the finding shape, stable ids, component
names and the component dependency graph."""

from __future__ import annotations

import hashlib
import re
from collections import Counter

GROUPS = ["profile", "structure", "layers", "vocabulary", "types", "history", "concepts", "boundaries", "data",
          "evolution"]
STRENGTHS = ["strong", "medium", "weak"]
EXAMPLES = 20            # breaks, files or fields kept as evidence
COMMON_FIELDS = {"id", "uuid", "pk", "inserted_at", "updated_at", "created_at", "modified_at", "deleted_at",
                 "created", "modified", "updated", "created_by", "updated_by", "lock_version", "version"}


def fingerprint(kind: str, *keys) -> str:
    h = hashlib.sha1("\x1f".join([kind, *map(str, keys)]).encode()).hexdigest()[:12]
    return f"{kind}-{h}"


def strength_of(share: float) -> str:
    return "strong" if share >= 0.9 else "medium" if share >= 0.7 else "weak"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def finding(kind, keys, title, detail, strength, basis, components=(), files=(), evidence=None, unit=None) -> dict:
    f = {"id": fingerprint(kind, *keys), "kind": kind, "group": kind.split(".")[0], "title": title,
         "detail": detail, "strength": strength, "basis": basis,
         "components": list(dict.fromkeys(c for c in components if c)),
         "files": list(dict.fromkeys(x for x in files if x))[:EXAMPLES], "evidence": evidence or {}}
    if unit:
        f["unit"] = unit
    return f


def comp_name(model: dict, cid: str) -> str:
    return namer(model)(cid)


_NAMERS = {}


def namer(model: dict):
    """cid -> its name, or its id when other components share the name."""
    key = id(model)
    hit = _NAMERS.get(key)
    if hit and hit[0] is model:
        return hit[1]
    comps = model.get("components") or {}
    names = {c: (v or {}).get("name") or c for c, v in comps.items()}
    dup = Counter(names.values())
    fn = lambda c: names.get(c, c) if dup[names.get(c, c)] == 1 else c
    _NAMERS.clear()
    _NAMERS[key] = (model, fn)
    return fn


def evidence_file(ev: str) -> str:
    """'path/file.ex:12' -> 'path/file.ex'."""
    return re.sub(r":\d+$", "", ev)


def graph(model: dict) -> dict:
    """{(from, to): {weight, evidence}} between internal components."""
    comps = model.get("components") or {}
    internal = {c for c, v in comps.items() if (v or {}).get("kind") != "external"}
    out = {}
    for d in model.get("dependencies") or []:
        a, b = d.get("from"), d.get("to")
        if a == b or a not in internal or b not in internal:
            continue
        e = out.setdefault((a, b), {"weight": 0, "evidence": []})
        e["weight"] += int(d.get("weight") or 1)
        e["evidence"] += d.get("evidence") or []
    return out
