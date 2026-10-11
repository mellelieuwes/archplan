"""Read a Prisma schema (schema.prisma): models, @@map table names, @map
column names, relations with fields and references, @id, @@id, @unique,
@@unique, @@index and enums."""

from __future__ import annotations

import itertools
import re

from .core import add_fk, add_index, get_table, set_col, set_pk, snake

TYPES = {"String": "text", "Int": "integer", "BigInt": "bigint", "Float": "double precision",
         "Decimal": "numeric", "Boolean": "boolean", "DateTime": "timestamp", "Json": "jsonb", "Bytes": "bytea"}
ARGS_MAX = 1000  # characters of an attribute's arguments; bounds the work on unclosed brackets
MAX_LINE = 2000  # characters; a longer line in a block is left out
MAX_ATTRS = 500  # @@id, @@unique and @@index attributes read per model
PARENS = re.compile(r"[()]")
HEAD = re.compile(r"^[ \t]*(model|enum|view)[ \t]+(\w+)[ \t]*\{", re.M)


def blocks(text: str):
    """(kind, name, body, line) for each model, enum and view block. The body
    ends at the brace that closes the block, found by counting braces outside
    strings; an unclosed block ends the scan. Linear in the text."""
    pos, line, at = 0, 1, 0
    while True:
        m = HEAD.search(text, pos)
        if not m:
            return
        line += text.count("\n", at, m.start())
        at = m.start()
        i, depth, quoted, n = m.end(), 1, False, len(text)
        while i < n:
            c = text[i]
            if quoted:
                if c == "\\":
                    i += 1
                elif c == '"' or c == "\n":
                    quoted = False
            elif c == '"':
                quoted = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if not depth:
                    break
            i += 1
        if i >= n:
            return
        yield m.group(1), m.group(2), text[m.end():i], line
        pos = i + 1


def strip_comments(text: str) -> str:
    return re.sub(r"//[^\n]*", "", text)


def args(attr: str, name: str) -> str | None:
    """The text inside @name(...) or @@name(...)."""
    m = re.search(rf"@{name}\(", attr)
    if not m:
        return None
    depth, end = 1, len(attr) + 1
    for p in PARENS.finditer(attr, m.end(), m.end() + ARGS_MAX):
        depth += 1 if p.group() == "(" else -1
        if not depth:
            end = p.end()
            break
    return attr[m.end(): end - 1]


def names(text: str | None) -> list:
    if not text:
        return []
    i = text.find("[")
    j = text.find("]", i + 1) if i >= 0 else -1
    inner = text[i + 1:j] if j >= 0 else text  # the first [...], as the regex \[([^\]]*)\] found it
    return [x.strip().split("(")[0] for x in inner.split(",") if x.strip()]


def kwarg(text: str | None, key: str) -> str | None:
    if not text:
        return None
    m = re.search(rf"\b{key}\s*:\s*(\[[^\]]{{0,{ARGS_MAX}}}\]|\"[^\"]{{0,{ARGS_MAX}}}\"|\w+)", text)
    return m.group(1).strip('"') if m else None


def read(text: str, rel: str = "schema.prisma") -> dict:
    text = strip_comments(text)
    enums, model_blocks = {}, {}
    for kind, name, body, line in blocks(text):
        if kind == "enum":
            enums[name] = [ln.split()[0] for ln in body.split("\n") if ln.strip() and not ln.strip().startswith("@")]
        elif kind == "model":
            model_blocks[name] = (body, line)
    table_of = {}
    for name, (body, _) in model_blocks.items():
        mm = re.search(r"@@map\(\s*\"([^\"]+)\"", body)
        table_of[name] = mm.group(1) if mm else name
    tables, maps = {}, {}
    for name, (body, line) in model_blocks.items():
        tname = table_of[name]
        t = get_table(tables, tname)
        maps[tname] = {"class": name, "file": rel, "line": line}
        colname = {}
        rels = []
        lines = [ln for ln in body.split("\n") if len(ln) <= MAX_LINE]
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith("@@"):
                continue
            parts = ln.split(None, 2)
            if len(parts) < 2:
                continue
            fname, ftype = parts[0], parts[1]
            attrs = parts[2] if len(parts) > 2 else ""
            base = ftype.rstrip("?[]")
            if base in model_blocks:
                if "@relation" in attrs and kwarg(args(attrs, "relation"), "fields"):
                    rels.append((attrs, base))
                continue
            cm = re.search(r"@map\(\s*\"([^\"]+)\"", attrs)
            col = cm.group(1) if cm else fname
            colname[fname] = col
            typ = TYPES.get(base, base if base not in enums else snake(base))
            if ftype.endswith("[]"):
                typ += "[]"
            dm = args(attrs, "default")
            c = set_col(t, col, typ, ftype.endswith("?"), dm if dm is not None else None,
                        enums.get(base))
            if dm and "autoincrement" in dm:
                c["default"] = "autoincrement"
            if re.search(r"@id\b", attrs):
                set_pk(t, [col])
            if re.search(r"@unique\b", attrs):
                add_index(t, [col], True)
        col = lambda f: colname.get(f, f)
        for attrs, target in rels:
            r = args(attrs, "relation")
            fields = names(kwarg(r, "fields"))
            refs = names(kwarg(r, "references"))
            od = kwarg(r, "onDelete")
            add_fk(t, [col(f) for f in fields], table_of[target], refs,
                   {"Cascade": "CASCADE", "SetNull": "SET NULL", "Restrict": "RESTRICT", "NoAction": "NO ACTION",
                    "SetDefault": "SET DEFAULT"}.get(od or ""))
        for kind, unique in (("id", None), ("unique", True), ("index", False)):
            found = ((ln, m) for ln in lines for m in re.finditer(rf"@@{kind}\(", ln))
            for ln, m in itertools.islice(found, MAX_ATTRS):
                a = args(ln[m.start():], kind)
                cols = [col(x) for x in names(a)]
                if kind == "id":
                    set_pk(t, cols)
                else:
                    add_index(t, cols, unique)
    return {"tables": tables, "enums": {snake(k): v for k, v in enums.items()}, "maps": maps}
