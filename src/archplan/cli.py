"""archplan: plan architecture changes with a human in an editor and an agent over MCP.

  archplan init [--export CMD]   set up .archplan/ in this repository
  archplan export                run the export command: code -> as-is model
  archplan up [--open]           start the editor in the background; print its URL
  archplan down                  stop it
  archplan serve                 run the editor in the foreground
  archplan status [--json]       editor URL, model size, threads waiting
  archplan threads [--waiting agent|human|any]
  archplan mcp                   MCP server on stdio, for an agent
  archplan watch                 one line per human comment or plan change, for a monitor
  archplan hook                  Claude Code hook: tell the agent about new comments
  archplan export-python         built-in exporter for Python projects
  archplan export-elixir         exporter for Elixir/Mix projects (needs mix)
  archplan trace-elixir          run mix tests and record call sequences
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .project import Project, ProjectError, counts, find_root, init, read_json, write_json
from .store import Store, needs, who_next


def die(msg: str, code: int = 1):
    print(f"archplan: {msg}", file=sys.stderr)
    sys.exit(code)


def project(a) -> Project:
    try:
        return Project.find(dir=a.dir)
    except ProjectError as e:
        die(str(e))


def running(p: Project) -> str | None:
    """The URL of this project's editor server when it answers."""
    info = read_json(p.path("server.json"))
    if not info:
        return None
    try:
        with urllib.request.urlopen(info["url"] + "api/info", timeout=1.5) as r:
            if json.load(r).get("root") == str(p.root):
                return info["url"]
    except (OSError, ValueError):
        pass
    return None


# ---------- commands ----------

def cmd_init(a):
    root = Path(a.root or ".").resolve()
    if (r := find_root(root)) and r != root and not a.force:
        die(f"{r} already has an archplan project; run archplan there, or pass --force")
    try:
        p = init(root, a.export, a.port, a.force)
    except ProjectError as e:
        die(str(e))
    print(f"created {p.dir}/config.json")
    if p.export_command():
        print(f"export command: {p.export_command()}")
        print("next: archplan export && archplan up")
    else:
        print("no exporter for this language yet: set \"export\" in config.json to a command that writes "
              f"{p.model_path.relative_to(p.root)} (format: archmodel/1), then run archplan export")


def shown(cmd: str) -> str:
    """The command with control and invisible characters escaped, so a prompt
    shows exactly what will run."""
    return "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in cmd)


def confirm_trust(p: Project) -> None:
    """Ask a person, at a terminal, to approve the export command before it
    first runs here. There is no flag to skip this: an agent must not be able
    to approve a command a cloned repository supplied."""
    if p.trusted() or not p.export_command():
        return
    print(f"The export command in {p.dir / 'config.json'} is new on this machine:\n  {shown(p.export_command())}",
          file=sys.stderr)
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        die("not approved; run `archplan trust` in a terminal after reading the command")
    sys.stderr.write("Run it, and allow the agent and the editor to run it from now on? [y/N] ")
    sys.stderr.flush()
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        die("not approved")
    p.trust()


def cmd_trust(a):
    p = project(a)
    confirm_trust(p)
    print(f"approved: {shown(p.export_command())}")


def cmd_export(a):
    p = project(a)
    confirm_trust(p)
    print(f"running: {shown(p.export_command())}", file=sys.stderr)
    t = time.monotonic()
    try:
        c = p.export()
    except ProjectError as e:
        die(str(e))
    print(f"{p.model_path.relative_to(p.root)}: " + ", ".join(f"{v} {k}" for k, v in c.items())
          + f" ({time.monotonic() - t:.1f}s)")


def cmd_serve(a):
    from .server import serve
    p = project(a)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        serve(p, a.bind, a.port)
    except KeyboardInterrupt:
        pass
    except ProjectError as e:
        die(str(e))


def cmd_up(a):
    p = project(a)
    url = running(p)
    if not url:
        if not p.model_path.exists() and p.export_command():
            print("no model yet; exporting first", file=sys.stderr)
            cmd_export(a)
        log = open(p.path("server.log"), "a")
        args = [sys.executable, "-m", "archplan", "serve", "--dir", str(p.root)]
        if a.port:
            args += ["--port", str(a.port)]
        subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=p.root, env=os.environ.copy())
        for _ in range(50):
            time.sleep(0.1)
            if url := running(p):
                break
        else:
            die(f"the editor did not start; see {p.path('server.log')}")
    print(url)
    if a.open:
        webbrowser.open(url)


