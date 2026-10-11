"""Export a Python project's as-is model (archmodel/1) with the standard library.

Components are packages (directories with __init__.py) and top-level modules.
Dependencies are imports between them. Types are top-level classes, with
annotated fields (and ORM fields such as Django's models.CharField), public
methods and base classes. Forbidden contracts from
import-linter become deny rules. The files section lists, per module file, the
project files it imports. Tests and Django migrations are left out.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

SKIP = {".git", ".hg", ".venv", "venv", "env", "node_modules", "build", "dist", "__pycache__", ".tox", ".nox",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", ".archplan", ".ignored", "migrations"}
MANY = {"list", "List", "Sequence", "MutableSequence", "Iterable", "Iterator", "set", "Set", "frozenset",
        "FrozenSet", "tuple", "Tuple", "dict", "Dict", "Mapping", "MutableMapping", "Collection", "deque"}
OPTIONAL = {"Optional", "Annotated", "ClassVar", "Final"}
INTERFACE_BASES = {"Protocol", "ABC", "ABCMeta"}
RELATIONS = {"ForeignKey", "OneToOneField", "ManyToManyField"}


def orm_field(x: ast.stmt) -> bool:
    """A class-level `name = models.CharField(...)` style field (Django and
    similar ORMs): a call to something named *Field, or a relation."""
    if not (isinstance(x, ast.Assign) and len(x.targets) == 1 and isinstance(x.targets[0], ast.Name)
            and isinstance(x.value, ast.Call)):
        return False
    head = ast.unparse(x.value.func).split(".")[-1]
    return head.endswith("Field") or head in RELATIONS or head == "GenericForeignKey"
ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
RECORD_BASES = {"NamedTuple", "TypedDict", "BaseModel"}


def source_root(root: Path) -> Path:
    return root / "src" if (root / "src").is_dir() else root


TESTS = {"tests", "test", "testing"}


def files(base: Path, tests: bool = False):
    for p in sorted(base.rglob("*.py")):
        rel = p.relative_to(base).parts
        if any(x in SKIP or x.startswith(".") or (not tests and x in TESTS) for x in rel[:-1]):
            continue
        if not tests and (rel[-1].startswith("test_") or rel[-1] == "conftest.py"):
            continue
        yield p


def module_name(base: Path, f: Path) -> str | None:
    """The dotted module name of f, or None when a directory on the way has no
    __init__.py (then the file is a script, not part of a package)."""
    rel = f.relative_to(base).with_suffix("").parts
    for i in range(1, len(rel)):
        if not (base.joinpath(*rel[:i]) / "__init__.py").is_file():
            return None
    parts = rel[:-1] if rel[-1] == "__init__" else rel
    return ".".join(parts) if parts else None


def resolve_relative(mod: str, is_pkg: bool, level: int, name: str | None) -> str:
    parts = mod.split(".")
    if not is_pkg:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    return ".".join([*parts, name] if name else parts)


def git_ref(root: Path) -> tuple[str, bool]:
    try:
        ref = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                             timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
                                    timeout=30).stdout.strip())
        return ref or "?", dirty
    except (OSError, subprocess.SubprocessError):
        return "?", False


def import_linter_rules(root: Path) -> list:
    """Deny rules from import-linter 'forbidden' contracts (pyproject or .importlinter)."""
    rules = []
    cfg = root / "pyproject.toml"
    if cfg.is_file() and sys.version_info >= (3, 11):
        import tomllib
        try:
            data = tomllib.loads(cfg.read_text())
        except tomllib.TOMLDecodeError:
            data = {}
        for c in data.get("tool", {}).get("importlinter", {}).get("contracts", []):
            if c.get("type") == "forbidden":
                for s in c.get("source_modules", []):
                    for t in c.get("forbidden_modules", []):
                        rules.append({"from": s, "to": t, "effect": "deny", "note": c.get("name", "import-linter")})
    ini = root / ".importlinter"
    if ini.is_file():
        import configparser
        cp = configparser.ConfigParser()
        cp.read(ini)
        for sec in cp.sections():
            if cp[sec].get("type") == "forbidden":
                srcs = cp[sec].get("source_modules", "").split()
                dsts = cp[sec].get("forbidden_modules", "").split()
                rules += [{"from": s, "to": t, "effect": "deny", "note": cp[sec].get("name", sec)} for s in srcs for t in dsts]
    return rules


class Module:
    def __init__(self, name: str, path: Path, rel: str, tree: ast.Module, is_pkg: bool):
        self.name, self.path, self.rel, self.tree, self.is_pkg = name, path, rel, tree, is_pkg
        self.names: dict[str, str] = {}  # local name -> qualified name, from imports
        self.imports: list[tuple[str, int]] = []  # (imported module or symbol, line)


def export(root: Path, packages: list[str] | None = None, granularity: str = "auto", tests: bool = False) -> dict:
    """granularity: 'package' (one component per package), 'module' (one per
    module) or 'auto' (modules when the project has fewer than three packages)."""
    base = source_root(root)
    mods: dict[str, Module] = {}
    for f in files(base, tests):
        name = module_name(base, f)
        if not name or (packages and name.split(".")[0] not in packages):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"), str(f))
        except SyntaxError:
            continue
        mods[name] = Module(name, f, str(f.relative_to(root)), tree, f.name == "__init__.py")

    # Components: every package, plus top-level modules outside packages.
    pkgs = {m.name for m in mods.values() if m.is_pkg}
    if granularity == "auto":
        granularity = "module" if len(pkgs) < 3 else "package"
    comp_of_mod = {}
    for m in mods.values():
        if m.is_pkg or granularity == "module":
            comp_of_mod[m.name] = m.name
        else:
            parent = m.name.rpartition(".")[0]
            comp_of_mod[m.name] = parent if parent in pkgs else m.name

    def comp_for(qual: str) -> str | None:
        """The component of a module or a symbol inside one."""
        q = qual
        while q:
            if q in comp_of_mod:
                return comp_of_mod[q]
            q = q.rpartition(".")[0]
        return None

    # Imports.
    for m in mods.values():
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    m.imports.append((a.name, node.lineno))
                    m.names[(a.asname or a.name).split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                src = resolve_relative(m.name, m.is_pkg, node.level, node.module) if node.level else (node.module or "")
                for a in node.names:
                    if a.name == "*":
                        m.imports.append((src, node.lineno))
                        continue
                    full = f"{src}.{a.name}" if src else a.name
                    m.names[a.asname or a.name] = full
                    m.imports.append((full if full in mods else src, node.lineno))

    # Types: top-level classes.
    types = {}
    class_ids = {}
    for m in mods.values():
        for node in m.tree.body:
            if isinstance(node, ast.ClassDef):
                class_ids[f"{m.name}.{node.name}"] = node

    def qualify(m: Module, name: str) -> str | None:
        """The type id a name in module m refers to, when it is a class in the model."""
        head, _, tail = name.partition(".")
        cand = []
        if head in m.names:
            cand.append(m.names[head] + ("." + tail if tail else ""))
        cand.append(f"{m.name}.{name}")
        if m.is_pkg is False:
            cand.append(f"{m.name.rpartition('.')[0]}.{name}")
        for c in cand:
            if c in class_ids:
                return c
            # a re-export: package.Name where the class lives in package.module
            mod_name, _, cls = c.rpartition(".")
            if mod_name in mods:
                for n, q in mods[mod_name].names.items():
                    if n == cls and q in class_ids:
                        return q
        return None

    def refs(m: Module, ann: ast.expr | None) -> tuple[list[str], bool]:
        """Model types named in an annotation, and whether it is a collection."""
        if ann is None:
            return [], False
        many = False
        node = ann
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return [], False
        if isinstance(node, ast.Subscript):
            head = ast.unparse(node.value).split(".")[-1]
            many = head in MANY
        out = []
        for n in ast.walk(node):
            if isinstance(n, (ast.Name, ast.Attribute)):
                q = qualify(m, ast.unparse(n))
                if q and q not in out:
                    out.append(q)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                q = qualify(m, n.value)
                if q and q not in out:
                    out.append(q)
        return out, many

    for m in mods.values():
        for node in m.tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            tid = f"{m.name}.{node.name}"
            bases = [ast.unparse(b) for b in node.bases]
            short = {b.split(".")[-1].split("[")[0] for b in bases}
            decos = {ast.unparse(d).split("(")[0].split(".")[-1] for d in node.decorator_list}
            has_abstract = any(isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                               and any(ast.unparse(d).endswith("abstractmethod") for d in x.decorator_list)
                               for x in node.body)
            kind = ("interface" if short & INTERFACE_BASES or has_abstract else
                    "enum" if short & ENUM_BASES else
                    "record" if short & RECORD_BASES or "dataclass" in decos else "class")
            fields, methods, uses = [], [], []
            seen = set()

            def field(name, ann):
                if name in seen or ann is None:
                    return
                seen.add(name)
                rs, many = refs(m, ann)
                if len(rs) == 1:
                    fields.append({"name": name, "ref": rs[0], **({"many": True} if many else {})})
                else:
                    fields.append({"name": name, "type": ast.unparse(ann)})

            for x in node.body:
                if isinstance(x, ast.AnnAssign) and isinstance(x.target, ast.Name):
                    field(x.target.id, x.annotation)
                elif orm_field(x):
                    name, call = x.targets[0].id, x.value
                    if name not in seen:
                        seen.add(name)
                        kind_ = ast.unparse(call.func).split(".")[-1]
                        target = call.args[0] if call.args and kind_ in RELATIONS else None
                        q = qualify(m, ast.unparse(target)) if isinstance(target, (ast.Name, ast.Attribute)) else None
                        if q:
                            fields.append({"name": name, "ref": q, **({"many": True} if kind_ == "ManyToManyField" else {})})
                        else:
                            arg = f"({target.value})" if isinstance(target, ast.Constant) and isinstance(target.value, str) else ""
                            fields.append({"name": name, "type": kind_ + arg})
                elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if x.name == "__init__":
                        for y in ast.walk(x):
                            if (isinstance(y, ast.AnnAssign) and isinstance(y.target, ast.Attribute)
                                    and isinstance(y.target.value, ast.Name) and y.target.value.id == "self"):
                                field(y.target.attr, y.annotation)
                    if x.name.startswith("_") and x.name not in ("__init__", "__call__"):
                        continue
                    if any(mm["name"] == x.name for mm in methods):
                        continue  # a property setter or an overload: keep the first
                    args = x.args
                    params = [a for a in args.posonlyargs + args.args + args.kwonlyargs if a.arg not in ("self", "cls")]
                    sig = "(" + ", ".join(a.arg + (": " + ast.unparse(a.annotation) if a.annotation else "")
                                          for a in params) + ")"
                    if x.returns is not None:
                        sig += " -> " + ast.unparse(x.returns)
                    methods.append({"name": x.name, "sig": sig})
                    for a in [*params, args.vararg, args.kwarg]:
                        if a is not None:
                            uses += refs(m, a.annotation)[0]
                    uses += refs(m, x.returns)[0]
            implements = [q for b in node.bases if (q := qualify(m, ast.unparse(b).split("[")[0]))]
            frefs = {f.get("ref") for f in fields}
            uses = [u for u in dict.fromkeys(uses) if u != tid and u not in frefs and u not in implements]
            doc = ast.get_docstring(node)
            types[tid] = {"name": node.name, "component": comp_of_mod[m.name], "kind": kind, "file": m.rel,
                          "line": node.lineno, **({"doc": doc.strip().splitlines()[0]} if doc else {}),
                          "fields": fields, "methods": methods, "implements": implements, "uses": uses}

    # Dependencies.
    edges = defaultdict(lambda: {"files": set(), "evidence": []})
    for m in mods.values():
        a = comp_of_mod[m.name]
        for target, line in m.imports:
            b = comp_for(target)
            if not b or b == a:
                continue
            e = edges[(a, b)]
            if m.rel not in e["files"]:
                e["files"].add(m.rel)
                if len(e["evidence"]) < 5:
                    e["evidence"].append(f"{m.rel}:{line}")
    deps = [{"from": a, "to": b, "weight": len(e["files"]), "evidence": e["evidence"]}
            for (a, b), e in sorted(edges.items())]

    def mod_for(qual: str) -> str | None:
        """The module a module or symbol name lives in."""
        q = qual
        while q:
            if q in mods:
                return q
            q = q.rpartition(".")[0]
        return None

    file_refs = {}
    for m in mods.values():
        refs_out, seen = [], {m.rel}
        for target, line in m.imports:
            t = mod_for(target)
            if t and mods[t].rel not in seen:
                seen.add(mods[t].rel)
                refs_out.append({"to": mods[t].rel, "line": line})
        file_refs[m.rel] = {"component": comp_of_mod[m.name], "refs": refs_out}

    comps = {}
    for cid in sorted(set(comp_of_mod.values())):
        m = mods[cid]
        path = str(m.path.parent.relative_to(root)) if m.is_pkg else m.rel
        doc = ast.get_docstring(m.tree)
        comps[cid] = {"name": cid.split(".")[-1], "path": path,
                      "match": [path + ("/**" if m.is_pkg else "")], "kind": "internal",
                      **({"doc": doc.strip().splitlines()[0]} if doc else {})}

    rules = [r for r in import_linter_rules(root) if comp_for(r["from"]) and comp_for(r["to"])]
    for r in rules:
        r["from"], r["to"] = comp_for(r["from"]), comp_for(r["to"])
    ref, dirty = git_ref(root)
    return {
        "format": "archmodel/1",
        "source": {"repo": root.name, "ref": ref, "dirty": dirty, "languages": ["python"],
                   "generator": "archplan export-python",
                   "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "commands": {"test": "python -m pytest"}},
        "layers": [],
        "components": comps,
        "dependencies": deps,
        "files": dict(sorted(file_refs.items())),
        "policy": {"default": "allow", "rules": rules,
                   **({"enforcer": {"tool": "import-linter", "command": "lint-imports"}} if rules else {})},
        "types": types,
        "sequences": {},
    }
