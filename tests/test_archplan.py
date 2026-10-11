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
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                e.close()
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
            srv.server_close()


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


def synthetic_model(n=6, skip_builder=(), extra=()):
    """A model with n systems, each with a public API, queries, a view with a
    builder and a model; file refs follow the convention unless told otherwise."""
    files = {}
    for i in range(n):
        d, t = f"core/systems/s{i}", f"thing{i}"
        files[f"{d}/_public.ex"] = {"component": f"S{i}", "refs": [{"to": f"{d}/_queries.ex", "line": 3}]}
        files[f"{d}/_queries.ex"] = {"component": f"S{i}", "refs": [{"to": f"{d}/{t}_model.ex", "line": 4}]}
        files[f"{d}/{t}_model.ex"] = {"component": f"S{i}", "refs": []}
        files[f"{d}/{t}_view.ex"] = {"component": f"S{i}", "refs": [{"to": f"{d}/{t}_view_builder.ex", "line": 5}]}
        if i not in skip_builder:
            files[f"{d}/{t}_view_builder.ex"] = {"component": f"S{i}", "refs": [{"to": f"{d}/_public.ex", "line": 6}]}
        files[f"{d}/h{i}.ex"] = {"component": f"S{i}", "refs": []}
        if i:  # each system calls the previous one's public API, several times over
            files[f"{d}/_public.ex"]["refs"] += [{"to": f"core/systems/s{i - 1}/_public.ex", "line": 9}]
            for k in range(4):
                files[f"{d}/x{i}{k}.ex"] = {"component": f"S{i}", "refs": [{"to": f"core/systems/s{i - 1}/_public.ex"}]}
    files["core/web/page.ex"] = {"component": "Web", "refs": []}
    for src, dst in extra:
        files[src]["refs"].append({"to": dst, "line": 1})
    comps = {f"S{i}": {"path": f"core/systems/s{i}", "match": [f"core/systems/s{i}/**"], "layer": "systems"}
             for i in range(n)}
    comps["Web"] = {"path": "core/web", "match": ["core/web/**"], "layer": "web"}
    return {"format": "archmodel/1", "components": comps, "dependencies": [], "types": {}, "files": files,
            "layers": [{"id": "web", "name": "Web"}, {"id": "systems", "name": "Systems"}]}


