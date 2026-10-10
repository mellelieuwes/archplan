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

from archplan import cli, hotspots, model, project  # noqa: E402
from archplan.mcp import Agent, record, snapshot_text  # noqa: E402
from archplan.suggestions import Conflict, Suggestions  # noqa: E402
from archplan.pyexport import export  # noqa: E402
from archplan.store import Store, mentions, needs, who_next  # noqa: E402


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

    def test_handover_and_planned(self):
        from archplan import handover
        p = project.init(self.root)
        s = Store(p.dir)
        th = s.create("comp:shop", "shop", "split it", "human")
        handover.lock(s, th["id"], "Split shop")
        ev = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": str(self.root)}
        self.assertEqual(self.run_hook(ev), "")  # planned: not for the agent yet
        h = handover.Handovers(p.dir).create(s, "batches")
        out = self.run_hook(ev)
        self.assertIn(f"handover {h['id']}", out)
        self.assertIn("propose batches first", out)
        self.assertIn("split it", out)  # the thread now waits on the agent
        self.assertEqual(self.run_hook(ev), "")


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


def serve_in_thread(p):
    """An editor server on a free port; returns (server, base url)."""
    import threading
    from http.server import ThreadingHTTPServer
    from archplan.server import make_handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(p, Store(p.dir)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def call(url, path, method="GET", body=None):
    """(status, JSON body) of an API request."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.load(e)


class HotspotTest(unittest.TestCase):
    COMPS = {"orders": {"path": "lib/orders", "match": ["lib/orders/**"]},
             "billing": {"path": "lib/billing", "match": ["lib/billing/**", "lib/billing.ex"]},
             "web": {"path": "lib/web"},
             "core": {"path": "lib", "match": ["lib/core.ex"]},
             "ext": {"path": "deps/x", "kind": "external"}}

    def test_mapping_longest_match_and_prefix_offset(self):
        m = hotspots.Mapper(self.COMPS)
        self.assertEqual(m("lib/orders/a.ex"), "orders")
        self.assertEqual(m("lib/billing.ex"), "billing")
        self.assertEqual(m("lib/web/x/y.ex"), "web")  # path, when there are no match patterns
        self.assertEqual(m("lib/core.ex"), "core")
        self.assertEqual(m("lib/other.ex"), "core")  # the longest component path that contains it
        self.assertIsNone(m("deps/x/a.ex"))  # external code is not ours
        self.assertIsNone(m("README.md"))
        # git reports paths from the repository root; this model was exported in core/
        files = ["core/lib/orders/a.ex", "core/lib/billing/b.ex", "core/lib/web/c.ex", "README.md", "assets/x.js"]
        self.assertEqual(hotspots.best_prefix(files, m), "core/")
        self.assertEqual(hotspots.best_prefix(["lib/orders/a.ex", "lib/web/b.ex"], m), "")

    def test_pairs_from_git(self):
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            self.skipTest("no git")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            sub = root / "core"
            sub.mkdir(parents=True)
            env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
                   "GIT_COMMITTER_EMAIL": "a@x", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
            git = lambda *a: subprocess.run(["git", *a], cwd=root, env=env, check=True, capture_output=True)
            git("init", "-q")
            n = [0]

            def commit(*paths, email="a@x"):
                n[0] += 1
                for f in paths:
                    write(root, {f: f"v{n[0]}\n"})
                git("add", "-A")
                subprocess.run(["git", "commit", "-q", "-m", f"c{n[0]}"], cwd=root, check=True, capture_output=True,
                               env={**env, "GIT_AUTHOR_EMAIL": email})
            for _ in range(3):
                commit("core/lib/orders/a.ex", "core/lib/billing/b.ex")
            commit("core/lib/orders/a.ex", email="b@x")
            commit("core/lib/web/w.ex", "core/lib/orders/c.ex")
            commit(*[f"core/lib/web/f{i}.ex" for i in range(6)])  # bulk: skipped with max_files=5
            commit("README.md")
            write(root, {"core/.archplan/config.json": "{}"})
            m = {"components": self.COMPS, "dependencies": [{"from": "web", "to": "orders"}]}
            res = hotspots.analyse(hotspots.history(root, 30), m, max_files=5, min_together=1, min_degree=0.3)
            self.assertEqual((res["commits"], res["skipped"], res["unmapped"], res["prefix"]), (6, 1, 1, "core/"))
            self.assertEqual(res["components"]["orders"]["commits"], 5)
            self.assertEqual(res["components"]["orders"]["authors"], 2)
            self.assertEqual(res["components"]["orders"]["files"], 2)
            pairs = {(x["a"], x["b"]): x for x in res["pairs"]}
            self.assertEqual(pairs[("billing", "orders")], {"a": "billing", "b": "orders", "together": 3,
                                                            "degree": 1.0, "dependency": False})
            self.assertEqual(pairs[("orders", "web")]["dependency"], True)
            self.assertEqual(res["pairs"][0]["a"], "billing")  # sorted by together * degree
            # through the project, with the cache
            p = project.Project(sub)
            (sub / ".archplan" / "model.archmodel.json").write_text(json.dumps({"format": "archmodel/1", **m}))
            r1 = hotspots.hotspots(p, 30, max_files=5, min_together=3)
            self.assertEqual([(x["a"], x["b"]) for x in r1["pairs"]], [("billing", "orders")])
            self.assertTrue((sub / ".archplan" / "hotspots.json").exists())
            self.assertEqual(hotspots.hotspots(p, 30, max_files=5, min_together=3), r1)
            self.assertEqual(hotspots.info(sub)["head"], r1["head"][:len(hotspots.info(sub)["head"])])
            self.assertIn("billing <-> orders", hotspots.text(r1))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(hotspots.info(Path(d)))


class SuggestionTest(TempProject):
    OPS = [{"op": "add_component", "id": "shop.ports", "path": "shop/ports"},
           {"op": "forbid", "from": "shop.billing", "to": "shop.orders"},
           {"op": "move_type", "type": "shop.billing.invoice.Payer", "to": "shop.ports"}]

    def test_propose_preview_accept_reject(self):
        p = project.init(self.root)
        p.export()
        a = Agent(p)
        th = Store(p.dir).create("comp:shop.billing", "billing", "split ports out?", "human")
        with self.assertRaises(ValueError):  # an op that does not apply saves nothing
            a.edit_target([{"op": "move_type", "type": "nope", "to": "shop"}])
        out = a.edit_target(self.OPS, note="Ports out of billing", thread=th["id"])
        self.assertEqual((out["status"], out["ops"]), ("pending", 3))
        self.assertFalse(p.target_path.exists())  # the target is unchanged
        self.assertIn(out["suggestion"], Store(p.dir).read()["threads"][0]["messages"][-1]["text"])
        sid = out["suggestion"]
        self.assertEqual(a.status()["suggestions"]["pending"], 1)
        srv, url = serve_in_thread(p)
        try:
            st, d = call(url, "/api/suggestions")
            self.assertEqual([x["id"] for x in d["suggestions"]], [sid])
            key = d["key"]
            st, d = call(url, f"/api/suggestions/{sid}/preview?ops=0,1")
            self.assertEqual(st, 200)
            self.assertIn("shop.ports", d["target"]["components"])
            self.assertEqual(d["target"]["types"]["shop.billing.invoice.Payer"]["component"], "shop.billing")
            self.assertEqual(call(url, f"/api/suggestions/{sid}/preview?ops=2")[0], 409)  # needs op 0 first
            self.assertEqual(call(url, f"/api/suggestions/{sid}/preview?ops=7")[0], 400)
            self.assertEqual(call(url, "/api/suggestions/sg-nope/preview")[0], 404)
            st, d = call(url, f"/api/suggestions/{sid}/accept", "POST", {"ops": [1, 0]})
            self.assertEqual(st, 200)
            sg = d["suggestion"]
            self.assertEqual((sg["status"], sg["accepted"]), ("partial", [0, 1]))
            self.assertIsNotNone(sg["decided"])
            self.assertNotEqual(d["key"], key)  # the editor's poll sees the change
            t = json.loads(p.target_path.read_text())
            self.assertIn("shop.ports", t["components"])
            self.assertEqual(p.state()["target_by"], "agent-suggestion")
            self.assertEqual(call(url, f"/api/suggestions/{sid}/reject", "POST", {})[0], 409)  # already decided
            # a second suggestion, made against the new target, is rejected
            sid2 = a.edit_target(self.OPS[2:])["suggestion"]
            st, d = call(url, f"/api/suggestions/{sid2}/reject", "POST", {})
            self.assertEqual((d["suggestion"]["status"], d["suggestion"]["accepted"]), ("rejected", []))
            self.assertEqual(json.loads(p.target_path.read_text()), t)
            # an op that no longer applies cannot be accepted
            sid3 = a.edit_target([{"op": "add_component", "id": "shop.x"}])["suggestion"]
            model.edit_target(p, [{"op": "add_component", "id": "shop.x"}], by="editor")
            self.assertEqual(call(url, f"/api/suggestions/{sid3}/accept", "POST", {})[0], 409)
            self.assertEqual(Suggestions(p.dir).get(sid3)["status"], "pending")
        finally:
            srv.shutdown()
            srv.server_close()
        from archplan.watch import suggestion_line
        self.assertEqual(suggestion_line(sg), f"suggestion {sid} in thread {th['id']} partly accepted "
                                              "(2 of 3 ops: 0, 1): Ports out of billing")


class ThreadExtrasTest(TempProject):
    def test_snapshot_and_mentions(self):
        p = project.init(self.root)
        p.export()
        snap = {"view": "Components", "mode": "Diff", "focus": "shop.orders", "filters": {"hideLeaves": True},
                "nodes": [{"id": "shop.orders", "label": "Orders"}, "shop.billing"],
                "edges": [{"from": "shop.orders", "to": "shop.billing"}]}
        srv, url = serve_in_thread(p)
        try:
            st, th = call(url, "/api/threads", "POST", {"anchor": "comp:shop.orders", "snapshot": snap,
                                                        "text": "Why does @[Orders](comp:shop.orders) call "
                                                                "@[Invoice](type:shop.billing.invoice.Invoice)?"})
            self.assertEqual((st, th["snapshot"]), (200, snap))
            big = {"nodes": ["x" * 100] * 1000}
            self.assertEqual(call(url, "/api/threads", "POST", {"anchor": "a", "text": "b", "snapshot": big})[0], 400)
            self.assertEqual(call(url, "/api/activity")[1], {})
        finally:
            srv.shutdown()
            srv.server_close()
        r = Agent(p).read_thread(th["id"])
        self.assertEqual(r["snapshot"], "snapshot: Components view; Diff; focus: shop.orders; filters: hideLeaves; "
                                        "nodes (2): Orders, shop.billing; edges (1): shop.orders -> shop.billing")
        self.assertEqual(r["messages"][0]["text"],
                         "Why does @Orders (comp:shop.orders) call @Invoice (type:shop.billing.invoice.Invoice)?")
        self.assertEqual(mentions("see @[](task:t1) and @[a]b"), "see @task:t1 (task:t1) and @[a]b")
        self.assertEqual(snapshot_text({}), "snapshot: empty")

    def test_activity(self):
        p = project.init(self.root)
        p.export()
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "open_thread", "arguments": {"anchor": "task:split-orders", "text": "done?"}}}]
        subprocess.run([str(ROOT / "bin" / "archplan"), "mcp"], cwd=self.root, capture_output=True, text=True,
                       input="\n".join(json.dumps(x) for x in reqs) + "\n", timeout=30)
        act = json.loads(p.path("activity.json").read_text())
        self.assertEqual((act["tool"], act["summary"], act["task"]),
                         ("open_thread", "Opened a thread on task:split-orders", "split-orders"))
        th = Store(p.dir).read()["threads"][0]
        record(p, Store(p.dir), "reply", {"id": th["id"], "text": "x"})
        act = json.loads(p.path("activity.json").read_text())
        self.assertEqual((act["summary"], act["thread"], act["task"]),
                         (f"Replied in thread {th['id']}", th["id"], "split-orders"))
        record(p, Store(p.dir), "edit_target", {"ops": [{}, {}]})
        self.assertEqual(json.loads(p.path("activity.json").read_text())["summary"], "Proposed 2 change(s)")


class RefactorTest(TempProject):
    def setUp(self):
        super().setUp()
        self.p = project.init(self.root)
        self.p.export()
        self.s = Store(self.p.dir)
        self.ids = [self.s.create(f"comp:shop.{x}", x, f"refactor {x}?", "human")["id"] for x in ("a", "b", "c")]
        self.srv, self.url = serve_in_thread(self.p)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_lock_unlock_order(self):
        a, b, c = self.ids
        st, th = call(self.url, f"/api/threads/{a}/lock", "POST", {"title": "Split A", "summary": "Move x"})
        self.assertEqual((st, th["status"], th["refactor"]["order"], th["refactor"]["state"]), (200, "planned", 1, "planned"))
        self.s.reply(b, "", "human", status="resolved")
        self.assertEqual(call(self.url, f"/api/threads/{b}/lock", "POST", {"title": "B"})[1]["refactor"]["order"], 2)
        self.assertEqual(call(self.url, f"/api/threads/{a}/lock", "POST", {})[0], 409)
        self.assertEqual(call(self.url, "/api/threads/th-nope/lock", "POST", {})[0], 404)
        # a reply keeps it planned, and planned threads do not wait on the agent
        self.s.reply(a, "one more thing", "human", status="open")
        self.assertEqual(self.s.read()["threads"][0]["status"], "planned")
        self.assertEqual([t["id"] for t in Agent(self.p).list_threads(waiting_on="agent")], [c])
        self.assertEqual([t["id"] for t in Agent(self.p).status()["threads"]["waiting_on_agent"]], [c])
        st, th = call(self.url, f"/api/threads/{a}/refactor", "PUT", {"summary": "Move x and y"})
        self.assertEqual((th["refactor"]["title"], th["refactor"]["summary"]), ("Split A", "Move x and y"))
        self.assertEqual(call(self.url, f"/api/threads/{c}/refactor", "PUT", {"title": "x"})[0], 409)
        st, d = call(self.url, "/api/refactors/order", "PUT", {"ids": [b, a]})
        self.assertEqual([(t["id"], t["refactor"]["order"]) for t in d["refactors"]], [(b, 1), (a, 2)])
        self.assertEqual(call(self.url, "/api/refactors/order", "PUT", {"ids": [c]})[0], 400)
        st, th = call(self.url, f"/api/threads/{b}/unlock", "POST", {})
        self.assertEqual((th["status"], "refactor" in th), ("open", False))

    def test_handover_batches(self):
        a, b, c = self.ids
        self.assertEqual(call(self.url, "/api/handover", "POST", {"mode": "batches"})[0], 400)  # nothing planned
        for t in (a, b):
            call(self.url, f"/api/threads/{t}/lock", "POST", {"title": t})
        key = call(self.url, "/api/handover")[1]["key"]
        st, h = call(self.url, "/api/handover", "POST", {"mode": "batches", "note": "small PRs"})
        self.assertEqual((st, h["status"], h["refactors"]), (200, "sent", [a, b]))
        self.assertEqual(call(self.url, "/api/handover", "POST", {"mode": "all"})[0], 409)
        d = call(self.url, "/api/handover")[1]
        self.assertEqual((d["current"]["id"], d["history"]), (h["id"], []))
        self.assertNotEqual(d["key"], key)
        self.assertEqual(call(self.url, f"/api/threads/{a}/unlock", "POST", {})[0], 409)
        ag = Agent(self.p)
        self.assertEqual(ag.status()["handover"]["status"], "sent")
        self.assertEqual({t["id"] for t in ag.list_threads("all", "agent")}, {a, b, c})  # now they wait on the agent
        g = ag.get_handover()
        self.assertEqual([r["thread"] for r in g["refactors"]], [a, b])
        self.assertIn("propose_batches", g["todo"])
        with self.assertRaises(Conflict):  # batches first
            ag.set_refactor_state(a, "in-progress")
        for bad in ([{"title": "x", "refactors": [a]}], [{"title": "x", "refactors": [a, b, a]}],
                    [{"title": "x", "refactors": [a, b, c]}], []):
            with self.assertRaises(ValueError):
                ag.propose_batches(bad)
        self.assertEqual(ag.propose_batches([{"title": "one", "refactors": [b]}, {"title": "two", "refactors": [a]}])["status"],
                         "proposed")
        self.assertEqual(call(self.url, "/api/handover/ho-nope/approve", "POST", {})[0], 404)
        st, h2 = call(self.url, f"/api/handover/{h['id']}/approve", "POST",
                      {"batches": [{"title": "both", "refactors": [a, b], "notes": "edited"}]})
        self.assertEqual((h2["status"], len(h2["batches"]), h2["decided"] is not None), ("approved", 1, True))
        self.assertEqual(call(self.url, f"/api/handover/{h['id']}/approve", "POST", {})[0], 409)
        out = ag.set_refactor_state(a, "in-progress")
        self.assertEqual((out["status"], out["progress"]), ("in-progress", f"Working on refactor 1 of 2: {a}"))
        ag.set_refactor_state(a, "done", note="moved")
        out = ag.set_refactor_state(b, "done")
        self.assertEqual(out["status"], "done")
        d = call(self.url, "/api/handover")[1]
        self.assertEqual((d["current"], d["history"][0]["status"]), (None, "done"))
        self.assertEqual(ag.status()["threads"]["waiting_on_agent"][0]["id"], c)

    def test_handover_all_and_cancel(self):
        from archplan.watch import handover_line
        a, b, c = self.ids
        for t in (a, b):
            call(self.url, f"/api/threads/{t}/lock", "POST", {"title": t})
        st, h = call(self.url, "/api/handover", "POST", {"mode": "all", "note": "go"})
        self.assertEqual(handover_line(h, None), f"handover {h['id']} sent: 2 planned refactors, all in one go: go. "
                                                 "Call get_handover.")
        ag = Agent(self.p)
        with self.assertRaises(Conflict):
            ag.propose_batches([{"title": "x", "refactors": [a, b]}])
        self.assertEqual(ag.set_refactor_state(a, "done")["status"], "in-progress")
        ag.set_refactor_state(b, "in-progress")
        st, h = call(self.url, f"/api/handover/{h['id']}/cancel", "POST", {})
        self.assertEqual(h["status"], "cancelled")
        self.assertEqual(handover_line(h, "in-progress"), f"handover {h['id']} cancelled")
        states = {t["id"]: t["refactor"]["state"] for t in self.s.read()["threads"] if t.get("refactor")}
        self.assertEqual(states, {a: "done", b: "planned"})  # done work stays done
        self.assertEqual(call(self.url, f"/api/handover/{h['id']}/cancel", "POST", {})[0], 409)
        self.assertEqual([t["id"] for t in ag.list_threads(waiting_on="agent")], [c])
        # a new handover takes only the refactors that are not done
        self.assertEqual(call(self.url, "/api/handover", "POST", {"mode": "all"})[1]["refactors"], [b])


if __name__ == "__main__":
    unittest.main()
