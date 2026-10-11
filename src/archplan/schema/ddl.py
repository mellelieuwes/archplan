"""A tolerant SQL DDL reader for schema.sql, structure.sql and
`pg_dump --schema-only` output (PostgreSQL, MySQL and SQLite dialects).

It reads CREATE TABLE, ALTER TABLE (ADD CONSTRAINT, ADD COLUMN, ALTER COLUMN,
DROP COLUMN, RENAME), CREATE TYPE ... AS ENUM, ALTER TYPE ... ADD VALUE,
CREATE INDEX and COMMENT ON. Other statements, such as functions, views and
grants, are skipped. A statement it cannot read is skipped, not an error."""

from __future__ import annotations

import re

from .core import add_check, add_fk, add_index, drop_col, find_col, get_table, set_col, set_pk

# An unclosed comment, string or quoted name runs to the end of the text (the
# \Z alternatives), and a [bracketed] name is at most 128 characters: without
# that, the regex would scan to the end again from every opening delimiter,
# which is quadratic on hostile input.
TOKEN = re.compile(r"""
    (?P<space>\s+|--[^\n]*|/\*.*?(?:\*/|\Z)|\#[^\n]*)
  | (?P<dollar>\$(?P<tag>[A-Za-z_]*)\$.*?(?:\$(?P=tag)\$|\Z))
  | (?P<str>[EeNnBbXx]?'(?:[^'\\]|''|\\.)*(?:'|\\?\Z))
  | (?P<qid>"(?:[^"]|"")*(?:"|\Z)|`[^`]*(?:`|\Z)|\[[A-Za-z_][^\]\n]{0,127}\])
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<word>[A-Za-z_][\w$]*)
  | (?P<op>::|<>|<=|>=|!=|\|\||\S)
""", re.S | re.X)

STOP = {"NOT", "NULL", "DEFAULT", "PRIMARY", "UNIQUE", "REFERENCES", "CHECK", "CONSTRAINT", "COLLATE",
        "GENERATED", "AUTO_INCREMENT", "AUTOINCREMENT", "COMMENT", "ON", "IDENTITY", "KEY", "CHARSET"}


class Tok:
    __slots__ = ("kind", "text", "up")

    def __init__(self, kind, text):
        self.kind, self.text = kind, text
        self.up = text.upper() if kind == "word" else text

    def __repr__(self):
        return self.text


def tokens(sql: str) -> list:
    out = []
    for m in TOKEN.finditer(sql):
        k = m.lastgroup
        if k == "space":
            continue
        if k == "tag":
            k = "dollar"
        text = m.group(0)
        if k == "qid":
            text = text[1:-1].replace('""', '"')
        out.append(Tok(k if k != "dollar" else "str", text))
    return out


def statements(sql: str) -> list:
    stmts, cur = [], []
    for t in tokens(sql):
        if t.kind == "op" and t.text == ";":
            if cur:
                stmts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        stmts.append(cur)
    return stmts


def unquote(s: str) -> str:
    """The value of a SQL string literal."""
    if s[:1] in "EeNnBbXx" and s[1:2] == "'":
        s = s[1:]
    if s.startswith("$"):
        return s[s.index("$", 1) + 1: s.rindex("$", 0, len(s) - 1)]
    return s[1:-1].replace("''", "'")


def render(toks: list) -> str:
    """Tokens back to compact SQL text."""
    out = ""
    for t in toks:
        s = t.text if t.kind != "qid" else f'"{t.text}"' if not re.fullmatch(r"\w+", t.text) else t.text
        if t.kind == "word":
            s = t.text
        if not out or out[-1] in "([." or s in (")", ",", "]", "[", "::", ".") or out.endswith("::") or (
                s == "(" and out[-1:].isalnum()):
            out += s
        else:
            out += " " + s
    return out.replace(",", ", ").replace(",  ", ", ")


def split_commas(toks: list) -> list:
    parts, cur, depth = [], [], 0
    for t in toks:
        if t.kind == "op" and t.text in "([":
            depth += 1
        elif t.kind == "op" and t.text in ")]":
            depth -= 1
        if depth == 0 and t.kind == "op" and t.text == ",":
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        parts.append(cur)
    return parts