class ArchitectureTest(unittest.TestCase):
    def test_globs(self):
        from archplan.architecture import glob_match, role_match
        self.assertTrue(glob_match("core/systems/lab", "core/systems/*"))
        self.assertFalse(glob_match("core/systems/lab/x", "core/systems/*"))
        self.assertTrue(glob_match("src/a/b/app", "src/**"))
        self.assertTrue(role_match("models/zaak.py", "models/**"))
        self.assertTrue(role_match("sub/day_view.ex", "*_view.ex"))
        self.assertFalse(role_match("day_view_builder.ex", "*_view.ex"))

    def test_infer_and_breaks(self):
        from archplan import architecture as A
        m = synthetic_model(skip_builder=(2,), extra=[("core/systems/s3/thing3_view.ex", "core/systems/s3/_queries.ex"),
                                                      ("core/web/page.ex", "core/systems/s1/_queries.ex")])
        p = A.infer(m)
        self.assertEqual(p["source"], "inferred")
        self.assertEqual(p["units"], [{"kind": "System", "glob": "core/systems/*"}])
        roles = {r["id"]: r["patterns"] for r in p["roles"]}
        self.assertEqual(roles, {"public": ["_public.ex"], "queries": ["_queries.ex"], "model": ["*_model.ex"],
                                 "view": ["*_view.ex"], "view_builder": ["*_view_builder.ex"]})
        rules = {r["id"]: r for r in p["rules"]}
        self.assertEqual(rules["other-units-call-public"]["to"], "public")
        self.assertEqual(rules["view-has-view_builder"]["match"], ["<stem>_view.ex -> <stem>_view_builder.ex"])
        self.assertEqual([x["name"] for x in p["layers"]], ["Web", "Systems"])
        a = A.analyse(p, m)
        self.assertEqual(len(a["units"]), 6)
        self.assertEqual(a["roles"][0], {"id": a["roles"][0]["id"], "present": 6, "of": 6})
        self.assertEqual(a["unmatched"], 6 + 5 * 4)  # h<i>.ex everywhere, x*.ex in five
        res = {r["id"]: r for r in a["rules"]}
        pair = res["view-has-view_builder"]
        self.assertEqual((pair["count"], pair["breaks"][0]["to"]), (1, "core/systems/s2/thing2_view_builder.ex"))
        calls = res["other-units-call-public"]
        self.assertEqual(calls["count"], 1)
        self.assertEqual(calls["breaks"][0]["from"], "core/web/page.ex")  # code outside units counts as another unit
        self.assertEqual(a["layers"]["unassigned"], [])
        s3 = next(u for u in a["units"] if u["id"] == "core/systems/s3")
        self.assertEqual(s3["component"], "S3")
        self.assertLess(next(u for u in a["units"] if u["id"] == "core/systems/s2")["conformance"], 1)
        # a model without file references still gets roles, and says why rules cannot be checked
        m2 = dict(m, files={})
        m2["types"] = {f"T{i}": {"file": f} for i, f in enumerate(m["files"])}
        self.assertTrue(A.analyse(p, m2)["warnings"])

    def test_django(self):
        from archplan import architecture as A
        files = ["src/proj/settings.py", "src/proj/urls.py",
                 "src/proj/shop/apps.py", "src/proj/shop/models.py", "src/proj/shop/views.py",
                 "src/proj/shop/api/serializers.py", "src/proj/shop/api/viewsets.py",
                 "src/proj/shop/api/helpers.py",
                 "src/proj/billing/apps.py", "src/proj/billing/models/__init__.py",
                 "src/proj/billing/models/invoice.py", "src/proj/billing/admin.py",
                 "src/proj/billing/templatetags/money.py"]
        refs = {"src/proj/shop/models.py": ["src/proj/shop/views.py"],  # models import a view: a break
                "src/proj/billing/admin.py": ["src/proj/shop/views.py"],  # another app's views: a break
                "src/proj/shop/api/serializers.py": ["src/proj/shop/api/viewsets.py", "src/proj/billing/models/invoice.py"]}
        m = {"components": {}, "types": {}, "files": {f: {"refs": [{"to": t} for t in refs.get(f, [])]} for f in files}}
        self.assertTrue(A.is_django(files))
        self.assertFalse(A.is_django(["src/a/apps.py", "src/a/models.py"]))
        p = A.infer(m)
        self.assertEqual(p["source"], "django")
        self.assertEqual(p["units"][0]["glob"], "src/proj/**")
        a = A.analyse(p, m)
        self.assertEqual([u["id"] for u in a["units"]], ["src/proj/billing", "src/proj/shop"])
        shop = {f["path"].rsplit("/", 1)[-1]: f["role"] for f in a["units"][1]["files"]}
        self.assertEqual(shop, {"apps.py": None, "models.py": "models", "views.py": "views",
                                "serializers.py": "serializers", "viewsets.py": "viewsets", "helpers.py": None})
        billing = {f["path"].rsplit("/", 1)[-1]: f["role"] for f in a["units"][0]["files"]}
        self.assertEqual((billing["invoice.py"], billing["money.py"]), ("models", "templatetags"))
        counts = {r["id"]: r["count"] for r in a["rules"]}
        self.assertEqual(counts, {"models-stay-low": 1, "apps-keep-views-private": 1, "serializers-not-views": 1})

    def test_hostile_patterns(self):
        from archplan.architecture import glob_match, pair_specs
        self.assertFalse(glob_match("/".join(["a"] * 40), "/".join(["**", "a"] * 20) + "/b"))
        self.assertTrue(glob_match("a", "a/**"))
        self.assertEqual(pair_specs({"match": ["<stem><stem>.ex -> x"]}), [])

    def test_colors(self):
        from archplan import architecture as A
        bad = {"roles": [{"id": "x", "patterns": [], "color": '"/><script>'}]}
        with self.assertRaises(ValueError):
            A.validate(bad)
        self.assertNotIn("color", A.clean_colors(bad)["roles"][0])
        self.assertEqual(A.validate({"roles": [{"id": "x", "patterns": [], "color": "#3b82f6"}]})["roles"][0]["color"], "#3b82f6")


