"""Shared table structures for the schema extractors.

A table is {"columns": [{"name", "type", "null", "default"?, "values"?}],
"pk": [...], "fks": [{"columns", "to", "to_columns", "on_delete"?}],
"indexes": [{"columns", "unique"}], "checks": [...], "comment"?}."""

from __future__ import annotations

import re


def new_table() -> dict:
    return {"columns": [], "pk": [], "fks": [], "indexes": [], "checks": []}


def get_table(tables: dict, name: str) -> dict:
    if name not in tables:
        tables[name] = new_table()
    return tables[name]


def find_col(t: dict, name: str) -> dict | None:
    for c in t["columns"]:
        if c["name"] == name:
            return c
    return None


def set_col(t: dict, name: str, type: str | None = None, null: bool | None = None, default=None,
            values=None, replace: bool = True) -> dict:
    """Add a column, or update the one with this name. With replace=False only
    missing facts are filled in."""
    c = find_col(t, name)
    if c is None:
        c = {"name": name, "type": type or "unknown", "null": True if null is None else null}
        if null is None:
            c["_guess"] = True  # nullability unknown: a later source may say
        t["columns"].append(c)
        replace = True
    if type and (replace or c.get("type") in (None, "unknown")):
        c["type"] = type
    if null is not None and (replace or c.get("_guess")):
        c["null"] = null
        c.pop("_guess", None)
    if default is not None and (replace or "default" not in c):
        c["default"] = str(default)
    if values and (replace or not c.get("values")):
        c["values"] = [str(v) for v in values]
    return c


def drop_col(t: dict, name: str) -> None:
    t["columns"] = [c for c in t["columns"] if c["name"] != name]
    t["pk"] = [c for c in t["pk"] if c != name]
    t["fks"] = [f for f in t["fks"] if name not in f["columns"]]
    t["indexes"] = [i for i in t["indexes"] if name not in i["columns"]]


def set_pk(t: dict, cols: list) -> None:
    t["pk"] = list(cols)
    for n in cols:
        c = find_col(t, n)
        if c is not None:
            c["null"] = False


def add_fk(t: dict, cols: list, to: str, to_cols: list | None = None, on_delete: str | None = None) -> None:
    cols = list(cols)
    for f in t["fks"]:
        if f["columns"] == cols and f["to"] == to:
            if on_delete and not f.get("on_delete"):
                f["on_delete"] = on_delete
            if to_cols and not f.get("to_columns"):
                f["to_columns"] = list(to_cols)
            return
    fk = {"columns": cols, "to": to, "to_columns": list(to_cols or [])}
    if on_delete:
        fk["on_delete"] = on_delete.upper().replace("_", " ")
    t["fks"].append(fk)


def add_index(t: dict, cols: list, unique: bool = False) -> None:
    cols = list(cols)
    for i in t["indexes"]:
        if i["columns"] == cols:
            i["unique"] = i["unique"] or unique
            return
    t["indexes"].append({"columns": cols, "unique": bool(unique)})


def add_check(t: dict, text: str) -> None:
    if text and text not in t["checks"]:
        t["checks"].append(text)


def merge_table(dst: dict, src: dict) -> None:
    """Fill dst with what src knows and dst does not."""
    for c in src["columns"]:
        set_col(dst, c["name"], c.get("type"), None if c.get("_guess") else c.get("null"), c.get("default"),
                c.get("values"), replace=False)
    if not dst["pk"] and src["pk"]:
        dst["pk"] = list(src["pk"])
    for f in src["fks"]:
        add_fk(dst, f["columns"], f["to"], f.get("to_columns"), f.get("on_delete"))
    for i in src["indexes"]:
        add_index(dst, i["columns"], i["unique"])
    for x in src["checks"]:
        add_check(dst, x)
    if src.get("comment") and not dst.get("comment"):
        dst["comment"] = src["comment"]


def finish(tables: dict) -> None:
    """Drop the working marks from the columns."""
    for t in tables.values():
        for c in t["columns"]:
            c.pop("_guess", None)


# ---------- column kinds, for detectors ----------

def is_json(type_: str) -> bool:
    t = (type_ or "").lower()
    return t in ("json", "jsonb", "map", "hstore") or t.startswith(("json", "embed")) or "jsonfield" in t


def is_bool(type_: str) -> bool:
    t = (type_ or "").lower()
    return t in ("bool", "boolean", "tinyint(1)") or t.startswith("bool")


def is_timestamp(type_: str) -> bool:
    t = (type_ or "").lower()
    return t.startswith(("timestamp", "datetime", "utc_datetime", "naive_datetime", "date")) and t != "date"


def snake(name: str) -> str:
    name = name[:200]  # a class or table name; the second pattern is quadratic on long capital runs
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s).lower()


def plural(word: str) -> str:
    """A naive English plural, as Rails derives table names."""
    if re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    if word.endswith("person"):
        return word[:-6] + "people"
    return word + "s"
