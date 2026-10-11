"""profile.pair: a unit has files without their partner (a view without a view builder)."""

from __future__ import annotations

from .profile_rule import profile_findings

ID = "profile.pair"
NAME = "Profile file pairs"
GROUP = "profile"
NEEDS = {"profile"}
PARAMS = {}



def run(ctx, params) -> list:
    prof, a = ctx.profile
    pair = lambda r: r.get("type") == "pair"
    return profile_findings({**prof, "rules": [r for r in prof.get("rules") or [] if pair(r)]},
                            {**a, "rules": [r for r in a["rules"] if pair(r)]}, ctx.model)
