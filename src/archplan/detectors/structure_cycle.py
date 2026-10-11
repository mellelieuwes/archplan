"""structure.cycle: two or three components that depend on each other in a cycle, with the
cheapest cut. Cycles in larger strongly connected components are tangles."""

from __future__ import annotations

from .cycles import MAX_CYCLES, small_cycle_findings

ID = "structure.cycle"
NAME = "Dependency cycles"
GROUP = "structure"
NEEDS = {"deps"}
PARAMS = {"max_cycles": MAX_CYCLES}



def run(ctx, params) -> list:
    return small_cycle_findings(ctx.model, max_cycles=params["max_cycles"])
