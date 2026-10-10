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

from .project import Project, ProjectError, UntrustedExport, etag, read_json, write_atomic, write_json
from .store import Store, wait

LONG_POLL = 25.0  # seconds the editor's request for changes may wait
STATIC = Path(str(resources.files("archplan") / "static"))


def changes(p: Project, store: Store) -> dict:
    """What the editor polls for: threads, the as-is model and the target revision."""
    st = p.state()
    c = {"version": store.version(), "model_etag": etag(p.model_path),
         "target_rev": st.get("target_rev", 0), "target_by": st.get("target_by")}
    c["key"] = f"{c['version']}.{c['model_etag']}.{c['target_rev']}"
    return c


def make_handler(p: Project, store: Store):
    export_lock = threading.Lock()

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
            return super().do_GET()

        def do_POST(self):
            if self.refuse():
                return
            parts = self.path.strip("/").split("/")
            try:
                b = self.body() or {}
                if parts == ["api", "threads"]:
                    return self.send_json(store.create(b["anchor"], b.get("label", ""), b["text"],
                                                       b.get("author", "human"), b.get("kind"), b.get("options")))
                if parts == ["api", "threads", "merge"]:
                    store.merge(b.get("threads", []))
                    return self.send_json(store.read())
                if len(parts) == 4 and parts[:2] == ["api", "threads"] and parts[3] == "messages":
                    return self.send_json(store.reply(parts[2], b.get("text", ""), b.get("author", "human"),
                                                      b.get("status"), b.get("kind"), b.get("options")))
                if parts == ["api", "export"]:
                    if not export_lock.acquire(blocking=False):
                        return self.send_json({"error": "an export is already running"}, HTTPStatus.CONFLICT)
                    try:
                        return self.send_json({"ok": True, "counts": p.export(require_trust=True), **changes(p, store)})
                    finally:
                        export_lock.release()
            except UntrustedExport as e:
                return self.send_json({"error": str(e)}, HTTPStatus.FORBIDDEN)
            except ProjectError as e:
                return self.send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except (KeyError, ValueError) as e:
                return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_PUT(self):
            if self.refuse():
                return
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
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def free_port(host: str, start: int, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket() as s:
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