class ArchitectureApiTest(TempProject):
    def test_round_trip(self):
        p = project.init(self.root)
        p.model_path.write_text(json.dumps(synthetic_model()))
        srv, url = serve_in_thread(p)
        try:
            st, d = call(url, "/api/architecture")
            self.assertEqual((st, d["profile"]), (200, None))
            prop = d["proposal"]
            self.assertEqual(len(d["analysis"]["units"]), 6)
            key = d["key"]
            prop["roles"][0]["name"] = "Public API"
            st, d = call(url, "/api/architecture", "PUT", {"profile": prop})
            self.assertEqual((st, d["profile"]["status"]), (200, "proposed"))
            self.assertNotEqual(d["key"], key)
            self.assertEqual(call(url, "/api/architecture", "PUT", {"profile": {"roles": [{"name": "x"}]}})[0], 400)
            st, d = call(url, "/api/architecture/lock", "POST", {})
            self.assertEqual(d["profile"]["status"], "locked")
            st, d = call(url, "/api/architecture/infer", "POST", {})
            self.assertEqual((st, d["saved"]), (200, False))  # a locked profile stays
            st, d = call(url, "/api/architecture")
            self.assertEqual((d["profile"]["status"], d["profile"]["roles"][0]["name"]), ("locked", "Public API"))
            self.assertNotIn("proposal", d)
        finally:
            srv.shutdown()
            srv.server_close()
        a = Agent(p)
        self.assertEqual(a.get_architecture()["status"], "locked")
        with self.assertRaises(ValueError):
            a.update_profile(d["profile"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["profile", "--dir", str(self.root)])
        self.assertIn("_public.ex", buf.getvalue())

    def test_python_exporter_files(self):
        m = export(self.root)
        f = m["files"]["shop/billing/invoice.py"]
        self.assertEqual(f["component"], "shop.billing")
        self.assertEqual([r["to"] for r in f["refs"]], ["shop/orders/model.py"])  # "from ..orders import model" is the module
        self.assertEqual([r["to"] for r in m["files"]["shop/orders/model.py"]["refs"]], ["shop/billing/invoice.py"])


def findings_model(ab=5):
    """Four components with a 2-cycle (A, D) and a 3-cycle (A, B, C), a type
    with groups of related fields and two types that share their fields."""
    comps = {c: {"name": c, "path": f"lib/{c.lower()}", "match": [f"lib/{c.lower()}/**"]} for c in "ABCD"}
    deps = [("A", "B", ab), ("B", "C", 1), ("C", "A", 3), ("A", "D", 2), ("D", "A", 4)]
    fld = lambda *names: [{"name": n, "type": "string"} for n in names]
    types = {
        "A.User": {"name": "User", "component": "A", "kind": "struct", "file": "lib/a/user.ex", "line": 3,
                   "fields": fld("id", "payout_iban", "payout_name", "payout_status", "verified_email", "verified_phone",
                                 "verified_at", "notify_mail", "notify_sms", "notify_push", "name")},
        "B.Member": {"name": "Member", "component": "B", "kind": "struct", "file": "lib/b/member.ex",
                     "fields": fld("id", "user_id", "public_id", "status", "role", "joined_at", "inserted_at")},
        "C.Participant": {"name": "Participant", "component": "C", "kind": "struct", "file": "lib/c/participant.ex",
                          "fields": fld("id", "user_id", "public_id", "status", "role", "joined_at", "updated_at")},
    }
    return {"format": "archmodel/1", "components": comps, "types": types,
            "dependencies": [{"from": a, "to": b, "weight": w, "evidence": [f"lib/{a.lower()}/x.ex:{w}"]}
                             for a, b, w in deps]}


class FindingsTest(TempProject):
    def setUp(self):
        super().setUp()
        self.p = project.init(self.root)
        self.p.model_path.write_text(json.dumps(findings_model()))

    def test_cycles_cheapest_cut(self):
        from archplan.findings import cycle_findings, sccs
        m = findings_model()
        self.assertEqual(sorted(sccs("ABCDE", {("A", "B"), ("B", "A"), ("C", "D")})), [["A", "B"], ["C"], ["D"], ["E"]])
        cyc = {tuple(f["evidence"]["cycle"]): f for f in cycle_findings(m)}
        self.assertEqual(set(cyc), {("A", "D"), ("A", "B", "C")})
        self.assertEqual(cyc[("A", "D")]["evidence"]["cut"]["from"], "A")
        abc = cyc[("A", "B", "C")]
        self.assertEqual((abc["evidence"]["cut"]["from"], abc["evidence"]["cut"]["to"], abc["evidence"]["cut"]["weight"]),
                         ("B", "C", 1))
        self.assertEqual(abc["title"], "A → B → C → A form a dependency cycle.")
        self.assertEqual(abc["files"], ["lib/b/x.ex"])

    def test_fingerprints_stable(self):
        from archplan.findings import analyse
        ids = lambda m: {f["id"]: f["kind"] for f in analyse(self.p)[0]}
        before = ids(None)
        self.assertEqual(sorted(set(before.values())), ["structure.cycle", "types.fields", "types.synonyms"])
        self.p.model_path.write_text(json.dumps(findings_model(ab=50)))  # counts change, the findings do not
        os.utime(self.p.model_path, (1, 1))
        self.assertEqual(ids(None), before)

    def test_types(self):
        from archplan.findings import field_findings, synonym_findings
        m = findings_model()
        (f,) = field_findings(m)
        self.assertEqual(f["title"], "A.User has 3 groups of related fields.")
        self.assertEqual(sorted(f["evidence"]["groups"]), ["notify", "payout", "verified"])
        (s,) = synonym_findings(m)
        self.assertEqual(s["evidence"]["types"], ["B.Member", "C.Participant"])
        self.assertEqual(s["strength"], "strong")  # id and timestamps do not count

    def test_baseline_round_trip(self):
        from archplan.findings import Findings
        F = Findings(self.p)
        fs = F.listing()["findings"]
        a, b = fs[0]["id"], fs[1]["id"]
        with self.assertRaises(ValueError):
            F.decide(a, "dismissed", " ")  # a reason is required
        F.decide(a, "dismissed", "known, fixed next quarter")
        F.decide(b, "accepted")
        st = {f["id"]: (f["state"], f["reason"]) for f in Findings(self.p).listing()["findings"]}
        self.assertEqual(st[a], ("dismissed", "known, fixed next quarter"))
        self.assertEqual(st[b], ("accepted", None))
        saved = json.loads(self.p.path("baseline.json").read_text())
        self.assertEqual((saved["version"], saved["findings"][a]["title"]), (1, fs[0]["title"]))
        F.decide(a, "new")
        self.assertEqual(Findings(self.p).get(a)["state"], "new")
        F.label(b, "likely the same", "both hold a user's membership")
        self.assertEqual(Findings(self.p).get(b)["label"], "likely the same")
        # a baselined finding that leaves the code counts as fixed
        m = findings_model()
        m["dependencies"] = [d for d in m["dependencies"] if (d["from"], d["to"]) != ("D", "A")]
        self.p.model_path.write_text(json.dumps(m))
        cyc = next(f for f in fs if f["kind"] == "structure.cycle" and f["evidence"]["cycle"] == ["A", "D"])
        F.decide(cyc["id"], "accepted", "") if cyc["id"] != b else None
        os.utime(self.p.model_path, (2, 2))
        self.assertEqual(Findings(self.p).listing()["counts"]["fixed"], 1)
        r = Findings(self.p).update_baseline()
        self.assertEqual(r["removed"], 1)
        self.assertEqual(Findings(self.p).listing()["counts"]["new"], 0)

    def test_untrusted_baseline(self):
        from archplan.findings import Findings
        fid = Findings(self.p).listing()["findings"][0]["id"]
        self.p.path("baseline.json").write_text(json.dumps({"version": 9, "findings": {
            fid: {"state": "<script>", "reason": "x"}, "other": {"state": "dismissed", "reason": ["r" * 900], "at": 5},
            "bad": "nope"}, "labels": {fid: {"label": 7, "note": {"a": 1}}}}))
        lst = Findings(self.p).listing()
        f = next(x for x in lst["findings"] if x["id"] == fid)
        self.assertEqual((f["state"], f["label"], f["label_note"]), ("new", "7", "{'a': 1}"))
        e = Findings(self.p).baseline.read()["findings"]["other"]
        self.assertEqual((len(e["reason"]), e["at"], e["title"]), (500, "5", None))
        self.p.path("baseline.json").write_text("not json")
        self.assertEqual(Findings(self.p).listing()["counts"]["accepted"], 0)

    def test_check_exit_codes(self):
        def run(*args):
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit) as e, \
                    unittest.mock.patch("sys.stderr", io.StringIO()):
                cli.main(["check", "--dir", str(self.root), *args])
            return e.exception.code, buf.getvalue()
        code, out = run()
        self.assertEqual(code, 1)
        self.assertIn("form a dependency cycle", out)
        code, out = run("--format", "github")
        self.assertIn("::warning file=lib/b/x.ex,line=1,title=archplan structure.cycle::", out)
        code, out = run("--json")
        self.assertEqual(json.loads(out)["counts"]["new"], len(json.loads(out)["findings"]))
        self.assertEqual(run("--update-baseline")[0], 0)
        self.assertEqual(run()[0], 0)
        self.assertIn("[accepted: baseline]", run("--all")[1])
        self.p.model_path.unlink()
        self.assertEqual(run()[0], 2)

    def test_api_discuss(self):
        srv, url = serve_in_thread(self.p)
        try:
            st, d = call(url, "/api/findings")
            self.assertEqual(st, 200)
            self.assertEqual(d["counts"]["by_group"]["structure"], 2)
            self.assertIn("findings_etag", d)
            key = d["key"]
            f = next(x for x in d["findings"] if x["kind"] == "types.fields")
            fid = f["id"]
            st, r = call(url, f"/api/findings/{fid}/discuss", "POST", {})
            self.assertTrue(r["created"])
            th = r["thread"]
            self.assertEqual(th["anchor"], f"finding:{fid}")
            self.assertEqual(th["snapshot"]["evidence"]["groups"]["payout"][0], "payout_iban")
            st, r2 = call(url, f"/api/findings/{fid}/discuss", "POST", {"text": "again"})
            self.assertEqual((r2["created"], r2["thread"]["id"]), (False, th["id"]))
            self.assertEqual(r2["finding"]["thread"], th["id"])
            self.assertEqual(len(Store(self.p.dir).read()["threads"]), 1)
            self.assertEqual(call(url, f"/api/findings/{fid}/dismiss", "POST", {})[0], 400)
            st, r = call(url, f"/api/findings/{fid}/dismiss", "POST", {"reason": "legacy"})
            self.assertEqual((st, r["finding"]["state"]), (200, "dismissed"))
            self.assertNotEqual(r["key"], key)
            self.assertEqual(call(url, f"/api/findings/{fid}/reopen", "POST", {})[1]["finding"]["state"], "new")
            self.assertEqual(call(url, "/api/findings/nope/accept", "POST", {})[0], 404)
        finally:
            srv.shutdown()
            srv.server_close()
        a = Agent(self.p)
        item = a.read_thread(th["id"])["item"]
        self.assertEqual(item["id"], fid)
        self.assertEqual(a.label_finding(fid, "worth splitting")["label"], "worth splitting")
        got = a.get_findings(group="types")
        self.assertEqual({x["kind"] for x in got["findings"]}, {"types.fields", "types.synonyms"})