class Cursor:
    def __init__(self, toks: list):
        self.t, self.i = toks, 0

    def peek(self, k: int = 0) -> str | None:
        j = self.i + k
        return self.t[j].up if j < len(self.t) else None

    def at_end(self) -> bool:
        return self.i >= len(self.t)

    def take(self) -> Tok | None:
        if self.i < len(self.t):
            self.i += 1
            return self.t[self.i - 1]
        return None

    def accept(self, *words) -> bool:
        for k, w in enumerate(words):
            if self.peek(k) != w:
                return False
        self.i += len(words)
        return True

    def skip_any(self, *words) -> None:
        while self.peek() in words:
            self.i += 1

    def name(self) -> str | None:
        """A possibly qualified name; the public or dbo schema prefix is dropped."""
        t = self.take()
        if t is None or t.kind not in ("word", "qid"):
            return None
        parts = [t.text]
        while self.peek() == "." and self.i + 1 < len(self.t):
            self.i += 1
            parts.append(self.take().text)
        if len(parts) > 1 and parts[-2].lower() in ("public", "dbo", "main"):
            parts = parts[-1:]
        return ".".join(parts[-2:])

    def group(self) -> list | None:
        """The tokens inside the next balanced parentheses."""
        if self.peek() != "(":
            return None
        depth, start = 0, self.i
        while self.i < len(self.t):
            x = self.t[self.i]
            self.i += 1
            if x.kind == "op" and x.text == "(":
                depth += 1
            elif x.kind == "op" and x.text == ")":
                depth -= 1
                if depth == 0:
                    return self.t[start + 1: self.i - 1]
        return self.t[start + 1:]

    def until(self, stops: set) -> list:
        out, depth = [], 0
        while self.i < len(self.t):
            x = self.t[self.i]
            if depth == 0 and x.kind == "word" and x.up in stops:
                break
            if x.kind == "op" and x.text in "([":
                depth += 1
            elif x.kind == "op" and x.text in ")]":
                depth -= 1
            out.append(x)
            self.i += 1
        return out


def ident_list(toks: list) -> list:
    """Column names in a parenthesised list: the first identifier of each item."""
    out = []
    for part in split_commas(toks):
        w = [t for t in part if t.kind in ("word", "qid")]
        if w:
            out.append(w[0].text if len(part) == 1 or part[0].kind in ("word", "qid") else render(part))
    return out


def check_values(toks: list) -> tuple:
    """(column, values) when a check limits a column to a set of strings:
    `col IN ('a', 'b')` or `col = ANY (ARRAY['a', 'b'])`."""
    ups = [t.up for t in toks]
    if "IN" not in ups and "ANY" not in ups:
        return None, []
    col = next((t.text for t in toks if t.kind in ("word", "qid") and t.up not in
                ("IN", "ANY", "ARRAY", "TEXT", "VARCHAR", "CHARACTER", "VARYING", "NOT", "AND", "OR", "NULL",
                 "IS", "VALUE")), None)
    vals = [unquote(t.text) for t in toks if t.kind == "str"]
    return col, list(dict.fromkeys(vals))


class Schema:
    def __init__(self):
        self.tables, self.enums = {}, {}

    def result(self) -> dict:
        for t in self.tables.values():
            for c in t["columns"]:
                base = c["type"].rstrip("[]").split(".")[-1]
                if base in self.enums and not c.get("values"):
                    c["values"] = list(self.enums[base])
        return {"tables": self.tables, "enums": self.enums}


def table_constraint(s: Schema, table: str, cur: Cursor) -> bool:
    """Read a table constraint at the cursor. False when it is not one."""
    t = get_table(s.tables, table)
    cname = None
    if cur.accept("CONSTRAINT"):
        cname = cur.take()
        cname = cname.text if cname else None
    if cur.accept("PRIMARY", "KEY"):
        g = cur.group() or []
        set_pk(t, ident_list(g))
        return True
    if cur.peek() == "UNIQUE" or (cur.peek() in ("KEY", "INDEX") and cname is None) or cur.peek() == "FULLTEXT":
        unique = cur.peek() == "UNIQUE"
        cur.take()
        cur.skip_any("KEY", "INDEX", "NULLS", "NOT", "DISTINCT")
        if cur.peek() != "(":
            cur.take()  # an index name
        g = cur.group()
        if g is not None:
            add_index(t, ident_list(g), unique)
        return True
    if cur.accept("FOREIGN", "KEY"):
        if cur.peek() != "(":
            cur.take()
        cols = ident_list(cur.group() or [])
        references(t, cols, cur)
        return True
    if cur.accept("CHECK"):
        g = cur.group() or []
        add_check(t, (cname + ": " if cname else "") + render(g))
        col, vals = check_values(g)
        c = find_col(t, col) if col else None
        if c is not None and vals:
            c["values"] = vals
        return True
    if cur.peek() == "EXCLUDE":
        return True
    return cname is not None


