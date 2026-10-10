"""Queries and edits on archmodel/1 files, for the agent side."""

from __future__ import annotations

import copy
import json

from .project import Project, etag, read_json

ANCHORS = ("Anchors name what a thread is about: type:<type id>, comp:<component id>, "
           "cedge:<from>-><to> (a component dependency), msg:<sequence id>:<message id>, "
           "concept:<concept id> (a glossary concept), leak:<component id>:<concept id> (a component using "
           "terms of a concept it should not), tedge:<kind>:<from type>-><to type> (a type relation), "
           "task:<plan task key>, step:<plan task key>#<step index>, and group:<anchor>|<anchor>|... "
           "for one comment on several items, such as an import cycle.")


def allowed(m: dict, f: str, t: str) -> bool:
    """The editor's policy check: a deny rule wins, then the default or an allow rule."""
    p = m.get("policy", {})
    rules = p.get("rules", [])
    hits = lambda r: r.get("from") in ("*", f) and r.get("to") in ("*", t)
    if any(r.get("effect") == "deny" and hits(r) for r in rules):
        return False
    return p.get("default", "allow") == "allow" or any(r.get("effect") == "allow" and hits(r) for r in rules)


def leaks(m: dict) -> list:
    """Usage rows where a component uses terms of a concept it should not:
    one it lists under avoid, or one owned by a component the policy forbids."""
    v = m.get("vocabulary") or {}
    concepts, comps, out = v.get("concepts", {}), v.get("components", {}), []
    for u in v.get("usage", []):
        c, k = u.get("component"), u.get("concept")
        o = concepts.get(k, {}).get("owner")
        if k not in concepts or c == o or k in comps.get(c, {}).get("uses", []):
            continue
        if k in comps.get(c, {}).get("avoid", []):
            why = "avoid"
        elif o and c in m.get("components", {}) and o in m.get("components", {}) and not allowed(m, c, o):
            why = "policy"
        else:
            continue
        out.append({**u, "owner": o, "why": why})
    return out


def violations(m: dict) -> list:
    """Dependencies in the model that its own policy forbids."""
    return [d for d in m.get("dependencies", []) if d["from"] != d["to"] and not allowed(m, d["from"], d["to"])]


def get_item(p: Project, anchor: str):
    kind, _, rest = anchor.partition(":")
    if kind == "group":
        return {"group": [{"anchor": a, "item": get_item(p, a)} for a in rest.split("|") if a]}
    if kind == "tedge":
        rel, _, pair = rest.partition(":")
        src, _, dst = pair.partition("->")
        return {"relation": rel, "from": {"anchor": "type:" + src, "item": get_item(p, "type:" + src)},
                "to": {"anchor": "type:" + dst, "item": get_item(p, "type:" + dst)}}
    if kind in ("task", "step"):
        key = rest.split("#")[0]
        plan = read_json(p.path("plan.json"))
        if not plan:
            return None
        return next((t for t in plan.get("tasks", []) if t.get("key") == key), None)
    m = p.target_or_model()
    if not m:
        return None
    if kind == "type":
        return m.get("types", {}).get(rest)
    if kind == "comp":
        c = m.get("components", {}).get(rest)
        if c is None or "vocabulary" not in m:
            return c
        v = m["vocabulary"]
        cv = v.get("components", {}).get(rest, {})
        return {**c, "glossary": {
            "file": cv.get("file"),
            "owns": {k: x for k, x in v.get("concepts", {}).items() if x.get("owner") == rest},
            "uses": cv.get("uses", []), "avoid": cv.get("avoid", []), "suggested": cv.get("suggested", []),
            "leaks": [x for x in leaks(m) if x["component"] == rest]}}
    if kind == "concept":
        v = m.get("vocabulary", {})
        k = v.get("concepts", {}).get(rest)
        if k is None:
            return None
        comps = v.get("components", {})
        return {"id": rest, **k,
                "used_by": [c for c, cv in comps.items() if rest in cv.get("uses", [])],
                "avoided_by": [c for c, cv in comps.items() if rest in cv.get("avoid", [])],
                "usage": [u for u in v.get("usage", []) if u.get("concept") == rest],
                "leaks": [x for x in leaks(m) if x["concept"] == rest]}
    if kind == "leak":
        comp, _, cid = rest.partition(":")
        hit = next((x for x in leaks(m) if x["component"] == comp and x["concept"] == cid), None)
        usage = next((u for u in m.get("vocabulary", {}).get("usage", [])
                      if u.get("component") == comp and u.get("concept") == cid), None)
        return {"leak": hit, "usage": usage, "concept": m.get("vocabulary", {}).get("concepts", {}).get(cid),
                "note": None if hit else "Not a leak in the target model."}
    if kind == "cedge":
        f, _, t = rest.partition("->")
        return {"dependency": next((d for d in m.get("dependencies", []) if d["from"] == f and d["to"] == t), None),
                "allowed": allowed(m, f, t),
                "rules": [r for r in m.get("policy", {}).get("rules", []) if r["from"] in (f, "*") and r["to"] in (t, "*")]}
    if kind == "msg":
        seq, _, mid = rest.partition(":")
        s = m.get("sequences", {}).get(seq, {})
        return next((x for x in s.get("messages", []) if x["id"] == mid), None)
    return None


