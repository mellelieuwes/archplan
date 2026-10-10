import io
import json
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
        self.root = Path(self.tmp.name)
        write(self.root, SAMPLE)

    def tearDown(self):
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
