import io
import json
import os
import unittest.mock
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from archplan import cli, model, project  # noqa: E402
from archplan.mcp import Agent  # noqa: E402
from archplan.pyexport import export  # noqa: E402
from archplan.store import Store, needs, who_next  # noqa: E402


def write(root: Path, files: dict):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))


SAMPLE = {
    "pyproject.toml": "[project]\nname = 'shop'\n",
    "shop/__init__.py": "",
    "shop/orders/__init__.py": '"""Orders."""\n',
    "shop/orders/model.py": """\
        from dataclasses import dataclass
        from shop.billing.invoice import Invoice

        @dataclass
        class Order:
            id: str
            invoices: list[Invoice]

            @property
            def total(self) -> int: ...
            @total.setter
            def total(self, v: int) -> None: ...
        """,
    "shop/billing/__init__.py": "",
    "shop/billing/invoice.py": """\
        from typing import Protocol
        from ..orders import model

        class Payer(Protocol):
            def pay(self, amount: int) -> bool: ...

        class Invoice:
            def __init__(self, payer: Payer):
                self.payer: Payer = payer
            def order(self) -> "model.Order": ...
        """,
    "tests/__init__.py": "",
    "tests/test_x.py": "import shop\n",
}


class TempProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        write(self.root, SAMPLE)
        self.env = unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.tmp.name) / "config")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class PyExportTest(TempProject):
    def test_components_dependencies_types(self):
        m = export(self.root)
        self.assertEqual(m["format"], "archmodel/1")
        self.assertEqual(set(m["components"]), {"shop", "shop.orders", "shop.billing"})
        deps = {(d["from"], d["to"]) for d in m["dependencies"]}
        self.assertEqual(deps, {("shop.orders", "shop.billing"), ("shop.billing", "shop.orders")})
        order = m["types"]["shop.orders.model.Order"]
        self.assertEqual(order["kind"], "record")
        self.assertEqual(order["fields"][1], {"name": "invoices", "ref": "shop.billing.invoice.Invoice", "many": True})
        self.assertEqual([x["name"] for x in order["methods"]], ["total"])
        self.assertEqual(m["types"]["shop.billing.invoice.Payer"]["kind"], "interface")
        inv = m["types"]["shop.billing.invoice.Invoice"]
        self.assertEqual(inv["fields"], [{"name": "payer", "ref": "shop.billing.invoice.Payer"}])
        self.assertEqual(inv["uses"], ["shop.orders.model.Order"])

    def test_tests_are_skipped_by_default(self):
        self.assertNotIn("tests", export(self.root)["components"])
        self.assertIn("tests.test_x", export(self.root, tests=True, granularity="module")["components"])


class ProjectTest(TempProject):
    def test_init_export_and_find(self):
        p = project.init(self.root)
        self.assertIn("export-python", p.config["export"])
        self.assertEqual(p.export()["components"], 3)
        sub = self.root / "shop" / "orders"
        self.assertEqual(project.Project.find(start=sub).root, self.root.resolve())
        with self.assertRaises(project.ProjectError):
            project.init(self.root)

    def test_save_target_keeps_base(self):
        p = project.init(self.root)
        p.export()
        rev = p.save_target({"format": "archmodel/1"}, "editor", project.etag(p.model_path))
        self.assertEqual(rev, 1)
        self.assertTrue(p.base_path.exists())
        self.assertEqual(p.state()["target_by"], "editor")


class DetectTest(unittest.TestCase):
    def test_elixir_in_root_or_largest_subdirectory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIsNone(project.detect_export(root))
            write(root, {"core/mix.exs": "", "core/lib/a.ex": "", "core/lib/b/c.ex": "",
                         "proxy/mix.exs": "", "proxy/lib/a.ex": ""})
            self.assertEqual(project.detect_export(root),
                             "archplan export-elixir -C core -o .archplan/model.archmodel.json")
            write(root, {"mix.exs": ""})
            self.assertEqual(project.detect_export(root), "archplan export-elixir -o .archplan/model.archmodel.json")


