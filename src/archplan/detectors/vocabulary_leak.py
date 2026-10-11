"""vocabulary.leak: a component uses terms of a concept it should not (from the glossaries)."""

from __future__ import annotations

from ..model import leaks
from .common import comp_name, evidence_file, finding

ID = "vocabulary.leak"
NAME = "Vocabulary leaks"
GROUP = "vocabulary"
NEEDS = {"vocabulary"}
PARAMS = {}



def run(ctx, params) -> list:
    return leak_findings(ctx.target)


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