VOCAB_TREE = {
    "lib/billing/invoice.py": """\
        class Invoice:
            def send_invoice(self, invoice_line, payment_term):
                # an invoice goes to the debtor
                return invoice_line.debtor_name
        def invoice_total(invoices): ...
        def overdue_invoices(debtor): ...
        """,
    "lib/billing/debtor.py": "def remind_debtor(debtor, invoice): return debtor.invoice_line\n",
    "lib/shop/cart.py": """\
        class Cart:
            def add_product(self, product, cart_line): ...
            def checkout(self, cart, voucher): return self.invoice
        def empty_cart(cart): ...
        def apply_voucher(cart, voucher_code): ...
        """,
    "lib/shop/product.py": "class Product:\n    def price(self, product_variant): return product_variant.price\n",
    "lib/users/user.py": "class User:\n    def login(self, password, user_session): ...\n    def avatar(self, user): ...\n",
    "lib/users/profile.py": "def user_profile(user, avatar_url, nickname): return nickname\n",
    "lib/search/index.py": "def rebuild_index(index, shard): return shard.tokens\n",
    "lib/mail/outbox.py": "def queue_mail(outbox, mail, recipient): return recipient\n",
}


def vocab_model():
    comps = {c: {"name": c, "path": f"lib/{c}", "match": [f"lib/{c}/**"]} for c in ("billing", "shop", "users", "search", "mail")}
    types = {"billing.Invoice": {"name": "Invoice", "component": "billing", "file": "lib/billing/invoice.py"},
             "shop.Cart": {"name": "Cart", "component": "shop", "file": "lib/shop/cart.py"}}
    files = {f: {"component": f.split("/")[1], "refs": []} for f in VOCAB_TREE}
    return {"format": "archmodel/1", "components": comps, "dependencies": [], "types": types, "files": files}


