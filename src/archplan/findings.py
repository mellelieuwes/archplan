"""Findings: what archplan notices in the code, as a list the human works
through. Each finding can start a discussion, be dismissed with a reason, or
be accepted as is.

Kinds, by group:

  profile     profile.rule     a unit breaks a calls rule of the architecture profile
              profile.pair     a unit has files without their partner (a view without a view builder)
  structure   structure.cycle  components that depend on each other in a cycle, with the cheapest cut
  layers      layers.upward    a component depends on a component in a higher layer
              layers.policy    a dependency the target's policy forbids
  vocabulary  vocabulary.leak  a component uses terms of a concept it should not (from the glossaries)
  types       types.fields     a type with many fields, or with groups of related fields
              types.synonyms   types in different components with mostly the same field names
  history     history.coupling components that change together without a dependency (cached hotspots)

A finding's id is a hash of its kind and stable keys (rule and unit, the
cycle's members, the two components), so it survives changes in counts.

The baseline in .archplan/baseline.json is committed. It holds the findings
the human accepted or dismissed, and Claude's labels:

  {"version": 1, "findings": {id: {"state", "reason", "at", "title"}},
   "labels": {id: {"label", "note", "at"}}}

Only git-free data is read: history findings come from .archplan/hotspots.json
as `archplan hotspots` left it."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict, deque

from . import architecture
from .architecture import Architecture, ArchitectureError
from .hotspots import Mapper
from .model import allowed, leaks
from .project import Project, etag, read_json, write_json
from .store import JsonStore, NotFound, Store, stamp

GROUPS = ["profile", "structure", "layers", "vocabulary", "types", "history"]
STRENGTHS = ["strong", "medium", "weak"]
STATES = ("new", "accepted", "dismissed")

MAX_CYCLES = 40          # cycles reported; each one found after cutting the cheaper ones before it
LARGE_TYPE = 25          # fields
CLUSTER_GROUPS = 3       # prefix groups ...
CLUSTER_SIZE = 3         # ... of at least this many fields
FLAGS = 6                # boolean fields
SYNONYM = 0.7            # Jaccard share of field names
SYNONYM_MIN = 4          # fields a type needs, after the common ones, to be compared
COMMON_FIELDS = {"id", "uuid", "pk", "inserted_at", "updated_at", "created_at", "modified_at", "deleted_at",
                 "created", "modified", "updated", "created_by", "updated_by", "lock_version", "version"}
COUPLING_DEGREE = 0.5
COUPLING_TOGETHER = 5
EXAMPLES = 20            # breaks, files or fields kept as evidence


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


# ---------- profile ----------

def profile_findings(prof: dict, analysis: dict, model: dict) -> list:
    roles = {r["id"]: r.get("name") or r["id"] for r in prof.get("roles") or []}
    rules = {r.get("id"): r for r in prof.get("rules") or []}
    units = {u["id"]: u for u in analysis["units"]}
    mapper = Mapper(model.get("components") or {})
    out = []
    for r in analysis["rules"]:
        by_unit = defaultdict(list)
        for b in r["breaks"]:
            by_unit[b["unit"]].append(b)
        checked = r.get("units_checked") or len(units)
        follow = max(checked - len(by_unit), 0)
        strength = strength_of(follow / checked) if checked else "weak"
        basis = f"{follow} of {checked} units follow it"
        rule = rules.get(r["id"]) or {}
        pair = r.get("type") == "pair"
        for uid, bs in sorted(by_unit.items()):
            u = units.get(uid) or {}
            where = comp_name(model, u["component"]) if u.get("component") else uid
            n = len(bs)
            if pair:
                kind = "profile.pair"
                title = (f"In {where}, {plural(n, roles.get(rule.get('role'), 'file') + ' file')} "
                         f"{'has' if n == 1 else 'have'} no matching {roles.get(rule.get('needs'), 'partner')}.")
            else:
                kind = "profile.rule"
                title = f"In {where}, {plural(n, 'reference')} {'breaks' if n == 1 else 'break'} the rule: {r['text']}."
            detail = "; ".join(f"{b['from']} -> {b.get('to') or '?'} ({b['detail']})" for b in bs[:3]) \
                + (f"; and {n - 3} more" if n > 3 else "")
            comps = [u.get("component")] + [mapper(b["from"]) for b in bs] + [mapper(b["to"]) for b in bs if b.get("to")]
            out.append(finding(kind, (r["id"], uid), title, detail, strength, basis, comps,
                               [b["from"] for b in bs] + [b["to"] for b in bs if b.get("to") and not pair],
                               {"rule": r["id"], "text": r["text"], "count": n, "breaks": bs[:EXAMPLES]}, uid))
    return out


# ---------- structure ----------

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


def sccs(nodes, edges) -> list:
    """Strongly connected components (Tarjan, iterative), each a sorted list."""
    adj = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
    for a in adj:
        adj[a].sort()
    index, low, on, stack, out, n = {}, {}, set(), [], [], 0
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(adj[root]))]
        index[root] = low[root] = n
        n += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            w = next(it, None)
            if w is not None:
                if w not in index:
                    index[w] = low[w] = n
                    n += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(adj[w])))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
    return out


def shortest_cycle(members: list, edges) -> list:
    """The shortest cycle inside one SCC as [a, b, ..., a]; ties go to the
    lightest, then to the first by name."""
    inside = set(members)
    adj = defaultdict(list)
    for (a, b), w in edges.items():
        if a in inside and b in inside:
            adj[a].append(b)
    best, best_key = None, None
    for s in members:
        prev, q = {s: None}, deque([s])
        found = None
        while q and found is None:
            v = q.popleft()
            for w in sorted(adj[v]):
                if w == s:
                    found = v
                    break
                if w not in prev:
                    prev[w] = v
                    q.append(w)
        if found is None:
            continue
        path = [found]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])
        path = path[::-1] + [s]
        key = (len(path), sum(edges[(a, b)] for a, b in zip(path, path[1:])), path)
        if best_key is None or key < best_key:
            best, best_key = path, key
    return best


def cycle_findings(model: dict) -> list:
    """Small cycles, found greedily: take the shortest cycle of each strongly
    connected component, cut its lightest edge, and repeat."""
    g = graph(model)
    weights = {k: v["weight"] for k, v in g.items()}
    nodes = {x for k in g for x in k}
    size = {c: len(s) for s in sccs(nodes, weights) for c in s}
    out, seen = [], set()
    while len(out) < MAX_CYCLES:
        groups = [s for s in sccs(nodes, weights) if len(s) > 1]
        if not groups:
            break
        for s in groups:
            cyc = shortest_cycle(s, weights)
            if not cyc:
                continue
            pairs = list(zip(cyc, cyc[1:]))
            cut = min(pairs, key=lambda e: (weights[e], e))
            del weights[cut]
            key = tuple(sorted(cyc[:-1]))
            if key in seen:
                continue
            seen.add(key)
            # Start the cycle at its first member by name, so the title is stable.
            ring = cyc[:-1]
            i = ring.index(min(ring))
            ring = ring[i:] + ring[:i]
            names = [comp_name(model, c) for c in ring + ring[:1]]
            w = g[cut]["weight"]
            ev = g[cut]["evidence"][:EXAMPLES]
            out.append(finding(
                "structure.cycle", key,
                f"{' → '.join(names)} form a dependency cycle.",
                f"Cheapest cut: {comp_name(model, cut[0])} → {comp_name(model, cut[1])}, {plural(w, 'reference')}.",
                "strong", f"{plural(len(ring), 'component')}" + (f", part of a tangle of {size[ring[0]]}"
                                                                if size.get(ring[0], 0) > len(ring) else ""),
                ring, [evidence_file(x) for x in ev],
                {"cycle": ring, "edges": [{"from": a, "to": b, "weight": g[(a, b)]["weight"]}
                                          for a, b in zip(ring, ring[1:] + ring[:1])],
                 "cut": {"from": cut[0], "to": cut[1], "weight": w, "evidence": ev},
                 "tangle": size.get(ring[0], len(ring))}))
            if len(out) >= MAX_CYCLES:
                break
    return out


# ---------- layers ----------

def layer_findings(prof: dict, model: dict) -> list:
    """Dependencies from a lower layer to a higher one, one finding per
    component and higher layer. Where most dependencies between two layers go
    up, the layer order itself is in doubt: one finding for the pair."""
    layers = [x for x in prof.get("layers") or [] if x.get("components")]
    rank = {c: i for i, layer in enumerate(layers) for c in layer["components"]}
    if len(layers) < 2:
        return []
    name = namer(model)
    g = graph(model)
    down, up = Counter(), defaultdict(list)  # (higher, lower) -> count / upward edges
    for (a, b) in sorted(g):
        if a in rank and b in rank and rank[a] != rank[b]:
            pair = tuple(sorted((rank[a], rank[b])))
            if rank[a] < rank[b]:
                down[pair] += 1
            else:
                up[pair].append((a, b))
    out = []
    for pair, edges in sorted(up.items()):
        hi, lo = layers[pair[0]], layers[pair[1]]
        total = down[pair] + len(edges)
        share = down[pair] / total
        basis = f"{down[pair]} of {total} dependencies between these layers go down"
        if share < 0.5:
            comps = sorted({a for a, _ in edges})
            ev = [x for a, b in edges for x in g[(a, b)]["evidence"][:1]]
            out.append(finding(
                "layers.upward", ("layers", lo.get("name"), hi.get("name")),
                f"Most dependencies between {lo.get('name')} and {hi.get('name')} go up: "
                f"{lo.get('name')} may not belong below {hi.get('name')}.",
                f"{plural(len(edges), 'dependency', 'dependencies')} from {plural(len(comps), 'component')} in "
                f"{lo.get('name')}, such as " + ", ".join(f"{name(a)} → {name(b)}" for a, b in edges[:3]) + ".",
                "weak", basis, comps[:EXAMPLES], [evidence_file(x) for x in ev],
                {"from_layer": lo.get("name"), "to_layer": hi.get("name"), "upward": len(edges), "down": down[pair],
                 "edges": [{"from": a, "to": b, "weight": g[(a, b)]["weight"]} for a, b in edges[:EXAMPLES]]}))
            continue
        by_comp = defaultdict(list)
        for a, b in edges:
            by_comp[a].append(b)
        for a, targets in sorted(by_comp.items()):
            refs = sum(g[(a, b)]["weight"] for b in targets)
            ev = [x for b in targets for x in g[(a, b)]["evidence"]]
            more = f" and {plural(len(targets) - 1, 'other')}" if len(targets) > 1 else ""
            out.append(finding(
                "layers.upward", (a, hi.get("name")),
                f"{name(a)} in {lo.get('name')} depends on {name(targets[0])}{more} in the higher layer {hi.get('name')}.",
                f"{plural(refs, 'reference')}: " + ", ".join(ev[:3]),
                strength_of(share), basis, [a, *targets], [evidence_file(x) for x in ev],
                {"from": a, "to": targets, "weight": refs, "evidence": ev[:EXAMPLES],
                 "from_layer": lo.get("name"), "to_layer": hi.get("name")}))
    return out


def policy_findings(model: dict, target: dict) -> list:
    pol = (target or {}).get("policy") or {}
    if not pol.get("rules") and pol.get("default", "allow") == "allow":
        return []
    out = []
    for (a, b), e in sorted(graph(model).items()):
        if allowed(target, a, b):
            continue
        rules = [r for r in pol.get("rules", []) if r.get("from") in (a, "*") and r.get("to") in (b, "*")]
        out.append(finding(
            "layers.policy", (a, b),
            f"{comp_name(model, a)} depends on {comp_name(model, b)}, which the policy forbids.",
            f"{plural(e['weight'], 'reference')}: " + ", ".join(e["evidence"][:3]),
            "strong", "an explicit rule in the target's policy" if rules else "the policy's default is deny",
            [a, b], [evidence_file(x) for x in e["evidence"]],
            {"from": a, "to": b, "weight": e["weight"], "evidence": e["evidence"][:EXAMPLES], "rules": rules}))
    return out


# ---------- vocabulary ----------

def leak_findings(target: dict) -> list:
    out = []
    concepts = ((target or {}).get("vocabulary") or {}).get("concepts", {})
    for x in leaks(target or {}):
        c, k = x.get("component"), x.get("concept")
        name = concepts.get(k, {}).get("name") or k
        n = x.get("count") or len(x.get("evidence") or x.get("terms") or []) or None
        why = "it lists the concept under avoid" if x["why"] == "avoid" else \
            f"the policy forbids it to depend on {x.get('owner')}, which owns it"
        out.append(finding(
            "vocabulary.leak", (c, k),
            f"{comp_name(target, c)} uses the term \"{name}\", owned by {x.get('owner') or 'no component'}.",
            f"It should not: {why}.", "strong" if x["why"] == "avoid" else "medium",
            "listed under avoid in the glossary" if x["why"] == "avoid" else "follows from the policy",
            [c, x.get("owner")], [evidence_file(e) for e in x.get("evidence") or [] if isinstance(e, str)],
            {"anchor": f"leak:{c}:{k}", "concept": k, "owner": x.get("owner"), "why": x["why"],
             **({"count": n} if n else {})}))
    return out


# ---------- types ----------

def is_flag(f: dict) -> bool:
    t = (f.get("type") or "").lower()
    return "bool" in t and "|" not in t.replace("| nil", "").replace("| none", "")


def field_findings(model: dict) -> list:
    out = []
    for tid, t in sorted((model.get("types") or {}).items()):
        fields = [f for f in t.get("fields") or [] if f.get("name")]
        if len(fields) < max(CLUSTER_GROUPS * CLUSTER_SIZE, FLAGS):
            continue
        groups = defaultdict(list)
        for f in fields:
            head, sep, _ = f["name"].lstrip("_").partition("_")
            if sep and head:
                groups[head].append(f["name"])
        clusters = {k: v for k, v in groups.items() if len(v) >= CLUSTER_SIZE and k not in ("is", "has", "can")}
        flags = [f["name"] for f in fields if is_flag(f)]
        why = []
        if len(fields) >= LARGE_TYPE:
            why.append(f"{len(fields)} fields")
        if len(clusters) >= CLUSTER_GROUPS:
            why.append(plural(len(clusters), "group") + " of related fields")
        if len(flags) >= FLAGS:
            why.append(plural(len(flags), "boolean flag"))
        if not why:
            continue
        name = t.get("name") or tid
        if len(clusters) >= CLUSTER_GROUPS:
            title = f"{tid} has {plural(len(clusters), 'group')} of related fields."
        elif len(fields) >= LARGE_TYPE:
            title = f"{tid} has {len(fields)} fields."
        else:
            title = f"{tid} has {len(flags)} boolean flags."
        detail = " · ".join([f"{k}_* ×{len(v)}" for k, v in sorted(clusters.items(), key=lambda kv: -len(kv[1]))]
                            + ([plural(len(flags), "boolean flag")] if len(flags) >= FLAGS else []))
        size = len(fields)
        strength = "strong" if len(why) >= 2 or size >= 2 * LARGE_TYPE else "medium"
        out.append(finding(
            "types.fields", (tid,), title, detail or ", ".join(why), strength, "; ".join(why),
            [t.get("component")], [t.get("file")],
            {"type": tid, "name": name, "fields": size,
             "groups": {k: v[:EXAMPLES] for k, v in sorted(clusters.items(), key=lambda kv: -len(kv[1]))[:8]},
             "flags": flags[:EXAMPLES], **({"line": t["line"]} if t.get("line") else {})}))
    return out


def names_of(t: dict) -> frozenset:
    return frozenset(f["name"] for f in t.get("fields") or [] if f.get("name") and f["name"] not in COMMON_FIELDS)


def synonym_findings(model: dict) -> list:
    """Groups of types in different components whose field names mostly match."""
    types = {tid: t for tid, t in (model.get("types") or {}).items()
             if t.get("component") and t.get("kind") not in ("interface", "enum", "function", "module", "alias")}
    names = {tid: names_of(t) for tid, t in types.items()}
    names = {tid: n for tid, n in names.items() if len(n) >= SYNONYM_MIN}
    index = defaultdict(set)
    for tid, n in names.items():
        for x in n:
            index[x].add(tid)
    parent = {}

    def root(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x
    best = {}
    for a in sorted(names):
        shared = Counter(b for x in names[a] for b in index[x] if b > a)
        for b, k in shared.items():
            if types[a]["component"] == types[b]["component"]:
                continue
            j = k / len(names[a] | names[b])
            if j >= SYNONYM:
                parent[root(b)] = root(a)
                best[(a, b)] = j
    groups = defaultdict(list)
    for x in {x for pair in best for x in pair}:
        groups[root(x)].append(x)
    out = []
    for members in groups.values():
        members.sort()
        common = frozenset.intersection(*(names[m] for m in members))
        js = [j for (a, b), j in best.items() if a in members]
        low = min(js)
        title = ", ".join(f"{types[m].get('name') or m} ({comp_name(model, types[m]['component'])})"
                          for m in members[:-1])
        title = (title + " and " if title else "") + \
            f"{types[members[-1]].get('name') or members[-1]} ({comp_name(model, types[members[-1]]['component'])})"
        union = frozenset.union(*(names[m] for m in members))
        out.append(finding(
            "types.synonyms", tuple(members), f"{title} share most of their fields.",
            f"{len(common)} of {len(union)} field names shared: " + ", ".join(sorted(common)[:8])
            + (", …" if len(common) > 8 else ""),
            "strong" if low >= 0.9 else "medium" if low >= 0.8 else "weak",
            f"{round(low * 100)}% or more of field names in common",
            [types[m]["component"] for m in members], [types[m].get("file") for m in members],
            {"types": members, "shared": sorted(common)[:EXAMPLES],
             "pairs": [{"a": a, "b": b, "jaccard": round(j, 2)} for (a, b), j in sorted(best.items()) if a in members]}))
    return out


# ---------- history ----------

def coupling_findings(p: Project, model: dict) -> list | None:
    """Hidden coupling from the cached hotspots analysis; None without a cache."""
    cache = read_json(p.path("hotspots.json")) or {}
    res = cache.get("result")
    if not res:
        return None
    comps = model.get("components") or {}
    deps = {(d["from"], d["to"]) for d in model.get("dependencies") or []}
    out = []
    for x in res.get("pairs") or []:
        a, b = x["a"], x["b"]
        if a not in comps or b not in comps or (a, b) in deps or (b, a) in deps:
            continue
        if x["degree"] < COUPLING_DEGREE or x["together"] < COUPLING_TOGETHER:
            continue
        out.append(finding(
            "history.coupling", tuple(sorted((a, b))),
            f"{comp_name(model, a)} and {comp_name(model, b)} change together but do not depend on each other.",
            f"{x['together']} shared commits, degree {x['degree']:.2f}, in the last {res.get('since_days')} days.",
            "strong" if x["degree"] >= 0.8 else "medium" if x["degree"] >= 0.65 else "weak",
            f"{x['together']} commits together, degree {x['degree']:.2f}", [a, b], [],
            {"a": a, "b": b, "together": x["together"], "degree": x["degree"], "head": (res.get("head") or "")[:10],
             "since_days": res.get("since_days")}))
    return out


# ---------- all of them ----------

def analyse(p: Project, arch: Architecture | None = None) -> tuple:
    """(findings, missing): every finding the data supports, without states,
    and the kinds left out for lack of data, with the reason."""
    arch = arch or Architecture(p)
    model = arch.model()
    target = p.target_or_model() or model
    missing = []
    out = []
    try:
        st = arch.state()
        prof = st.get("profile") or st.get("proposal") or {}
        a = architecture.analyse(prof, model, p.root, cap=10 ** 6)
        if a.get("edges"):
            out += profile_findings(prof, a, model)
        else:
            missing.append({"kind": "profile.rule", "reason": "the model has no file references (files section)"})
            out += profile_findings({**prof, "rules": [r for r in prof.get("rules") or [] if r.get("type") == "pair"]},
                                    {**a, "rules": [r for r in a["rules"] if r.get("type") == "pair"]}, model)
    except ArchitectureError as e:
        prof = {}
        missing.append({"kind": "profile.rule", "reason": str(e)})
    if not model.get("dependencies"):
        missing.append({"kind": "structure.cycle", "reason": "the model has no dependencies"})
    out += cycle_findings(model)
    if len([x for x in prof.get("layers") or [] if x.get("components")]) < 2:
        missing.append({"kind": "layers.upward", "reason": "the profile has fewer than two layers"})
    out += layer_findings(prof, model)
    out += policy_findings(model, target)
    if not (target.get("vocabulary") or {}).get("usage"):
        missing.append({"kind": "vocabulary.leak", "reason": "the project has no glossaries (vocabulary usage)"})
    out += leak_findings(target)
    if not any(t.get("fields") for t in (model.get("types") or {}).values()):
        missing.append({"kind": "types.fields", "reason": "the model's types have no fields"})
    out += field_findings(model)
    out += synonym_findings(model)
    hist = coupling_findings(p, model)
    if hist is None:
        missing.append({"kind": "history.coupling", "reason": "no cached hotspots; run `archplan hotspots`"})
    out += hist or []
    seen, uniq = set(), []
    for f in out:
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    uniq.sort(key=lambda f: (GROUPS.index(f["group"]), STRENGTHS.index(f["strength"]), f["kind"], f["title"]))
    return uniq, missing


TEXT_MAX = 500  # characters kept of a baseline string


def clip(x, n: int = TEXT_MAX) -> str | None:
    return None if x is None else str(x)[:n]


def clean_baseline(d) -> dict:
    """The baseline as committed repo data: untrusted. Only accepted and
    dismissed entries count; every value becomes a short string."""
    d = d if isinstance(d, dict) else {}
    fs, ls = d.get("findings"), d.get("labels")
    out = {"version": 1, "findings": {}, "labels": {}}
    for k, e in (fs if isinstance(fs, dict) else {}).items():
        if isinstance(e, dict) and e.get("state") in ("accepted", "dismissed"):
            out["findings"][clip(k, 100)] = {"state": e["state"], "reason": clip(e.get("reason")),
                                             "at": clip(e.get("at"), 40), "title": clip(e.get("title"))}
    for k, e in (ls if isinstance(ls, dict) else {}).items():
        if isinstance(e, dict) and e.get("label"):
            out["labels"][clip(k, 100)] = {"label": clip(e["label"], 60), "note": clip(e.get("note")),
                                           "at": clip(e.get("at"), 40)}
    return out


class Baseline(JsonStore):
    """.archplan/baseline.json. Its version is the file format, not a write counter."""
    name = "baseline"
    empty = {"version": 1, "findings": {}, "labels": {}}

    def read(self) -> dict:
        try:
            return clean_baseline(super().read())
        except ValueError:  # not JSON
            return clean_baseline(None)

    def update(self, fn):
        with self.locked():
            data = self.read()
            out = fn(data)
            data["version"] = 1
            write_json(self.path, data)
            return out


class Findings:
    def __init__(self, p: Project):
        self.p = p
        self.arch = Architecture(p)
        self.baseline = Baseline(p.dir)
        self._cache = (None, None)

    def raw(self) -> tuple:
        key = tuple(etag(x) for x in (self.p.model_path, self.p.target_path, self.arch.path,
                                      self.p.path("hotspots.json")))
        if self._cache[0] != key:
            self._cache = (key, analyse(self.p, self.arch))
        return self._cache[1]

    def listing(self, store: Store | None = None) -> dict:
        """{"findings", "counts", "missing"}: every current finding with its
        state, label and thread."""
        fs, missing = self.raw()
        b = self.baseline.read()
        base, labels = b.get("findings") or {}, b.get("labels") or {}
        threads = {}
        for th in (store or Store(self.p.dir)).read()["threads"]:
            if th["anchor"].startswith("finding:"):
                threads.setdefault(th["anchor"][8:], th["id"])
        out = []
        for f in fs:
            e = base.get(f["id"]) or {}
            f = {**f, "state": e.get("state", "new"), "reason": e.get("reason")}
            if f["id"] in labels:
                f["label"] = labels[f["id"]].get("label")
                f["label_note"] = labels[f["id"]].get("note")
            if f["id"] in threads:
                f["thread"] = threads[f["id"]]
            out.append(f)
        now = {f["id"] for f in fs}
        by_group, by_kind = Counter(), Counter()
        for f in out:
            if f["state"] == "new":
                by_group[f["group"]] += 1
                by_kind[f["kind"]] += 1
        counts = {s: sum(1 for f in out if f["state"] == s) for s in STATES}
        counts.update({"fixed": sum(1 for i in base if i not in now),
                       "by_group": {g: by_group[g] for g in GROUPS}, "by_kind": dict(sorted(by_kind.items()))})
        return {"findings": out, "counts": counts, "missing": missing}

    def get(self, fid: str, store: Store | None = None) -> dict:
        for f in self.listing(store)["findings"]:
            if f["id"] == fid:
                return f
        raise NotFound(f"no finding {fid}")

    def decide(self, fid: str, state: str, reason: str | None = None) -> dict:
        """Accept or dismiss a finding (it goes into the baseline), or reopen it."""
        if state not in STATES:
            raise ValueError(f"bad state {state!r}")
        reason = (reason or "").strip()
        if state == "dismissed" and not reason:
            raise ValueError("a dismissal needs a reason")
        if state == "new":
            def fn(d):
                if fid not in d["findings"]:
                    raise NotFound(f"finding {fid} is not in the baseline")
                del d["findings"][fid]
            self.baseline.update(fn)
            return self.get(fid)
        f = self.get(fid)
        self.baseline.update(lambda d: d["findings"].__setitem__(
            fid, {"state": state, "reason": reason or None, "at": stamp(), "title": f["title"]}))
        return {**f, "state": state, "reason": reason or None}

    def label(self, fid: str, label: str, note: str = "") -> dict:
        """Claude's judgment on a finding, shown as a label. An empty label removes it."""
        self.get(fid)
        label = (label or "").strip()[:60]

        def fn(d):
            if label:
                d["labels"][fid] = {"label": label, "note": (note or "").strip()[:500] or None, "at": stamp()}
            else:
                d["labels"].pop(fid, None)
        self.baseline.update(fn)
        return self.get(fid)

    def discuss(self, store: Store, fid: str, text: str | None = None) -> tuple:
        """(thread, created): the thread on finding:<id>, made with the
        finding's evidence as its snapshot when there is none yet."""
        f = self.get(fid, store)
        if f.get("thread"):
            return next(t for t in store.read()["threads"] if t["id"] == f["thread"]), False
        model = self.arch.model()
        snap = {"view": "findings", "focus": [f"finding:{fid}"],
                "nodes": [{"id": c, "label": comp_name(model, c)} for c in f["components"][:30]],
                "finding": {k: f[k] for k in ("id", "kind", "group", "title", "detail", "strength", "basis", "files")},
                "evidence": f["evidence"]}
        cyc = f["evidence"].get("edges") if f["kind"] == "structure.cycle" else None
        if cyc:
            snap["edges"] = cyc
        th = store.create(f"finding:{fid}", f["title"], (text or "").strip() or f"{f['title']}\n\n{f['detail']}",
                          "human", snapshot=snap)
        return th, True

    def update_baseline(self) -> dict:
        """Accept every current new finding with the reason "baseline", and
        drop entries for findings that are gone."""
        fs, _ = self.raw()
        now = {f["id"]: f for f in fs}

        def fn(d):
            gone = [i for i in d["findings"] if i not in now]
            for i in gone:
                del d["findings"][i]
            added = 0
            for i, f in now.items():
                if i not in d["findings"]:
                    d["findings"][i] = {"state": "accepted", "reason": "baseline", "at": stamp(), "title": f["title"]}
                    added += 1
            return {"added": added, "removed": len(gone), "total": len(d["findings"])}
        return self.baseline.update(fn)


