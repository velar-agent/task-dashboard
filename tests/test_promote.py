import email
import importlib.util
import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit


WORKER = Path(__file__).resolve().parents[1] / "worker"
spec = importlib.util.spec_from_file_location("promote", WORKER / "scripts" / "promote-dev-to-prod.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)


def response(value, status=200):
    return status, {"content-type": "application/json"}, json.dumps(value).encode()


def content(modules, entry):
    parts = []
    for name, body in modules.items():
        header = (f'--fixture\r\nContent-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
                  'Content-Type: application/javascript+module\r\n\r\n')
        parts.append(header.encode() + body + b"\r\n")
    return 200, {"content-type": 'multipart/form-data; boundary="fixture"', "cf-entrypoint": entry}, (
        b"".join(parts) + b"--fixture--\r\n")


class FakeWorkers:
    """HTTP 경계에서 코드, 업로드 목록과 트래픽을 서로 독립적으로 관리한다."""

    def __init__(self, case):
        self.case = case
        self.account = promote.ACCOUNT
        self.local = {name: (WORKER / "src" / name).read_bytes() for name in promote.MODULES}
        self.dev = self.local.copy()
        self.dev_entry = "index.js"
        self.code = {"old": {"index.js": b"/* bundled */"}}
        self.latest = "old"
        self.traffic = [{"version_id": "old", "percentage": 100}]
        bindings = [dict(promote.STORE), {"name": "INGEST_KEY", "type": "secret_text"}]
        self.bindings = {"old": bindings, "new": bindings.copy()}
        self.writes = []
        self.probes = []
        self.lost = set()
        self.broken = set()
        self.dev_page = promote.PAGE.encode()

    def __call__(self, method, url, data=None, headers=None):
        addr = urlsplit(url)
        active = self.traffic[0]["version_id"]
        if addr.netloc != "api.cloudflare.com":
            self.case.assertIn(addr.netloc, (urlsplit(promote.DEV_URL).netloc, urlsplit(promote.PROD_URL).netloc))
            self.case.assertNotIn("Authorization", headers or {})
            env = "dev" if addr.netloc == urlsplit(promote.DEV_URL).netloc else active
            self.probes.append((env, method, addr.path))
            if addr.path == "/healthz":
                return response({"status": "ok"})
            if addr.path == "/":
                page = self.dev_page if env == "dev" else promote.PAGE.encode()
                return 200, {"content-type": "text/html; charset=utf-8"}, b"<html>old</html>" if env == "old" else page
            if addr.path == "/api/tasks":
                return response({"tasks": None if env in self.broken else []})
            self.case.assertEqual((method, addr.path, data), ("PUT", "/api/ingest", b"{}"))
            return response({}, 401)

        self.case.assertEqual(headers["Authorization"], "Bearer offline-fixture")
        prefix = f"/client/v4/accounts/{self.account}/workers/scripts/"
        self.case.assertTrue(addr.path.startswith(prefix))
        name, _, path = addr.path.removeprefix(prefix).partition("/")
        if method == "GET" and path == "content/v2":
            if name == promote.DEV:
                return content(self.dev, self.dev_entry)
            self.case.assertEqual(name, promote.PROD)
            return content(self.code[active], "index.js")
        self.case.assertEqual(name, promote.PROD)
        if method == "POST":
            self.writes.append((path, addr.query))
        if (method, path) == ("GET", "deployments"):
            result = {"deployments": [{"id": "deployment", "versions": self.traffic}]}
        elif (method, path) == ("GET", "versions"):
            result = {"items": [{"id": self.latest}]}
        elif method == "GET" and path.startswith("versions/"):
            result = {"resources": {"bindings": self.bindings[path.split("/")[1]]}}
        elif (method, path) == ("POST", "versions"):
            self.case.assertEqual(addr.query, "bindings_inherit=strict")
            msg = email.message_from_bytes(b"Content-Type: " + headers["Content-Type"].encode() + b"\r\n\r\n" + data)
            self.case.assertTrue(msg.is_multipart())
            modules = {}
            for part in msg.get_payload():
                key = part.get_param("name", header="content-disposition")
                body = part.get_payload(decode=True)
                if key == "metadata":
                    self.case.assertEqual(part.get_content_type(), "application/json")
                    self.meta = json.loads(body)
                else:
                    self.case.assertEqual(part.get_content_type(), "application/javascript+module")
                    self.case.assertEqual(part.get_filename(), key)
                    modules[key] = body
            self.latest = "new"
            self.code["new"] = modules
            result = {"id": "new"}
        elif (method, path) == ("POST", "deployments"):
            payload = json.loads(data)
            self.case.assertEqual(payload["strategy"], "percentage")
            version, = payload["versions"]
            self.case.assertEqual(version["percentage"], 100)
            self.traffic = [version]
            if version["version_id"] in self.lost:
                return response({"error": "offline-fixture"}, 502)
            result = {"id": "deployment"}
        else:
            self.case.fail(f"예상하지 않은 관리 API 호출 {method} {path}")
        return response({"success": True, "result": result})


class PromoteTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWorkers(self)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.http = stack.enter_context(patch.object(promote, "http", side_effect=self.fake))
        stack.enter_context(patch.object(promote.time, "sleep"))
        stack.enter_context(patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "offline-fixture"}))
        self.log = io.StringIO()
        stack.enter_context(redirect_stdout(self.log))
        stack.enter_context(redirect_stderr(self.log))

    def tearDown(self):
        self.assertNotIn("offline-fixture", self.log.getvalue())

    def test_deploy_uploads_exact_code_and_inherits_only_prod_secret(self):
        self.fake.account = "1" * 32
        self.assertEqual(promote.main(["deploy", "--account", self.fake.account]), 0)
        self.assertEqual(self.fake.writes, [("versions", "bindings_inherit=strict"), ("deployments", "")])
        self.assertEqual(self.fake.code["new"], self.fake.local)
        cfg = tomllib.loads((WORKER / "wrangler.toml").read_text())
        self.assertEqual(self.fake.meta, {
            "main_module": "index.js", "compatibility_date": cfg["compatibility_date"],
            "bindings": [{"type": "durable_object_namespace", "name": "STORE", "class_name": "TaskStore"},
                         {"type": "inherit", "name": "INGEST_KEY"}],
        })
        self.assertEqual(self.fake.probes, [("new", "GET", "/healthz"), ("new", "GET", "/"),
                                            ("new", "GET", "/api/tasks"), ("new", "PUT", "/api/ingest")])

    def test_changed_missing_extra_module_or_wrong_entry_refuses_force(self):
        for changed, entry in (({"index.js": b"different"}, "index.js"),
                               ({"html.js": b"different"}, "index.js"),
                               ({"extra.js": b""}, "index.js"), ({}, "html.js")):
            with self.subTest(entry=entry, changed=list(changed)):
                self.fake.dev = self.fake.local | changed
                self.fake.dev_entry = entry
                self.assertEqual(promote.main(["deploy", "--force"]), 3)
        self.fake.dev = {"index.js": self.fake.local["index.js"]}
        self.fake.dev_entry = "index.js"
        self.assertEqual(promote.main(["deploy"]), 3)
        self.assertEqual(self.fake.writes, [])

    def test_skip_dry_run_and_force(self):
        self.assertEqual(promote.main(["deploy", "--dry-run"]), 0)
        self.fake.code["old"] = self.fake.local
        self.assertEqual(promote.main(["deploy"]), 0)
        self.assertIn("SKIP", self.log.getvalue())
        self.assertEqual(promote.main(["deploy", "--dry-run", "--force"]), 0)
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(promote.main(["deploy", "--force"]), 0)
        self.assertEqual(len(self.fake.writes), 2)

    def test_gate_applies_even_when_code_matches(self):
        self.fake.code["old"] = self.fake.local
        self.fake.latest = "other"
        self.assertEqual(promote.main(["deploy"]), 7)
        self.fake.latest = "old"
        self.fake.traffic[0]["percentage"] = 50
        self.assertEqual(promote.main(["deploy"]), 7)
        self.fake.traffic[0]["percentage"] = 100
        store, secret = self.fake.bindings["old"]
        for invalid in ([store], [secret], [store, dict(secret, type="plain_text")],
                        [dict(store, class_name="Other"), secret], [dict(store, script_name=promote.DEV), secret]):
            self.fake.bindings["old"] = invalid
            self.assertEqual(promote.main(["deploy", "--force"]), 7)
        self.assertEqual(self.fake.writes, [])

    def test_new_version_with_missing_binding_gets_no_traffic(self):
        self.fake.bindings["new"] = [promote.STORE]
        self.assertEqual(promote.main(["deploy"]), 4)
        self.assertEqual(self.fake.writes, [("versions", "bindings_inherit=strict")])
        self.assertEqual(self.fake.traffic[0]["version_id"], "old")

    def test_rollback_restores_previous_version_and_checks_its_own_page(self):
        self.fake.broken.add("new")
        self.assertEqual(promote.main(["deploy"]), 5)
        self.assertEqual(self.fake.traffic, [{"version_id": "old", "percentage": 100}])
        self.assertEqual(self.fake.probes[-1], ("old", "PUT", "/api/ingest"))
        self.assertEqual(promote.main(["deploy", "--force"]), 7)
        self.assertEqual(len(self.fake.writes), 3)

    def test_lost_deployment_and_rollback_replies_are_reconciled(self):
        self.fake.lost = {"new", "old"}
        self.fake.broken.add("new")
        self.assertEqual(promote.main(["deploy"]), 5)
        self.assertEqual(len(self.fake.writes), 3)

    def test_failed_rollback_health_requires_manual_action(self):
        self.fake.broken = {"new", "old"}
        self.assertEqual(promote.main(["deploy"]), 6)
        self.assertEqual(self.fake.traffic[0]["version_id"], "old")

    def test_precheck_runs_both_suites_and_compares_dev_page(self):
        with patch.object(promote.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(promote.main(["precheck"]), 0)
            self.assertEqual([call.args[0][1:] for call in run.call_args_list], [
                ["worker/scripts/sync-html.py", "--check"], ["--test", "worker/test/"], ["-m", "unittest"]])
            self.assertTrue(all(call.kwargs["cwd"] == WORKER.parent for call in run.call_args_list))
            self.fake.dev_page = b"<html>different</html>"
            self.assertEqual(promote.main(["precheck"]), 1)
            self.http.reset_mock()
            run.return_value.returncode = 1
            self.assertEqual(promote.main(["precheck"]), 1)
            self.http.assert_not_called()

    def test_wrong_target_and_token_errors_precede_api_calls(self):
        self.assertEqual(promote.main(["deploy", "--name", promote.DEV]), 2)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": ""}):
            path = Path(tmp) / "token"
            args = ["status", "--token-file", str(path)]
            self.assertEqual(promote.main(args), 2)
            path.write_text("offline-fixture")
            path.chmod(0o644)
            self.assertEqual(promote.main(args), 2)
            self.http.assert_not_called()
            path.chmod(0o600)
            self.assertEqual(promote.main(args), 0)
            self.assertIn("old@100%", self.log.getvalue())
