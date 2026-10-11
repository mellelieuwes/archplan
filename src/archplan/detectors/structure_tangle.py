"""structure.tangle: four or more components that all reach each other. One finding, with a
small set of cuts that breaks every cycle. Its id is its hub, so it survives members joining or
leaving."""

from __future__ import annotations

from .cycles import tangle_findings

ID = "structure.tangle"
NAME = "Dependency tangles"
GROUP = "structure"
NEEDS = {"deps"}
PARAMS = {}



def run(ctx, params) -> list:
    return tangle_findings(ctx.model)