def cmd_down(a):
    p = project(a)
    info = read_json(p.path("server.json"))
    if not info:
        return print("not running")
    pid = info.get("pid")
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        if "archplan" not in cmd:
            raise ProcessLookupError
        os.kill(pid, signal.SIGTERM)
        print(f"stopped {info['url']}")
    except (ProcessLookupError, TypeError):
        print("not running")
    p.path("server.json").unlink(missing_ok=True)


def cmd_status(a):
    from .mcp import Agent
    p = project(a)
    s = Agent(p).status()
    if a.json:
        return print(json.dumps(s, indent=2, ensure_ascii=False))
    print(f"project  {s['project']}  ({s['root']})")
    print(f"editor   {running(p) or 'not running (archplan up)'}")
    m = s["model"]
    print("model    " + (", ".join(f"{v} {k}" for k, v in m.items()) if isinstance(m, dict) else m))
    if s["intent"]:
        print(f"intent   {s['intent']}")
    print(f"plan     {s['plan_tasks']} tasks, {s['target']['violations']} policy violations in the target")
    w = s["threads"]["waiting_on_agent"]
    print(f"threads  {len(w)} waiting on the agent, {s['threads']['waiting_on_human']} on the human")
    for t in w:
        print(f"  {t['id']}  {t['label'][:60]}: {t['last']['text'][:100]}")


def cmd_threads(a):
    p = project(a)
    for t in Store(p.dir).read()["threads"]:
        if t["status"] != "open" and not a.all:
            continue
        if a.waiting != "any" and who_next(t) != a.waiting:
            continue
        flag = needs(t) or ""
        print(f"{t['id']}  [{t['status']}, waiting on {who_next(t)}{', ' + flag if flag else ''}]  {t['label']}")
        for m in t["messages"]:
            print(f"    {m['author']}: {' '.join(m['text'].split())[:300]}")


def cmd_mcp(a):
    from . import mcp
    try:
        p = Project.find(dir=a.dir)
    except ProjectError as e:
        return mcp.serve_unconfigured(str(e))
    mcp.serve(p)


def cmd_watch(a):
    from .watch import watch
    try:
        while True:
            try:
                p = Project.find(dir=a.dir)
                break
            except ProjectError:
                if a.dir:
                    raise
                time.sleep(5)  # wait quietly until `archplan init` runs here
        watch(p, a.interval)
    except KeyboardInterrupt:
        pass


def cmd_hook(a):
    """For Claude Code SessionStart and UserPromptSubmit hooks. Prints (into the
    agent's context) the threads waiting on the agent that it was not told
    about yet in this session. Silent outside archplan projects."""
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        ev = {}
    root = find_root(Path(ev.get("cwd") or "."))
    if not root:
        return
    p = Project(root)
    seen_path = p.path(".hook-seen.json")
    seen = read_json(seen_path, {}) or {}
    sid = ev.get("session_id", "")
    if seen.get("session") != sid:
        seen = {"session": sid, "threads": {}}
    new = []
    for t in Store(p.dir).read()["threads"]:
        if who_next(t) == "agent" and seen["threads"].get(t["id"]) != len(t["messages"]):
            new.append(t)
            seen["threads"][t["id"]] = len(t["messages"])
    if ev.get("hook_event_name") == "SessionStart" or new:
        write_json(seen_path, seen)
    if not new:
        return
    url = running(p)
    lines = [f"archplan: {len(new)} comment thread(s) in {p.name} wait on you"
             + (f" (editor {url})" if url else "") + ". Read them with the archplan MCP tools "
             "(read_thread, then reply in the thread); use the /archplan skill for the workflow."]
    for t in new[:10]:
        last = " ".join(t["messages"][-1]["text"].split())
        lines.append(f"- {t['id']} on {t['label'][:80]}: {last[:200]}")
    print("\n".join(lines))


