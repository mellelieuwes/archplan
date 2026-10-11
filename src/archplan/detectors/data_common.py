"""Helpers for the data detectors, which read the model's schema section:
table owners, who reads and writes a table, and layer ranks."""

from __future__ import annotations

from collections import defaultdict

from .common import COMMON_FIELDS, namer

TIMESTAMPS = ("timestamp", "datetime", "utc_datetime", "naive_datetime", "timestamptz")


def schema(ctx) -> dict:
    return ctx.model.get("schema") or {}


def tables(ctx) -> dict:
    return schema(ctx).get("tables") or {}


def owner(ctx, table: str) -> str | None:
    return ((schema(ctx).get("maps") or {}).get(table) or {}).get("component")


def users(ctx, table: str, modes=("orm", "read", "write")) -> dict:
    """{component: weight} for the components that use a table in these modes."""
    out = defaultdict(int)
    for a in schema(ctx).get("access") or []:
        if a.get("table") == table and a.get("mode") in modes:
            out[a["component"]] += a.get("weight") or 1
    return dict(out)


def access_by_table(ctx) -> dict:
    out = defaultdict(list)
    for a in schema(ctx).get("access") or []:
        out[a.get("table")].append(a)
    return out


def name(ctx, cid: str) -> str:
    return namer(ctx.model)(cid) if cid else "?"


def layer_rank(ctx) -> dict:
    """{component: index} from the profile's layers, outermost first; empty
    without a profile with two or more layers."""
    try:
        prof = ctx.profile[0]
    except Exception:  # noqa: BLE001 - no profile, or no project behind the context
        prof = getattr(ctx, "profile_dict", None) or {}
    layers = [x for x in (prof or {}).get("layers") or [] if x.get("components")]
    if len(layers) < 2:
        return {}
    return {c: (i, x.get("name") or x.get("id")) for i, x in enumerate(layers) for c in x["components"]}


def is_json(c: dict) -> bool:
    t = (c.get("type") or "").lower()
    return t in ("json", "jsonb", "map", "hstore", "json[]", "jsonb[]") or t.startswith("json")


def is_bool(c: dict) -> bool:
    t = (c.get("type") or "").lower()
    return t.startswith("bool") or t == "tinyint(1)"


def is_stamp(c: dict) -> bool:
    t = (c.get("type") or "").lower()
    return t.startswith(TIMESTAMPS)


def plain_columns(t: dict) -> set:
    return {c["name"] for c in t.get("columns") or []} - COMMON_FIELDS