def find_items(p: Project, query: str, kind: str = "any", limit: int = 30) -> list:
    """Components and types whose id, name or path contains query (case-insensitive)."""
    m = p.target_or_model() or {}
    q = query.lower()
    out = []
    if kind in ("any", "component"):
        for cid, c in m.get("components", {}).items():
            if q in cid.lower() or q in (c.get("name") or "").lower() or q in (c.get("path") or "").lower():
                out.append({"anchor": "comp:" + cid, "name": c.get("name", cid), "path": c.get("path"), "layer": c.get("layer")})
    if kind in ("any", "type"):
        for tid, t in m.get("types", {}).items():
            if q in tid.lower():
                out.append({"anchor": "type:" + tid, "name": t.get("name"), "component": t.get("component"), "kind": t.get("kind")})
    rank = lambda x: (0 if (x["name"] or "").lower() == q else 1 if q in (x["name"] or "").lower() else 2)
    return sorted(out, key=rank)[:limit]


# ---------- target edits ----------

EDIT_OPS = {
    "add_component": "{op, id, name?, path?, layer?, doc?}: a new (empty) component",
    "remove_component": "{op, id}: remove a component that has no types left",
    "set_component": "{op, id, name?, path?, layer?, doc?}: change a component's fields",
    "move_type": "{op, type, to}: move a type to another component",
    "rename_type": "{op, type, name}: rename a type",
    "set_doc": "{op, type, doc}: change a type's doc",
    "forbid": "{op, from, to, note?}: forbid a dependency (adds a deny rule)",
    "permit": "{op, from, to, note?}: allow a dependency (removes deny rules, adds an allow rule when needed)",
    "remove_dependency": "{op, from, to}: drop an import edge the target should no longer have",
}


