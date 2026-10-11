"""The editor server: serves the bundled editor and a small JSON API."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote

from . import handover, hotspots
from .architecture import Architecture, ArchitectureError
from .findings import Findings
from .project import Project, ProjectError, UntrustedExport, etag, read_json, write_atomic, write_json
from .handover import Handovers
from .store import Conflict, NotFound, Store, wait
from .suggestions import Suggestions, pick, preview

FINDING_ACTIONS = {"dismiss": "dismissed", "accept": "accepted", "reopen": "new", "discuss": None}
LONG_POLL = 25.0  # seconds the editor's request for changes may wait
STATIC = Path(str(resources.files("archplan") / "static"))


def changes(p: Project, store: Store) -> dict:
    """What the editor polls for: threads, the as-is model, the target revision,
    the agent's suggestions, the handover, the architecture profile, the
    findings baseline and the agent's last activity. key
    changes when any does."""
    st = p.state()
    c = {"version": store.version(), "model_etag": etag(p.model_path),
         "target_rev": st.get("target_rev", 0), "target_by": st.get("target_by"),
         "suggestions_version": Suggestions(p.dir).version(), "handover_version": Handovers(p.dir).version(),
         "architecture_etag": etag(p.path("architecture.json")), "findings_etag": etag(p.path("baseline.json")),
         "activity_etag": etag(p.path("activity.json"))}
    c["key"] = ".".join(str(c[k]) for k in ("version", "model_etag", "target_rev", "suggestions_version",
                                            "handover_version", "architecture_etag", "findings_etag",
                                            "activity_etag"))
    return c


def make_handler(p: Project, store: Store):
    export_lock = threading.Lock()
    hotspot_lock = threading.Lock()
    suggestions = Suggestions(p.dir)
    handovers = Handovers(p.dir)
    arch = Architecture(p)
    arch_lock = threading.Lock()
    findings = Findings(p)
    findings.arch = arch  # share the profile analysis cache

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(STATIC), **kw)

        def log_message(self, fmt, *args):
            if os.environ.get("ARCHPLAN_DEBUG"):
                sys.stderr.write("%s\n" % (fmt % args))

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def send_json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path: Path):
            try:
                body = path.read_bytes()
            except FileNotFoundError:
                return self.send_json({"error": f"{path.name} not found"}, HTTPStatus.NOT_FOUND)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"null")

        def local_origin(self) -> bool:
            """Refuse writes from other sites' pages (a browser on this machine
            could otherwise be made to post to the API)."""
            o = self.headers.get("Origin")
            return o is None or o == f"http://{self.headers.get('Host')}"

        def local_host(self) -> bool:
            """Answer only requests addressed to this machine by name or loopback
            address. A web page that rebinds its own domain to 127.0.0.1 then
            cannot read or write the project (DNS rebinding)."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def refuse(self) -> bool:
            if not self.local_host():
                self.send_json({"error": "request for a non-local host refused"}, HTTPStatus.FORBIDDEN)
                return True
            if self.command != "GET" and not self.local_origin():
                self.send_json({"error": "cross-origin request refused"}, HTTPStatus.FORBIDDEN)
                return True
            return False

        def do_GET(self):
            if self.refuse():
                return
            path, _, query = self.path.partition("?")
            params = dict(x.split("=", 1) for x in query.split("&") if "=" in x)
            if path == "/api/info":
                st = p.state()
                return self.send_json({"collab": True, "name": p.name, "root": str(p.root), "dir": str(p.dir),
                                       "model": "/api/model" if p.model_path.exists() else None,
                                       "can_export": bool(p.export_command()),
                                       "git": hotspots.info(p.root),
                                       "intent": st.get("intent", ""), **changes(p, store)})
            if path == "/api/model":
                return self.send_file(p.model_path)
            if path == "/api/target":
                return self.send_file(p.target_path)
            if path == "/api/base":
                return self.send_file(p.base_path)
            if path == "/api/state":
                st = p.state()
                return self.send_json({**st, "has_target": p.target_path.exists(),
                                       "base_current": st.get("base_etag") == etag(p.model_path)})
            if path == "/api/threads":
                since = params.get("since", "-1")
                if since == changes(p, store)["key"]:
                    wait(lambda: changes(p, store)["key"], since, LONG_POLL)
                return self.send_json({**store.read(), **changes(p, store)})
            if path == "/api/activity":
                return self.send_json(read_json(p.path("activity.json"), {}) or {})
            if path == "/api/handover":
                return self.send_json({"current": handovers.current(), "history": handovers.history(10),
                                       **changes(p, store)})
            if path == "/api/architecture":
                try:
                    with arch_lock:
                        return self.send_json({**arch.state(), **changes(p, store)})
                except ArchitectureError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            if path == "/api/findings":
                try:
                    with arch_lock:
                        return self.send_json({**findings.listing(store), **changes(p, store)})
                except ArchitectureError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            if path == "/api/suggestions":
                return self.send_json({"suggestions": suggestions.newest(), **changes(p, store)})
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "suggestions"] and parts[3] == "preview":
                try:
                    sg = suggestions.get(parts[2])
                    ops = params.get("ops")
                    idx = None if ops is None else [int(x) for x in unquote(ops).split(",") if x.strip()]
                    m, done = preview(p, sg, idx)
                    return self.send_json({"target": m, "applied": done, "ops": pick(sg, idx)})
                except NotFound as e:
                    return self.send_json({"error": str(e).strip("'\"")}, HTTPStatus.NOT_FOUND)
                except Conflict as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.CONFLICT)
                except ValueError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            if path == "/api/hotspots":
                try:
                    with hotspot_lock:
                        kw = {k: t(params[q]) for q, k, t in (("together", "min_together", int),
                                                               ("degree", "min_degree", float)) if q in params}
                        return self.send_json(hotspots.hotspots(p, int(params.get("since", 365)),
                                                                refresh=params.get("refresh") == "1", **kw))
                except hotspots.NotGit as e:
                    return self.send_json({"error": f"no git history: {e}"}, HTTPStatus.NOT_FOUND)
                except ValueError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            return super().do_GET()

        def do_POST(self):
            if self.refuse():
                return
            parts = self.path.strip("/").split("/")
            try:
                b = self.body() or {}
                if parts == ["api", "threads"]:
                    return self.send_json(store.create(b["anchor"], b.get("label", ""), b["text"],
                                                       b.get("author", "human"), b.get("kind"), b.get("options"),
                                                       b.get("snapshot")))
                if parts == ["api", "threads", "merge"]:
                    store.merge(b.get("threads", []))
                    return self.send_json(store.read())
                if len(parts) == 4 and parts[:2] == ["api", "threads"] and parts[3] == "messages":
                    return self.send_json(store.reply(parts[2], b.get("text", ""), b.get("author", "human"),
                                                      b.get("status"), b.get("kind"), b.get("options")))
                if len(parts) == 4 and parts[:2] == ["api", "suggestions"] and parts[3] in ("accept", "reject"):
                    idx = [] if parts[3] == "reject" else b.get("ops")
                    if parts[3] == "accept" and idx is not None and (not isinstance(idx, list) or not idx):
                        raise ValueError("ops must be a non-empty list of op indices; reject to accept none")
                    sg = suggestions.decide(p, parts[2], idx)
                    return self.send_json({"suggestion": sg, **changes(p, store)})
                if len(parts) == 4 and parts[:2] == ["api", "threads"] and parts[3] == "lock":
                    return self.send_json(handover.lock(store, parts[2], b.get("title", ""), b.get("summary", "")))
                if len(parts) == 4 and parts[:2] == ["api", "threads"] and parts[3] == "unlock":
                    return self.send_json(handover.unlock(store, handovers, parts[2]))
                if parts == ["api", "architecture", "lock"]:
                    with arch_lock:
                        return self.send_json({"profile": arch.lock(), **changes(p, store)})
                if parts == ["api", "architecture", "infer"]:
                    with arch_lock:
                        proposal, saved = arch.infer()
                    return self.send_json({"proposal": proposal, "saved": saved, **changes(p, store)})
                if len(parts) == 4 and parts[:2] == ["api", "findings"] and parts[3] in FINDING_ACTIONS:
                    with arch_lock:
                        if parts[3] == "discuss":
                            th, created = findings.discuss(store, parts[2], b.get("text"))
                            return self.send_json({"thread": th, "created": created,
                                                   "finding": findings.get(parts[2], store), **changes(p, store)})
                        f = findings.decide(parts[2], FINDING_ACTIONS[parts[3]], b.get("reason"))
                    return self.send_json({"finding": f, **changes(p, store)})
                if parts == ["api", "handover"]:
                    return self.send_json(handovers.create(store, b.get("mode", "all"), b.get("note", "")))
                if len(parts) == 4 and parts[:2] == ["api", "handover"] and parts[3] == "approve":
                    return self.send_json(handovers.approve(store, parts[2], b.get("batches")))
                if len(parts) == 4 and parts[:2] == ["api", "handover"] and parts[3] == "cancel":
                    return self.send_json(handovers.cancel(store, parts[2]))
                if parts == ["api", "export"]:
                    if not export_lock.acquire(blocking=False):
                        return self.send_json({"error": "an export is already running"}, HTTPStatus.CONFLICT)
                    try:
                        return self.send_json({"ok": True, "counts": p.export(require_trust=True), **changes(p, store)})
                    finally:
                        export_lock.release()
            except UntrustedExport as e:
                return self.send_json({"error": str(e)}, HTTPStatus.FORBIDDEN)
            except ArchitectureError as e:
                return self.send_json({"error": str(e)}, HTTPStatus.CONFLICT)
            except NotFound as e:
                return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except Conflict as e:
                return self.send_json({"error": str(e)}, HTTPStatus.CONFLICT)
            except ProjectError as e:
                return self.send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except (KeyError, TypeError, ValueError) as e:
                return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_PUT(self):
            if self.refuse():
                return
            parts = self.path.strip("/").split("/")
            try:
                b = self.body() or {}
                if self.path == "/api/target":
                    rev = p.save_target(b["target"], "editor", b.get("base_etag"))
                    if "intent" in b:
                        p.set_state(intent=b["intent"])
                    return self.send_json({"ok": True, "target_rev": rev})
                if self.path == "/api/plan":
                    write_atomic(p.path("plan.md"), b.get("md", ""))
                    write_atomic(p.path("plan.json"), b.get("json", "{}"))
                    return self.send_json({"ok": True})
                if len(parts) == 4 and parts[:2] == ["api", "threads"] and parts[3] == "refactor":
                    return self.send_json(handover.edit(store, parts[2], b.get("title"), b.get("summary")))
                if parts == ["api", "architecture"]:
                    with arch_lock:
                        return self.send_json({"profile": arch.save(b["profile"]), **changes(p, store)})
                if parts == ["api", "refactors", "order"]:
                    return self.send_json({"refactors": handover.reorder(store, b.get("ids")), **changes(p, store)})
            except NotFound as e:
                return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except Conflict as e:
                return self.send_json({"error": str(e)}, HTTPStatus.CONFLICT)
            except (KeyError, TypeError, ValueError) as e:
                return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def free_port(host: str, start: int, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket() as s:
            # Match the server's own option, so a port in TIME_WAIT after a restart is reused.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise ProjectError(f"no free port in {start}..{start + tries - 1}")


def serve(p: Project, host: str = "127.0.0.1", port: int | None = None) -> None:
    store = Store(p.dir)
    port = free_port(host, port or p.port)
    srv = ThreadingHTTPServer((host, port), make_handler(p, store))
    url = f"http://{host}:{port}/"
    write_json(p.path("server.json"), {"url": url, "pid": os.getpid(), "port": port})
    print(f"archplan: {url}  ({p.root})", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    finally:
        info = read_json(p.path("server.json"), {}) or {}
        if info.get("pid") == os.getpid():
            p.path("server.json").unlink(missing_ok=True)