def cmd_export_python(a):
    from .pyexport import export
    root = Path(a.root or ".").resolve()
    m = export(root, a.package, a.granularity, a.tests)
    text = json.dumps(m, indent=2, ensure_ascii=False) + "\n"
    if a.output:
        Path(a.output).parent.mkdir(parents=True, exist_ok=True)
        Path(a.output).write_text(text)
        print(f"{a.output}: " + ", ".join(f"{v} {k}" for k, v in counts(m).items()), file=sys.stderr)
    else:
        sys.stdout.write(text)


INTRO = """\
archplan: plan an architecture change in a browser editor, then let an agent build it.

The as-is model comes from your code. You edit the to-be model; the difference
becomes a plan. You and the agent discuss any component, type, dependency or
call in comment threads.

Start, in the repository root:
  archplan init      set up .archplan/ (picks an exporter for Go or Python)
  archplan export    read the code into .archplan/model.archmodel.json
  archplan up        open the editor (press ? there for help)

With Claude Code: install the plugin, then type /archplan in the repository.
  claude plugin marketplace add vloothuis/archplan
  claude plugin install archplan@archplan

All commands: archplan --help
"""


def cmd_help(a):
    print(INTRO, end="")
    root = find_root()
    if root:
        print(f"\nThis repository is set up: {root}/.archplan. Run `archplan status`.")


def elixir(cmd: str, args: list) -> None:
    """Run the shipped Elixir exporter: export-elixir and trace-elixir pass
    their arguments to `archplan-elixir export|trace`."""
    wrapper = Path(__file__).resolve().parent / "elixir" / "archplan-elixir"
    os.execvp("sh", ["sh", str(wrapper), cmd, *args])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("export-elixir", "trace-elixir"):
        if argv[1:2] in (["-h"], ["--help"]):
            argv = [argv[0]]
        elixir(argv[0].split("-")[0], argv[1:])
    ap = argparse.ArgumentParser(prog="archplan", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"archplan {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help):
        s = sub.add_parser(name, help=help)
        s.add_argument("--dir", help="project root (default: the nearest directory with .archplan, from here up)")
        s.set_defaults(fn=fn)
        return s

    s = add("init", cmd_init, "set up .archplan/ in a repository")
    s.add_argument("root", nargs="?", help="repository root (default: .)")
    s.add_argument("--export", help="command that writes the as-is model (default: detected from the language)")
    s.add_argument("--port", type=int)
    s.add_argument("--force", action="store_true")
    s = add("export", cmd_export, "re-export the as-is model from the code")
    s = add("trust", cmd_trust, "approve this repository's export command for the agent and the editor")
    s = add("serve", cmd_serve, "run the editor server in the foreground")
    s.add_argument("--port", type=int)
    s.add_argument("--bind", default="127.0.0.1")
    s = add("up", cmd_up, "start the editor server in the background and print its URL")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="open it in the default browser")
    add("down", cmd_down, "stop the background editor server")
    s = add("status", cmd_status, "show the project state")
    s.add_argument("--json", action="store_true")
    s = add("threads", cmd_threads, "print comment threads")
    s.add_argument("--waiting", choices=["agent", "human", "any"], default="any")
    s.add_argument("--all", action="store_true", help="include resolved threads")
    add("mcp", cmd_mcp, "MCP server on stdio")
    s = add("watch", cmd_watch, "print human comments and plan changes as they happen")
    s.add_argument("--interval", type=float, default=2)
    add("hook", cmd_hook, "Claude Code hook (reads the hook event on stdin)")
    s = add("export-python", cmd_export_python, "export a Python project's as-is model")
    s.add_argument("root", nargs="?")
    s.add_argument("-o", "--output")
    s.add_argument("--package", action="append", help="top-level package to include (default: all found)")
    s.add_argument("--granularity", choices=["auto", "package", "module"], default="auto",
                   help="one component per package or per module (auto: modules when there are fewer than 3 packages)")
    s.add_argument("--tests", action="store_true", help="include test packages and test_*.py files")

    add("help", cmd_help, "how to get started")
    a = ap.parse_args(argv)
    (getattr(a, "fn", None) or cmd_help)(a)