class TrustTest(TempProject):
    def test_export_needs_approval(self):
        p = project.init(self.root)
        self.assertTrue(p.trusted())  # init approves the command it writes
        cfg = json.loads((p.dir / "config.json").read_text())
        cfg["export"] = "echo pwned > owned.txt"
        (p.dir / "config.json").write_text(json.dumps(cfg))
        p = project.Project(self.root)
        self.assertFalse(p.trusted())
        with self.assertRaises(project.UntrustedExport):
            p.export(require_trust=True)
        self.assertFalse((self.root / "owned.txt").exists())
        with self.assertRaises(project.UntrustedExport):
            Agent(p).refresh_model()
        # without a terminal, the command line cannot approve it either
        r = subprocess.run([str(ROOT / "bin" / "archplan"), "trust"], cwd=self.root, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(p.trusted())

    def test_prompt_shows_hidden_characters(self):
        self.assertEqual(cli.shown("ls\x1b[2K\rmake\u202e"), "ls\\u001b[2K\\u000dmake\\u202e")

    def test_own_exporter_ignores_planted_package(self):
        write(self.root, {"archplan/__init__.py": "", "archplan/__main__.py": "open('owned.txt', 'w')\n",
                          "archplan/cli.py": "open('owned.txt', 'w')\ndef main(argv=None): pass\n"})
        p = project.init(self.root)
        self.assertIn("archplan", p.export_command())
        p.export()
        self.assertFalse((self.root / "owned.txt").exists())


class ServerTest(TempProject):
    def test_host_and_origin_checks(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        from archplan.server import make_handler
        p = project.init(self.root)
        cfg = json.loads((p.dir / "config.json").read_text())
        cfg["export"] = "echo pwned > owned.txt"
        (p.dir / "config.json").write_text(json.dumps(cfg))
        p = project.Project(self.root)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(p, Store(p.dir)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"

        def code(path, method="GET", headers=None, body=None):
            req = urllib.request.Request(url + path, method=method, headers=headers or {},
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        try:
            self.assertEqual(code("/api/info"), 200)
            self.assertEqual(code("/api/info", headers={"Host": "evil.example:80"}), 403)
            self.assertEqual(code("/api/threads", "POST", {"Origin": "http://evil.example"}, {"anchor": "a", "text": "b"}), 403)
            self.assertEqual(code("/api/threads", "POST", {"Content-Type": "application/json"}, {"anchor": "a", "text": "b"}), 200)
            self.assertEqual(code("/api/export", "POST", {}, {}), 403)  # an unapproved command is refused
            self.assertFalse((self.root / "owned.txt").exists())
        finally:
            srv.shutdown()


class StoreTest(unittest.TestCase):
    def test_threads(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d))
            th = s.create("comp:a", "a", "why?", "human")
            self.assertEqual(who_next(th), "agent")
            th = s.reply(th["id"], "pick one", "agent", kind="decision", options=["x", "y"])
            self.assertEqual((who_next(th), needs(th)), ("human", "decision"))
            th = s.reply(th["id"], "", "human", status="resolved")
            self.assertEqual(who_next(th), "nobody")
            with self.assertRaises(ValueError):
                s.reply(th["id"], "", "human", status="bogus")


class EditTest(TempProject):
    def test_edit_target(self):
        p = project.init(self.root)
        p.export()
        out = model.edit_target(p, [
            {"op": "add_component", "id": "shop.ports", "path": "shop/ports"},
            {"op": "move_type", "type": "shop.billing.invoice.Payer", "to": "shop.ports"},
            {"op": "forbid", "from": "shop.billing", "to": "shop.orders"},
        ])
        self.assertEqual(out["violations"], 1)
        t = json.loads(p.target_path.read_text())
        self.assertEqual(t["types"]["shop.billing.invoice.Payer"]["component"], "shop.ports")
        self.assertEqual(p.state()["target_by"], "agent")
        # all or nothing
        with self.assertRaises(ValueError):
            model.edit_target(p, [{"op": "add_component", "id": "x"}, {"op": "move_type", "type": "nope", "to": "x"}])
        self.assertNotIn("x", json.loads(p.target_path.read_text())["components"])
        self.assertEqual(model.get_item(p, "cedge:shop.billing->shop.orders")["allowed"], False)

    def test_agent_status_and_find(self):
        p = project.init(self.root)
        p.export()
        a = Agent(p)
        Store(p.dir).create("comp:shop.orders", "orders", "split this?", "human")
        st = a.status()
        self.assertEqual(len(st["threads"]["waiting_on_agent"]), 1)
        self.assertEqual(a.find("invoice")[0]["anchor"], "type:shop.billing.invoice.Invoice")


class HookTest(TempProject):
    def run_hook(self, ev):
        buf = io.StringIO()
        sys.stdin = io.StringIO(json.dumps(ev))
        try:
            with redirect_stdout(buf):
                cli.main(["hook"])
        finally:
            sys.stdin = sys.__stdin__
        return buf.getvalue()

    def test_tells_once_per_session(self):
        p = project.init(self.root)
        Store(p.dir).create("comp:shop", "shop", "look at this", "human")
        ev = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": str(self.root)}
        self.assertIn("look at this", self.run_hook(ev))
        self.assertEqual(self.run_hook(ev), "")
        self.assertIn("look at this", self.run_hook({**ev, "session_id": "s2"}))
        self.assertEqual(self.run_hook({**ev, "cwd": tempfile.gettempdir()}), "")


class McpTest(TempProject):
    def test_stdio(self):
        project.init(self.root).export()
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_item", "arguments": {"anchor": "comp:shop.orders"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}}]
        r = subprocess.run([str(ROOT / "bin" / "archplan"), "mcp"], cwd=self.root, capture_output=True, text=True,
                           input="\n".join(json.dumps(x) for x in reqs) + "\n", timeout=30)
        out = [json.loads(l) for l in r.stdout.splitlines()]
        self.assertEqual([o["id"] for o in out], [1, 2, 3])
        self.assertEqual(json.loads(out[1]["result"]["content"][0]["text"])["path"], "shop/orders")
        self.assertIn("error", out[2])

    def test_unconfigured(self):
        with tempfile.TemporaryDirectory() as d:
            reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status"}}]
            r = subprocess.run([str(ROOT / "bin" / "archplan"), "mcp"], cwd=d, capture_output=True, text=True,
                               input="\n".join(json.dumps(x) for x in reqs) + "\n", timeout=30,
                               env={"PATH": "/usr/bin:/bin"})
            out = [json.loads(l) for l in r.stdout.splitlines()]
            self.assertIn("archplan init", out[1]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
