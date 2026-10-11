"""A project is a code repository with an .archplan directory.

The directory holds everything archplan shares between the human (in the
editor) and an agent (over MCP):

  config.json            how to export the code's model, which port to use
  model.archmodel.json   the as-is model, written by the exporter
  target.archmodel.json  the target model the human (or agent) edits
  base.archmodel.json    the as-is model the target was last edited against
  state.json             target revision, who wrote it last, the plan intent
  plan.md, plan.json     the change plan, written by the editor
  threads.json           comment threads between human and agent
  suggestions.json       target edits the agent proposed, and the human's decisions
  handovers.json         planned refactors the human sent the agent, with batches and progress
  activity.json          the agent's latest MCP call, for the editor; not committed
  hotspots.json          cached analysis of the git history; not committed
  architecture.json      the architecture profile: units, roles, rules, layers
  baseline.json          findings the human accepted or dismissed, and Claude's labels on findings
  vocabulary.json        terms to leave out of the glossary candidates
  vocab-cache.json       per-file word counts for the candidates; not committed
  server.json            the running editor server (url, pid); not committed
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

DIR = ".archplan"
DEFAULT_PORT = 8765

GITIGNORE = """\
# Local runtime files. Commit the rest to share the target, plan and threads.
server.json
server.log
.hook-seen.json
activity.json
hotspots.json
vocab-cache.json
*.tmp
.*.lock
"""


class ProjectError(Exception):
    pass


class UntrustedExport(ProjectError):
    pass


def trust_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "archplan" / "trusted.json"


def command_hash(root: Path, cmd: str) -> str:
    return hashlib.sha256(f"{root}\0{cmd}".encode()).hexdigest()


def find_root(start: Path | None = None) -> Path | None:
    """The nearest directory, from start upwards, that has an .archplan directory."""
    p = (start or Path.cwd()).resolve()
    for d in (p, *p.parents):
        if (d / DIR / "config.json").is_file():
            return d
    return None


def write_atomic(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def write_json(p: Path, obj) -> None:
    write_atomic(p, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return default


def etag(p: Path) -> str | None:
    """A cheap change marker for a file: size and modification time."""
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    return f"{st.st_size:x}-{st.st_mtime_ns:x}"


class Project:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.dir = self.root / DIR
        self.config = read_json(self.dir / "config.json", {}) or {}

    @classmethod
    def find(cls, start: Path | None = None, dir: str | None = None) -> "Project":
        """The project at dir (a project root or its .archplan directory), or the
        nearest one above start."""
        if dir:
            d = Path(dir).resolve()
            root = d.parent if d.name == DIR else d
            if not (root / DIR / "config.json").is_file():
                raise ProjectError(f"no archplan project at {root} (run `archplan init` there)")
            return cls(root)
        if start is None and os.environ.get("CLAUDE_PROJECT_DIR"):
            start = Path(os.environ["CLAUDE_PROJECT_DIR"])
        root = find_root(start)
        if root is None:
            raise ProjectError("no .archplan directory here or above; run `archplan init` in the repository root")
        return cls(root)

    # paths

    def path(self, name: str) -> Path:
        return self.dir / name

    @property
    def model_path(self) -> Path:
        return self.root / self.config.get("model", f"{DIR}/model.archmodel.json")

    @property
    def target_path(self) -> Path:
        return self.path("target.archmodel.json")

    @property
    def base_path(self) -> Path:
        return self.path("base.archmodel.json")

    @property
    def name(self) -> str:
        return self.config.get("name") or self.root.name

    @property
    def port(self) -> int:
        return int(self.config.get("port", DEFAULT_PORT))

    # state

    def state(self) -> dict:
        return read_json(self.path("state.json"), {}) or {}

    def set_state(self, **kw) -> dict:
        st = self.state()
        st.update(kw)
        write_json(self.path("state.json"), st)
        return st

    def save_target(self, target: dict, by: str, base_etag: str | None = None) -> int:
        """Write the target model and bump its revision. by is 'editor' or 'agent'.

        base_etag is the as-is model the editor edited against. When it is the
        current model, a copy becomes the base, so the editor can later rebase
        the target onto a newer export."""
        write_json(self.target_path, target)
        st = self.state()
        rev = int(st.get("target_rev", 0)) + 1
        upd = {"target_rev": rev, "target_by": by}
        cur = etag(self.model_path)
        if base_etag and base_etag == cur and st.get("base_etag") != cur:
            write_atomic(self.base_path, self.model_path.read_text())
            upd["base_etag"] = cur
        self.set_state(**upd)
        return rev

    def target_or_model(self) -> dict | None:
        """The target model, or the as-is model when nobody edited one yet."""
        return read_json(self.target_path) or read_json(self.model_path)

    # export

    def export_command(self) -> str | None:
        return self.config.get("export")

    # The export command comes from config.json, which a cloned repository
    # supplies. It runs in a shell, so the agent (refresh_model) and the editor
    # (From code) run it only after a person approved this exact command for this
    # directory, outside the repository: `archplan init`, `archplan trust`, or
    # answering yes in `archplan export`.

    def trusted(self) -> bool:
        cmd = self.export_command()
        return bool(cmd) and command_hash(self.root, cmd) in (read_json(trust_path(), {}) or {}).get("commands", [])

    def trust(self) -> None:
        cmd = self.export_command()
        if not cmd:
            raise ProjectError("no export command to trust")
        tp = trust_path()
        tp.parent.mkdir(parents=True, exist_ok=True)
        data = read_json(tp, {}) or {}
        data.setdefault("commands", [])
        h = command_hash(self.root, cmd)
        if h not in data["commands"]:
            data["commands"].append(h)
            write_json(tp, data)

    def export(self, timeout: float = 600, require_trust: bool = False) -> dict:
        """Run the configured exporter in the project root. The command writes the
        as-is model to the configured model path (or prints it on stdout)."""
        cmd = self.export_command()
        if not cmd:
            raise ProjectError(f"no export command in {self.dir / 'config.json'}")
        if require_trust and not self.trusted():
            raise UntrustedExport(
                f"the export command in .archplan/config.json is not approved on this machine: {cmd!r}. "
                "A person must run `archplan trust` (or `archplan export`) in a terminal once.")
        env = os.environ.copy()
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        if cmd.startswith("archplan "):
            # Our own exporter: run it with this interpreter, so it works when
            # archplan is not on the PATH (as in a plugin's bin/). Isolated mode
            # (-I) keeps the repository's own files off sys.path, so a planted
            # archplan package in the root cannot take its place.
            boot = f"import sys; sys.path.insert(0, {pkg_parent!r}); from archplan.cli import main; sys.exit(main())"
            cmd = f"{shlex.quote(sys.executable)} -I -c {shlex.quote(boot)} {cmd[len('archplan '):]}"
        before = etag(self.model_path)
        r = subprocess.run(cmd, shell=True, cwd=self.root, capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            raise ProjectError(f"export failed ({r.returncode}): {cmd}\n{r.stderr.strip()[-2000:]}")
        if etag(self.model_path) == before and r.stdout.lstrip().startswith("{"):
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self.model_path, r.stdout)
        m = read_json(self.model_path)
        if not m or m.get("format") != "archmodel/1":
            raise ProjectError(f"export did not produce an archmodel/1 file at {self.model_path}")
        if not m.get("vocabulary"):
            from .vocab import enrich  # the vocabulary step for exporters that have none
            enrich(self)
        return counts(m)


def counts(m: dict) -> dict:
    return {k: len(m.get(k) or {}) for k in ("components", "dependencies", "types", "sequences")}


def detect_export(root: Path) -> str | None:
    """A default export command for the languages archplan has an exporter for."""
    out = shlex.quote(f"{DIR}/model.archmodel.json")
    if (root / "go.mod").is_file():
        return f"go run github.com/vloothuis/archplan/exporters/archplan-go@latest -o {out}"
    if any((root / f).is_file() for f in ("pyproject.toml", "setup.py", "setup.cfg")):
        return f"archplan export-python -o {out}"
    mix = mix_dir(root)
    if mix is not None:
        c = f" -C {shlex.quote(str(mix))}" if str(mix) != "." else ""
        return f"archplan export-elixir{c} -o {out}"
    return None


def mix_dir(root: Path) -> Path | None:
    """The Mix project: the root, or else the direct subdirectory with a mix.exs
    and the most .ex files under lib/."""
    if (root / "mix.exs").is_file():
        return Path(".")
    subs = [d for d in sorted(root.iterdir())
            if d.is_dir() and not d.name.startswith(".") and (d / "mix.exs").is_file()]
    if not subs:
        return None
    return max(subs, key=lambda d: sum(1 for _ in (d / "lib").rglob("*.ex"))).relative_to(root)


def init(root: Path, export: str | None = None, port: int | None = None, force: bool = False) -> Project:
    d = root.resolve() / DIR
    cfg_path = d / "config.json"
    if cfg_path.exists() and not force:
        raise ProjectError(f"{cfg_path} exists (use --force to overwrite)")
    d.mkdir(parents=True, exist_ok=True)
    cfg = {"name": root.resolve().name, "model": f"{DIR}/model.archmodel.json",
           "export": export or detect_export(root.resolve()), "port": port or DEFAULT_PORT}
    write_json(cfg_path, cfg)
    if cfg["export"]:
        Project(root).trust()
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)
    return Project(root)