class VocabTest(TempProject):
    def setUp(self):
        super().setUp()
        write(self.root, VOCAB_TREE)
        self.p = project.init(self.root)
        self.p.model_path.write_text(json.dumps(vocab_model()))

    def test_paths_stay_inside(self):
        from archplan.vocab import inside
        outside = Path(tempfile.mkdtemp())
        (outside / "secret.py").write_text("secret_token = 1\n")
        os.symlink(outside / "secret.py", self.root / "link.py")
        for rel in ("../secret.py", "/etc/passwd", str(outside / "secret.py"), "link.py"):
            self.assertIsNone(inside(self.root, rel), rel)
        self.assertIsNotNone(inside(self.root, "a/b.py"))

    def test_words(self):
        from archplan.vocab import fold, fold_map, split, term_key, words
        self.assertEqual(split("parseHTTPRequest_v2"), ["parse", "http", "request", "v", "2"])
        self.assertEqual(words("Systems.Assignment.CrewTasks"), ["system", "assignment", "crew", "task"])
        self.assertEqual([fold(w) for w in ("entities", "batches", "carts", "status", "address", "boxes", "bus")],
                         ["entity", "batch", "cart", "status", "address", "box", "bus"])
        self.assertEqual(term_key("CART_ITEMS"), term_key("cart item"))
        self.assertEqual(fold_map({"zaken", "adres", "carts", "cart"}),
                         {"zaken": "zaken", "adres": "adres", "carts": "cart", "cart": "cart"})

    def test_tfidf_and_ignore(self):
        from archplan.vocab import Vocab, clean_ignore
        v = Vocab(self.p)
        res = v.candidates("billing")
        terms = [t["term"] for t in res["components"]["billing"]["terms"]]
        # debtor only occurs in billing, so its idf beats invoice, which shop also uses
        self.assertEqual(terms[:2], ["invoice", "debtor"])
        self.assertIn("invoice line", terms)  # a bigram inside one identifier
        self.assertNotIn("self", terms)
        inv = res["components"]["billing"]["terms"][0]
        self.assertEqual(inv["others"], [{"component": "shop", "count": 1}])
        self.assertEqual(inv["owner_guess"], "billing")
        self.assertIn("billing.Invoice", inv["types"])
        self.assertTrue(inv["sites"][0].startswith("lib/billing/"))
        self.assertTrue(self.p.path("vocab-cache.json").exists())
        self.assertIn("vocab-cache.json", self.p.path(".gitignore").read_text())
        v.set_ignored("Debtors", "billing", True)
        v.set_ignored("voucher", None, True)
        self.assertNotIn("debtor", [t["term"] for t in v.candidates("billing")["components"]["billing"]["terms"]])
        self.assertNotIn("voucher", [t["term"] for t in v.candidates("shop")["components"]["shop"]["terms"]])
        flagged = v.candidates("billing", ignored=True)["components"]["billing"]
        self.assertEqual(next(t for t in flagged["terms"] if t["term"] == "debtor")["ignored"], "here")
        self.assertEqual(flagged["ignored"], ["debtor"])
        v.set_ignored("debtor", "billing", False)
        self.assertEqual(v.ignored(), {"ignore": ["voucher"], "ignore_in": {}})
        self.assertEqual(clean_ignore({"ignore": ["X" * 500, 3, {"a": 1}, None], "ignore_in": {"c": "nope", "d": ["Carts"]}}),
                         {"ignore": ["x" * 60], "ignore_in": {"d": ["cart"]}})
        self.assertEqual(clean_ignore("junk"), {"ignore": [], "ignore_in": {}})

    def test_glossary_enrichment(self):
        from archplan.vocab import enrich, parse_glossary
        g = parse_glossary("---\nowns:\n  Invoice: [bill]  # the main one\n  debtor:\n    - payer\nuses: []\n"
                           "avoid:\n  - cart\n---\n**Invoice**: what a debtor owes.\n")
        self.assertEqual(g["owns"], {"Invoice": ["bill"], "debtor": ["payer"]})
        self.assertEqual((g["avoid"], g["definitions"]["invoice"]), (["cart"], "what a debtor owes."))
        write(self.root, {"lib/billing/GLOSSARY.md": "---\nowns:\n  Invoice: []\nuses: []\navoid: [cart]\n---\n"
                                                     "**Invoice**: what a debtor owes.\n",
                          "lib/shop/GLOSSARY.md": "---\nowns:\n  Cart: []\n---\n"})
        self.assertTrue(enrich(self.p))
        v = json.loads(self.p.model_path.read_text())["vocabulary"]
        self.assertEqual(v["concepts"]["invoice"]["definition"], "what a debtor owes.")
        self.assertEqual(v["components"]["billing"]["avoid"], ["cart"])
        self.assertIn("debtor", [x["term"] for x in v["components"]["billing"]["suggested"]])
        rows = {(u["component"], u["concept"]): u for u in v["usage"]}
        self.assertEqual(rows[("shop", "invoice")]["idents"], 1)
        self.assertEqual(rows[("shop", "invoice")]["evidence"], ["lib/shop/cart.py:3"])
        m = json.loads(self.p.model_path.read_text())
        self.assertEqual([(x["component"], x["concept"], x["why"]) for x in model.leaks(m)], [])
        m["vocabulary"]["components"]["shop"] = {"avoid": ["invoice"]}
        self.assertEqual([(x["component"], x["concept"], x["why"]) for x in model.leaks(m)],
                         [("shop", "invoice", "avoid")])
        self.assertFalse(enrich(self.p))  # a model with a vocabulary is left alone

    def test_propose_glossary(self):
        a = Agent(self.p)
        draft = {"billing": {"concepts": [{"name": "Invoice", "terms": ["invoice", "bill"],
                                           "definition": "What a debtor owes.", "kind": "domain"},
                                          {"name": "Debtor"}]},
                 "shop": {"concepts": [{"name": "Cart"}], "uses": ["invoice"], "avoid": ["Debtor"]}}
        out = a.propose_glossary(draft, "first glossaries")
        sg = Suggestions(self.p.dir).get(out["suggestion"])
        self.assertEqual([o["op"] for o in sg["ops"]], ["add_concept", "add_concept", "add_concept", "set_glossary"])
        self.assertEqual(sg["ops"][0], {"op": "add_concept", "id": "invoice", "name": "Invoice", "owner": "billing",
                                        "terms": ["invoice", "bill"], "kind": "domain",
                                        "definition": "What a debtor owes."})
        self.assertEqual(sg["ops"][3], {"op": "set_glossary", "component": "shop", "uses": ["invoice"],
                                        "avoid": ["debtor"]})
        Suggestions(self.p.dir).decide(self.p, sg["id"], None)
        t = json.loads(self.p.target_path.read_text())["vocabulary"]
        self.assertEqual(t["concepts"]["debtor"]["terms"], ["debtor"])
        self.assertEqual(t["components"]["shop"], {"uses": ["invoice"], "avoid": ["debtor"]})
        for bad, msg in [({"nope": {"concepts": [{"name": "X"}]}}, "no component"),
                         ({"shop": {"concepts": [{"name": "Invoice"}]}}, "already owned by billing"),
                         ({"shop": {"concepts": [{"name": "A"}]}, "users": {"concepts": [{"name": "a"}]}}, "twice"),
                         ({"users": {"uses": ["ghost"]}}, "unknown concept"),
                         ({"billing": {"uses": ["Invoice"]}}, "owns itself")]:
            with self.assertRaises(ValueError) as e:
                a.propose_glossary(bad)
            self.assertIn(msg, str(e.exception))

    def test_api(self):
        srv, url = serve_in_thread(self.p)
        try:
            st, d = call(url, "/api/vocabulary/candidates?top=3")
            self.assertEqual((st, d["n_components"], set(d["components"])), (200, 5, {"billing", "shop", "users", "search", "mail"}))
            self.assertLessEqual(len(d["components"]["billing"]["terms"]), 3)
            key = d["key"]
            self.assertEqual(call(url, "/api/vocabulary/candidates?component=nope")[0], 404)
            st, d = call(url, "/api/vocabulary/ignore", "POST", {"term": "invoice"})
            self.assertEqual((st, d["ignore"]), (200, ["invoice"]))
            self.assertNotEqual(d["key"], key)
            self.assertEqual(call(url, "/api/vocabulary/ignore", "POST", {"term": ["x"]})[0], 400)
            st, d = call(url, "/api/vocabulary/unignore", "POST", {"term": "invoice"})
            self.assertEqual(d["ignore"], [])
            st, d = call(url, "/api/vocabulary/draft", "POST", {"components": ["billing"], "note": "start here"})
            self.assertEqual((st, d["thread"]["anchor"]), (200, "glossary"))
            self.assertIn("@[billing](comp:billing)", d["thread"]["messages"][0]["text"])
            self.assertEqual(call(url, "/api/vocabulary/draft", "POST", {"components": ["ghost"]})[0], 404)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(Agent(self.p).list_threads(waiting_on="agent")[0]["anchor"], "glossary")


if __name__ == "__main__":
    unittest.main()
