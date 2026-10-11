"""Optional live introspection. Never automatic.

sqlite: the stdlib sqlite3 module opens the file read-only.
postgres: the psql CLI, with the connection URL read from an environment
variable the user names. The URL is split into PG* variables for psql, so it
is not on the command line, and it is never stored or printed."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import ddl
from .core import add_fk, add_index, get_table, merge_table, set_col, set_pk


class LiveError(Exception):
    pass


def sqlite_uri(path: Path) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sqlite(path: Path) -> dict:
    if not path.is_file():
        raise LiveError(f"no such file: {path}")
    try:
        # as_uri percent-encodes the path, so a name holding ?mode=rwc cannot
        # change the mode.
        con = sqlite3.connect(sqlite_uri(path), uri=True)
    except sqlite3.Error as e:
        raise LiveError(f"cannot open {path}: {e}") from None
    tables = {}
    try:
        rows = con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                           "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for name, sql in rows:
            t = get_table(tables, name)
            pk = []
            for _, col, typ, notnull, dflt, ispk in con.execute(f'PRAGMA table_info({ident(name)})'):
                set_col(t, col, (typ or "unknown").lower(), not notnull and not ispk,
                        dflt if dflt is not None else None)
                if ispk:
                    pk.append((ispk, col))
            if pk:
                set_pk(t, [c for _, c in sorted(pk)])
            fks = {}
            for row in con.execute(f'PRAGMA foreign_key_list({ident(name)})'):
                fid, _, to, frm, to_col, _, on_delete = row[:7]
                f = fks.setdefault(fid, {"to": to, "cols": [], "to_cols": [], "on_delete": on_delete})
                f["cols"].append(frm)
                if to_col:
                    f["to_cols"].append(to_col)
            for f in fks.values():
                add_fk(t, f["cols"], f["to"], f["to_cols"],
                       f["on_delete"] if f["on_delete"] not in (None, "NO ACTION") else None)
            for _, iname, unique, origin, *_ in con.execute(f'PRAGMA index_list({ident(name)})'):
                if origin == "pk":
                    continue
                cols = [r[2] for r in con.execute(f'PRAGMA index_info({ident(iname)})') if r[2]]
                if cols:
                    add_index(t, cols, bool(unique))
            if sql:  # checks, from the table's own DDL
                parsed = ddl.parse(sql)["tables"].get(name)
                if parsed:
                    t["checks"] = parsed["checks"]
                    merge_table(t, parsed)
    except sqlite3.Error as e:
        raise LiveError(f"cannot read {path}: {e}") from None
    finally:
        con.close()
    return {"tables": tables, "enums": {}}


PG_QUERY = r"""
WITH t AS (
  SELECT c.oid, c.relname AS name, n.nspname AS schema, obj_description(c.oid, 'pg_class') AS comment
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg_toast%' AND NOT c.relispartition
)
SELECT json_build_object(
 'tables', COALESCE((SELECT json_agg(json_build_object(
   'name', CASE WHEN t.schema = 'public' THEN t.name ELSE t.schema || '.' || t.name END,
   'comment', t.comment,
   'columns', (SELECT json_agg(json_build_object('name', a.attname,
        'type', format_type(a.atttypid, a.atttypmod), 'null', NOT a.attnotnull,
        'default', pg_get_expr(d.adbin, d.adrelid)) ORDER BY a.attnum)
      FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
      WHERE a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped),
   'constraints', (SELECT json_agg(pg_get_constraintdef(k.oid)) FROM pg_constraint k WHERE k.conrelid = t.oid),
   'indexes', (SELECT json_agg(pg_get_indexdef(i.indexrelid)) FROM pg_index i
      WHERE i.indrelid = t.oid AND NOT i.indisprimary)
 )) FROM t), '[]'::json),
 'enums', COALESCE((SELECT json_object_agg(ty.typname, (SELECT json_agg(e.enumlabel ORDER BY e.enumsortorder)
      FROM pg_enum e WHERE e.enumtypid = ty.oid))
   FROM pg_type ty JOIN pg_namespace n ON n.oid = ty.typnamespace
   WHERE ty.typtype = 'e' AND n.nspname NOT IN ('pg_catalog', 'information_schema')), '{}'::json)
)
"""


def pg_env(url: str) -> dict:
    """PG* variables for psql from a postgres:// URL."""
    u = urlparse(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise LiveError("the URL must start with postgres:// or postgresql://")
    env = {}
    if u.hostname:
        env["PGHOST"] = unquote(u.hostname)
    if u.port:
        env["PGPORT"] = str(u.port)
    if u.username:
        env["PGUSER"] = unquote(u.username)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    if u.path and u.path != "/":
        env["PGDATABASE"] = unquote(u.path.lstrip("/"))
    q = parse_qs(u.query)
    for k, var in (("sslmode", "PGSSLMODE"), ("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"),
                   ("password", "PGPASSWORD"), ("dbname", "PGDATABASE"), ("sslrootcert", "PGSSLROOTCERT")):
        if q.get(k):
            env[var] = q[k][0]
    return env


def postgres(url_env: str, timeout: float = 60) -> dict:
    url = os.environ.get(url_env or "")
    if not url:
        raise LiveError(f"the environment variable {url_env!r} is not set")
    psql = shutil.which("psql")
    if not psql:
        raise LiveError("psql is not on the PATH")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG") and k != url_env}
    env.update(pg_env(url))
    try:
        r = subprocess.run([psql, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", PG_QUERY], env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LiveError("psql timed out") from None
    if r.returncode != 0:
        # psql's message can echo the host or user, never the password; strip the URL anyway
        msg = r.stderr.strip().replace(url, "<url>")
        raise LiveError(f"psql failed: {msg[-500:]}")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise LiveError("psql did not return JSON") from None
    return from_catalog(data)


def from_catalog(data: dict) -> dict:
    """The schema from the JSON that PG_QUERY returns."""
    s = ddl.Schema()
    s.enums = {k: v or [] for k, v in (data.get("enums") or {}).items()}
    for x in data.get("tables") or []:
        name = x["name"]
        t = get_table(s.tables, name)
        for c in x.get("columns") or []:
            set_col(t, c["name"], c["type"], c["null"], c.get("default"))
        if x.get("comment"):
            t["comment"] = x["comment"]
        stmts = [f"ALTER TABLE {quote(name)} ADD {d};" for d in x.get("constraints") or []]
        stmts += [d + ";" for d in x.get("indexes") or []]
        ddl.parse("\n".join(stmts), s)
    return s.result()


def quote(name: str) -> str:
    return ".".join(f'"{p}"' for p in name.split("."))