def compact(f: dict) -> dict:
    """A finding in a few lines, for an agent."""
    out = {k: f[k] for k in ("id", "kind", "title", "detail", "strength", "basis", "state") if f.get(k) is not None}
    out["components"] = f["components"][:6]
    if f["files"]:
        out["files"] = f["files"][:4]
    for k in ("unit", "reason", "label", "thread"):
        if f.get(k):
            out[k] = f[k]
    return out


def github_escape(s: str, prop: bool = False) -> str:
    s = s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return s.replace(":", "%3A").replace(",", "%2C") if prop else s


def line_of(f: dict) -> int | None:
    ev = f.get("evidence") or {}
    if ev.get("line"):
        return ev["line"]
    for b in ev.get("breaks") or []:
        m = re.search(r"\(line (\d+)\)", b.get("detail", ""))
        if m and b.get("from") == (f["files"] or [None])[0]:
            return int(m.group(1))
    for x in (ev.get("cut") or {}).get("evidence") or ev.get("evidence") or []:
        m = re.match(r"(.*):(\d+)$", x) if isinstance(x, str) else None
        if m and m.group(1) == (f["files"] or [None])[0]:
            return int(m.group(2))
    return None


def text(listing: dict, show_all: bool = False) -> str:
    c = listing["counts"]
    fs = [f for f in listing["findings"] if show_all or f["state"] == "new"]
    lines = [f"{c['new']} new, {c['accepted']} accepted, {c['dismissed']} dismissed"
             + (f", {c['fixed']} baselined findings fixed" if c["fixed"] else "") + "."]
    group = None
    for f in fs:
        if f["group"] != group:
            group = f["group"]
            lines += ["", group.capitalize() + ":"]
        st = "" if f["state"] == "new" else f" [{f['state']}" + (f": {f['reason']}" if f.get("reason") else "") + "]"
        lab = f" (Claude: {f['label']})" if f.get("label") else ""
        lines.append(f"  {f['strength']:<6}  {f['title']}{lab}{st}")
        lines.append(f"          {f['basis']}. {f['detail']}"[:300])
        lines.append(f"          id {f['id']}" + (f", {f['files'][0]}" if f["files"] else ""))
    for m in listing.get("missing") or []:
        lines += ["", f"Not checked, {m['kind']}: {m['reason']}."] if m is listing["missing"][0] else \
            [f"Not checked, {m['kind']}: {m['reason']}."]
    return "\n".join(lines)


def github(listing: dict, show_all: bool = False) -> str:
    out = []
    for f in listing["findings"]:
        if not show_all and f["state"] != "new":
            continue
        props = []
        if f["files"]:
            props.append("file=" + github_escape(f["files"][0], True))
            ln = line_of(f)
            if ln:
                props.append(f"line={ln}")
        props.append("title=" + github_escape(f"archplan {f['kind']}", True))
        out.append(f"::warning {','.join(props)}::{github_escape(f['title'] + ' ' + f['basis'] + '.')}")
    return "\n".join(out)