def references(t: dict, cols: list, cur: Cursor) -> None:
    if not cur.accept("REFERENCES"):
        return
    to = cur.name()
    to_cols = ident_list(cur.group() or []) if cur.peek() == "(" else []
    on_delete = None
    while cur.peek() in ("ON", "MATCH", "DEFERRABLE", "INITIALLY", "NOT"):
        if cur.accept("ON", "DELETE"):
            act = [cur.take().up]
            if act[0] in ("SET", "NO"):
                act.append(cur.take().up)
            on_delete = " ".join(act)
        elif cur.accept("ON", "UPDATE"):
            if cur.take().up in ("SET", "NO"):
                cur.take()
        else:
            cur.take()
    if to:
        add_fk(t, cols, to, to_cols, on_delete)


def column_def(s: Schema, table: str, toks: list) -> None:
    if not toks or toks[0].kind not in ("word", "qid"):
        return
    t = get_table(s.tables, table)
    cur = Cursor(toks)
    name = cur.take().text
    type_toks = cur.until(STOP)
    typ = render(type_toks).lower().replace(", ", ",") if type_toks else "unknown"
    typ = re.sub(r"\s+character set \w+", "", re.sub(r"^public\.", "", typ))
    if typ.startswith("enum("):
        vals = [unquote(x.text) for x in type_toks if x.kind == "str"]
        set_col(t, name, "enum", values=vals)
    c = set_col(t, name, typ if not typ.startswith("enum(") else "enum")
    if typ in ("serial", "bigserial", "smallserial"):
        c["default"] = "autoincrement"
    while not cur.at_end():
        if cur.accept("NOT", "NULL"):
            c["null"] = False
        elif cur.accept("NULL"):
            c["null"] = True
        elif cur.accept("DEFAULT"):
            c["default"] = "NULL" if cur.accept("NULL") else render(cur.until(STOP))
        elif cur.accept("PRIMARY", "KEY"):
            set_pk(t, [name])
            cur.skip_any("ASC", "DESC", "AUTOINCREMENT")
        elif cur.accept("UNIQUE"):
            cur.skip_any("KEY")
            add_index(t, [name], True)
        elif cur.peek() == "REFERENCES":
            references(t, [name], cur)
        elif cur.accept("CHECK"):
            g = cur.group() or []
            add_check(t, render(g))
            _, vals = check_values(g)
            if vals:
                c["values"] = vals
        elif cur.accept("COMMENT"):
            x = cur.take()
            if x and x.kind == "str":
                c["comment"] = unquote(x.text)
        elif cur.accept("AUTO_INCREMENT") or cur.accept("AUTOINCREMENT"):
            c["default"] = "autoincrement"
        elif cur.accept("GENERATED"):
            rest = cur.until({"NOT", "NULL", "PRIMARY", "UNIQUE", "REFERENCES", "CHECK", "COMMENT"})
            if any(x.up == "IDENTITY" for x in rest):
                c["default"] = "identity"
        else:
            cur.take()


def create_table(s: Schema, cur: Cursor) -> None:
    cur.skip_any("IF", "NOT", "EXISTS")
    name = cur.name()
    if not name:
        return
    body = cur.group()
    if body is None:
        return  # CREATE TABLE ... AS SELECT, or PARTITION OF
    get_table(s.tables, name)
    for part in split_commas(body):
        sub = Cursor(part)
        if part and part[0].kind == "word" and part[0].up in (
                "CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "KEY", "INDEX", "FULLTEXT", "EXCLUDE",
                "SPATIAL", "LIKE"):
            if part[0].up == "SPATIAL":
                sub.take()
            if part[0].up != "LIKE":
                table_constraint(s, name, sub)
        else:
            column_def(s, name, part)
    rest = render(cur.t[cur.i:])
    m = re.search(r"COMMENT\s*=?\s*'((?:[^']|''){0,4000})'", rest, re.I)
    if m:
        s.tables[name]["comment"] = m.group(1).replace("''", "'")