def apply_edits(m: dict, ops: list) -> list:
    """Apply edit operations to an archmodel dict in place; return one line per op."""
    out = []
    comps, types = m.setdefault("components", {}), m.setdefault("types", {})
    pol = m.setdefault("policy", {"default": "allow", "rules": []})
    rules = pol.setdefault("rules", [])

    def need_comp(cid):
        if cid not in comps:
            raise ValueError(f"no component {cid!r}")

    def need_type(tid):
        if tid not in types:
            raise ValueError(f"no type {tid!r}")

    for op in ops:
        o = op.get("op")
        if o == "add_component":
            cid = op["id"]
            if cid in comps:
                raise ValueError(f"component {cid!r} exists")
            comps[cid] = {k: op[k] for k in ("name", "path", "layer", "doc") if op.get(k)}
            comps[cid].setdefault("name", cid)
            out.append(f"added component {cid}")
        elif o == "remove_component":
            cid = op["id"]
            need_comp(cid)
            left = [t for t, x in types.items() if x.get("component") == cid]
            if left:
                raise ValueError(f"component {cid!r} still has {len(left)} types, e.g. {left[0]}")
            del comps[cid]
            m["dependencies"] = [d for d in m.get("dependencies", []) if cid not in (d["from"], d["to"])]
            out.append(f"removed component {cid}")
        elif o == "set_component":
            cid = op["id"]
            need_comp(cid)
            comps[cid].update({k: op[k] for k in ("name", "path", "layer", "doc") if k in op})
            out.append(f"updated component {cid}")
        elif o == "move_type":
            need_type(op["type"]); need_comp(op["to"])
            was = types[op["type"]].get("component")
            types[op["type"]]["component"] = op["to"]
            out.append(f"moved {op['type']}: {was} -> {op['to']}")
        elif o == "rename_type":
            need_type(op["type"])
            types[op["type"]]["name"] = op["name"]
            out.append(f"renamed {op['type']} to {op['name']}")
        elif o == "set_doc":
            need_type(op["type"])
            types[op["type"]]["doc"] = op["doc"]
            out.append(f"set doc of {op['type']}")
        elif o in ("forbid", "permit"):
            f, t = op["from"], op["to"]
            for c in (f, t):
                if c != "*":
                    need_comp(c)
            drop = "allow" if o == "forbid" else "deny"
            rules[:] = [r for r in rules if not (r.get("from") == f and r.get("to") == t and r.get("effect") == drop)]
            want = o == "permit"
            if allowed(m, f, t) != want:
                r = {"from": f, "to": t, "effect": "allow" if want else "deny"}
                if op.get("note"):
                    r["note"] = op["note"]
                rules.append(r)
            out.append(f"{'forbade' if o == 'forbid' else 'permitted'} {f} -> {t}")
        elif o == "remove_dependency":
            before = len(m.get("dependencies", []))
            m["dependencies"] = [d for d in m.get("dependencies", []) if not (d["from"] == op["from"] and d["to"] == op["to"])]
            if len(m["dependencies"]) == before:
                raise ValueError(f"no dependency {op['from']} -> {op['to']}")
            out.append(f"removed dependency {op['from']} -> {op['to']}")
        else:
            raise ValueError(f"unknown op {o!r}; known: {', '.join(EDIT_OPS)}")
    return out


def edited(p: Project, ops: list) -> tuple:
    """A copy of the target model (or the as-is model when there is no target
    yet) with ops applied, and one line per op. Raises ValueError when an op
    does not apply; the stored target is not touched."""
    m = p.target_or_model()
    if not m:
        raise ValueError("no model yet; run the export first (refresh_model)")
    if not isinstance(ops, list) or not all(isinstance(o, dict) for o in ops):
        raise ValueError("ops must be a list of objects")
    m = copy.deepcopy(m)
    return m, apply_edits(m, ops)


def save_edited(p: Project, m: dict, by: str) -> int:
    """Save an edited target. The first target starts from the current model, so
    that model becomes its base."""
    base = None if p.target_path.exists() else etag(p.model_path)
    return p.save_target(m, by, base)


def edit_target(p: Project, ops: list, by: str = "agent") -> dict:
    """Apply ops to the target model and save it. All ops apply or none."""
    m, done = edited(p, ops)
    rev = save_edited(p, m, by)
    return {"applied": done, "target_rev": rev, "violations": len(violations(m)),
            "note": "The editor shows the change in its Diff view within a few seconds and "
                    "recomputes the plan; call get_plan afterwards to see the tasks."}


def dumps(x) -> str:
    return x if isinstance(x, str) else json.dumps(x, indent=2, ensure_ascii=False)