def alter_table(s: Schema, cur: Cursor) -> None:
    cur.skip_any("IF", "EXISTS", "ONLY")
    name = cur.name()
    if not name:
        return
    t = get_table(s.tables, name)
    for part in split_commas(cur.t[cur.i:]):
        a = Cursor(part)
        if a.accept("ADD"):
            if a.peek() in ("CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "KEY", "INDEX"):
                table_constraint(s, name, a)
            else:
                a.skip_any("COLUMN")
                a.skip_any("IF", "NOT", "EXISTS")
                column_def(s, name, a.t[a.i:])
        elif a.accept("ALTER") or a.accept("MODIFY") or a.accept("CHANGE"):
            a.skip_any("COLUMN")
            col = a.take()
            if col is None:
                continue
            c = find_col(t, col.text)
            if c is None:
                continue
            if a.accept("SET", "DEFAULT"):
                c["default"] = render(a.t[a.i:])
            elif a.accept("SET", "NOT", "NULL"):
                c["null"] = False
            elif a.accept("DROP", "NOT", "NULL"):
                c["null"] = True
            elif a.accept("DROP", "DEFAULT"):
                c.pop("default", None)
            elif a.accept("SET", "DATA", "TYPE") or a.accept("TYPE"):
                c["type"] = render(a.until({"USING", "COLLATE"})).lower()
        elif a.accept("DROP"):
            if a.accept("CONSTRAINT") or a.peek() in ("INDEX", "KEY", "FOREIGN", "PRIMARY"):
                continue
            a.skip_any("COLUMN")
            a.skip_any("IF", "EXISTS")
            col = a.take()
            if col is not None:
                drop_col(t, col.text)
        elif a.accept("RENAME"):
            if a.accept("TO"):
                new = a.name()
                if new and new != name:
                    s.tables[new] = s.tables.pop(name)
                    name, t = new, s.tables[new]
                continue
            a.skip_any("COLUMN")
            old = a.take()
            if old and a.accept("TO"):
                new = a.take()
                c = find_col(t, old.text)
                if c is not None and new is not None:
                    c["name"] = new.text
                    t["pk"] = [new.text if x == old.text else x for x in t["pk"]]


def create_index(s: Schema, cur: Cursor, unique: bool) -> None:
    cur.skip_any("CONCURRENTLY")
    cur.skip_any("IF", "NOT", "EXISTS")
    if cur.peek() != "ON":
        cur.name()
    if not cur.accept("ON"):
        return
    cur.skip_any("ONLY")
    table = cur.name()
    if cur.accept("USING"):
        cur.take()
    g = cur.group()
    if table and g is not None:
        add_index(get_table(s.tables, table), ident_list(g), unique)


def read_statement(s: Schema, toks: list) -> None:
    cur = Cursor(toks)
    if cur.accept("CREATE"):
        cur.skip_any("OR", "REPLACE", "GLOBAL", "LOCAL", "TEMPORARY", "TEMP", "UNLOGGED", "VIRTUAL")
        if cur.accept("TABLE"):
            create_table(s, cur)
        elif cur.accept("UNIQUE", "INDEX"):
            create_index(s, cur, True)
        elif cur.accept("INDEX"):
            create_index(s, cur, False)
        elif cur.accept("TYPE"):
            name = cur.name()
            if name and cur.accept("AS", "ENUM"):
                s.enums[name.split(".")[-1]] = [unquote(t.text) for t in (cur.group() or []) if t.kind == "str"]
    elif cur.accept("ALTER", "TABLE"):
        alter_table(s, cur)
    elif cur.accept("ALTER", "TYPE"):
        name = cur.name()
        if name and cur.accept("ADD", "VALUE"):
            cur.skip_any("IF", "NOT", "EXISTS")
            v = cur.take()
            if v is not None and v.kind == "str":
                s.enums.setdefault(name.split(".")[-1], []).append(unquote(v.text))
    elif cur.accept("COMMENT", "ON"):
        what = cur.take()
        if what is None or what.up not in ("TABLE", "COLUMN"):
            return
        target = cur.name()
        if not target or not cur.accept("IS"):
            return
        v = cur.take()
        if v is None or v.kind != "str":
            return
        if what.up == "TABLE" and target in s.tables:
            s.tables[target]["comment"] = unquote(v.text)
        elif what.up == "COLUMN" and "." in target:
            tbl, col = target.rsplit(".", 1)
            tbl = tbl.split(".")[-1] if tbl.split(".")[0].lower() == "public" else tbl
            c = find_col(s.tables.get(tbl) or {"columns": []}, col)
            if c is not None:
                c["comment"] = unquote(v.text)
    elif cur.accept("DROP", "TABLE"):
        cur.skip_any("IF", "EXISTS")
        n = cur.name()
        s.tables.pop(n, None)


def parse(sql: str, s: Schema | None = None) -> dict:
    """{"tables", "enums"} from DDL text."""
    s = s or Schema()
    for st in statements(sql):
        try:
            read_statement(s, st)
        except (IndexError, AttributeError, KeyError, ValueError):
            continue
    return s.result()
