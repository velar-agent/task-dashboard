import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "push-to-worker.py"
spec = importlib.util.spec_from_file_location("push_to_worker", SCRIPT)
push = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push)


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Source(QuietHandler):
    def do_GET(self):
        if self.path != "/api/tasks":
            self.send_error(404)
            return
        self.server.auth.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(self.server.snapshot, ensure_ascii=False).encode("utf-8"))


class FakeWorker(QuietHandler):
    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.puts.append((self.path, self.headers, json.loads(body)))
        if self.server.status != 200:
            # HTTP 오류 사유와 본문이 키를 되돌려 주는 경우도 검사한다.
            self.send_error(self.server.status, self.headers.get("Authorization", ""))
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


@contextmanager
def serve(handler):
    with HTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


class PushTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = root / "state.sha"
        stack.enter_context(patch.object(push, "STATE_FILE", self.state))
        self.dev_state = root / "dev.sha"
        stack.enter_context(patch.object(push, "DEV_STATE_FILE", self.dev_state))
        self.google_dev_state = root / "google-dev.sha"
        stack.enter_context(patch.object(push, "GOOGLE_DEV_STATE_FILE", self.google_dev_state))
        self.google_prod_state = root / "google-prod.sha"
        stack.enter_context(patch.object(push, "GOOGLE_PROD_STATE_FILE", self.google_prod_state))
        self.key = "unit-test-ingest-key"
        key_file = root / "key"
        key_file.write_text(self.key + "\n", encoding="ascii")

        self.source = stack.enter_context(serve(Source))
        self.source.auth = []
        self.source.snapshot = {
            "tasks": [{"task_id": "one", "events": [{"stage": "점검", "note": "확인"}]}],
            "generated_at": "2026-11-18T12:00:00+09:00",
        }
        self.worker = stack.enter_context(serve(FakeWorker))
        self.worker.status = 200
        self.worker.puts = []
        stack.enter_context(patch.dict(os.environ, {
            "SOURCE_URL": f"http://127.0.0.1:{self.source.server_port}/api/tasks",
            "WORKER_URL": f"http://127.0.0.1:{self.worker.server_port}/",
            "KEY_FILE": str(key_file),
            "no_proxy": "127.0.0.1",
            "CONNECT_IP": "",
        }))
        self.stdout = stack.enter_context(redirect_stdout(io.StringIO()))
        self.stderr = stack.enter_context(redirect_stderr(io.StringIO()))

    def tearDown(self):
        self.assertNotIn(self.key, self.stdout.getvalue())
        self.assertNotIn(self.key, self.stderr.getvalue())
        self.assertTrue(all(auth is None for auth in self.source.auth))

    def test_only_changed_tasks_are_uploaded_unless_forced(self):
        snapshot = self.source.snapshot
        self.assertEqual(push.main([]), 0)
        (path, headers, body), = self.worker.puts
        self.assertEqual(path, "/api/ingest")
        self.assertEqual(headers["Authorization"], "Bearer " + self.key)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["User-Agent"], "task-dashboard-push/1.0")
        self.assertEqual(body, snapshot)
        first_hash = self.state.read_text().strip()
        self.assertRegex(first_hash, r"^[0-9a-f]{64}$")

        snapshot["generated_at"] = "2026-11-18T12:00:15+09:00"
        snapshot["tasks"] = [{"events": [{"note": "확인", "stage": "점검"}], "task_id": "one"}]
        self.assertEqual(push.main([]), 0)
        self.assertEqual(len(self.worker.puts), 1)
        self.assertEqual(self.state.read_text().strip(), first_hash)

        snapshot["tasks"][0]["events"][0]["note"] = "변경"
        self.assertEqual(push.main([]), 0)
        self.assertEqual(len(self.worker.puts), 2)
        self.assertEqual(self.worker.puts[-1][2], snapshot)
        self.assertNotEqual(self.state.read_text().strip(), first_hash)
        self.assertEqual(push.main(["--force"]), 0)
        self.assertEqual(len(self.worker.puts), 3)
        self.assertEqual(self.worker.puts[-1][2], snapshot)

    def test_failed_update_preserves_previous_hash(self):
        self.assertEqual(push.main([]), 0)
        previous = self.state.read_bytes()
        self.source.snapshot["tasks"] = []
        self.worker.status = 503
        self.assertEqual(push.main([]), 1)
        self.assertEqual(self.state.read_bytes(), previous)
        self.assertEqual(len(self.stderr.getvalue().splitlines()), 1)
        self.assertIn("HTTP 503", self.stderr.getvalue())
        self.worker.status = 200
        self.assertEqual(push.main([]), 0)
        self.assertNotEqual(self.state.read_bytes(), previous)
        self.assertEqual(self.worker.puts[-1][2]["tasks"], [])

    def test_dev_uses_own_default_url_key_hash_and_lock(self):
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-dev").write_text("dev-fixture", encoding="ascii")
        (config / "ingest-key").write_text("prod-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent)}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                for args, name, fixture in ((["--target", "dev"], "task-dashboard-dev", "dev-fixture"),
                                            ([], "task-dashboard", "prod-fixture")):
                    self.assertEqual(push.main(args), 0)
                    upload = http.call_args.args[0]
                    self.assertEqual(upload.full_url, f"https://{name}.velar-agent.workers.dev/api/ingest")
                    self.assertEqual(upload.get_header("Authorization"), "Bearer " + fixture)
                    self.assertEqual(upload.data, body)
                self.assertEqual(self.dev_state.read_bytes(), self.state.read_bytes())
                self.assertTrue(self.dev_state.with_suffix(".lock").is_file())
                self.assertTrue(self.state.with_suffix(".lock").is_file())
                http.reset_mock()
                self.assertEqual(push.main(["--target", "dev"]), 0)
                self.assertEqual(http.call_count, 1)  # 소스 조회만 수행

    def test_dev_honors_url_and_key_overrides(self):
        self.assertEqual(push.main(["--target", "dev"]), 0)
        (_, headers, body), = self.worker.puts
        self.assertEqual(headers["Authorization"], "Bearer " + self.key)
        self.assertEqual(body, self.source.snapshot)
        self.assertTrue(self.dev_state.exists())
        self.assertFalse(self.state.exists())

    def test_google_dev_uses_own_default_url_key_hash_and_lock(self):
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        (config / "ingest-key-dev").write_text("dev-fixture", encoding="ascii")
        (config / "ingest-key").write_text("prod-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent), "CONNECT_IP": ""}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(
                    upload.full_url,
                    "http://task-dashboard-dev.velar-agent.duckdns.org/api/ingest",
                )
                self.assertEqual(upload.get_header("Authorization"), "Bearer google-fixture")
                self.assertEqual(upload.data, body)
                self.assertRegex(self.google_dev_state.read_text().strip(), r"^[0-9a-f]{64}$")
                self.assertTrue(self.google_dev_state.with_suffix(".lock").is_file())
                self.assertFalse(self.state.exists())
                self.assertFalse(self.dev_state.exists())
                http.reset_mock()
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                self.assertEqual(http.call_count, 1)  # 소스 조회만 수행

    def test_google_dev_honors_url_and_key_overrides(self):
        self.assertEqual(push.main(["--target", "google-dev"]), 0)
        (_, headers, body), = self.worker.puts
        self.assertEqual(headers["Authorization"], "Bearer " + self.key)
        self.assertEqual(body, self.source.snapshot)
        self.assertTrue(self.google_dev_state.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.dev_state.exists())

    def test_google_dev_connect_ip_rewrites_host(self):
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent), "CONNECT_IP": "192.0.2.99"}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(upload.full_url, "http://192.0.2.99/api/ingest")
                self.assertEqual(
                    upload.get_header("Host"),
                    "task-dashboard-dev.velar-agent.duckdns.org",
                )
                self.assertEqual(upload.get_header("Authorization"), "Bearer google-fixture")

    def test_google_dev_connect_ip_live_host_header(self):
        worker_url = (
            f"http://task-dashboard-dev.velar-agent.duckdns.org:{self.worker.server_port}/"
        )
        with patch.dict(os.environ, {"WORKER_URL": worker_url, "CONNECT_IP": "127.0.0.1"}):
            self.assertEqual(push.main(["--target", "google-dev"]), 0)
            (_, headers, body), = self.worker.puts
            self.assertEqual(headers["Authorization"], "Bearer " + self.key)
            self.assertEqual(body, self.source.snapshot)
            # 포트 포함 시 Host 에는 포트까지 붙는다.
            self.assertEqual(
                headers["Host"],
                f"task-dashboard-dev.velar-agent.duckdns.org:{self.worker.server_port}",
            )
            self.assertTrue(self.google_dev_state.exists())

    def test_google_prod_uses_own_default_url_key_hash_and_lock(self):
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-prod").write_text("google-prod-fixture", encoding="ascii")
        (config / "ingest-key-google-dev").write_text("google-dev-fixture", encoding="ascii")
        (config / "ingest-key").write_text("prod-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent), "CONNECT_IP": ""}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-prod"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(
                    upload.full_url,
                    "http://task-dashboard.velar-agent.duckdns.org/api/ingest",
                )
                self.assertEqual(upload.get_header("Authorization"), "Bearer " + "google-prod-fixture")
                self.assertEqual(upload.data, body)
                self.assertRegex(self.google_prod_state.read_text().strip(), r"^[0-9a-f]{64}$")
                self.assertTrue(self.google_prod_state.with_suffix(".lock").is_file())
                self.assertFalse(self.state.exists())
                self.assertFalse(self.dev_state.exists())
                self.assertFalse(self.google_dev_state.exists())

    def test_google_prod_honors_url_and_key_overrides(self):
        self.assertEqual(push.main(["--target", "google-prod"]), 0)
        (_, headers, body), = self.worker.puts
        self.assertEqual(headers["Authorization"], "Bearer " + self.key)
        self.assertEqual(body, self.source.snapshot)
        self.assertTrue(self.google_prod_state.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.dev_state.exists())
        self.assertFalse(self.google_dev_state.exists())

    def test_google_prod_connect_ip_rewrites_host(self):
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-prod").write_text("google-prod-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent), "CONNECT_IP": "192.0.2.99"}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-prod"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(upload.full_url, "http://192.0.2.99/api/ingest")
                self.assertEqual(
                    upload.get_header("Host"),
                    "task-dashboard.velar-agent.duckdns.org",
                )
                self.assertEqual(upload.get_header("Authorization"), "Bearer " + "google-prod-fixture")

    def test_google_prod_connect_ip_live_host_header(self):
        worker_url = (
            f"http://task-dashboard.velar-agent.duckdns.org:{self.worker.server_port}/"
        )
        with patch.dict(os.environ, {"WORKER_URL": worker_url, "CONNECT_IP": "127.0.0.1"}):
            self.assertEqual(push.main(["--target", "google-prod"]), 0)
            (_, headers, body), = self.worker.puts
            self.assertEqual(headers["Authorization"], "Bearer " + self.key)
            self.assertEqual(body, self.source.snapshot)
            self.assertEqual(
                headers["Host"],
                f"task-dashboard.velar-agent.duckdns.org:{self.worker.server_port}",
            )
            self.assertTrue(self.google_prod_state.exists())

    def test_google_dev_connect_ip_ipv4_rewrites_host(self):
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent), "CONNECT_IP": "203.0.113.10"}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(upload.full_url, "http://203.0.113.10/api/ingest")
                self.assertEqual(upload.get_header("Host"), "task-dashboard-dev.velar-agent.duckdns.org")
                self.assertEqual(upload.get_header("Authorization"), "Bearer " + "google-fixture")

    def test_google_dev_connect_ip_ipv6_rewrites_host(self):
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent), "CONNECT_IP": "2001:db8::1"}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(upload.full_url, "http://[2001:db8::1]/api/ingest")
                self.assertEqual(upload.get_header("Host"), "task-dashboard-dev.velar-agent.duckdns.org")
                self.assertEqual(upload.get_header("Authorization"), "Bearer " + "google-fixture")

    def test_google_dev_connect_ip_with_port_is_rejected(self):
        invalid = "192.0.2.99:8080"
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent), "CONNECT_IP": invalid}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                with self.assertRaises(push.PushError) as ctx:
                    push.push_once(True, "google-dev")
                self.assertEqual(str(ctx.exception), "CONNECT_IP 는 IP 주소여야 합니다")
                self.assertNotIn(invalid, str(ctx.exception))
                self.assertEqual(http.call_count, 1)
                self.assertFalse(self.google_dev_state.exists())

    def test_google_dev_connect_ip_hostname_is_rejected(self):
        invalid = "example.com"
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent), "CONNECT_IP": invalid}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 1)
                self.assertEqual(self.stderr.getvalue().strip(), "스냅샷 푸시 실패 - CONNECT_IP 는 IP 주소여야 합니다")
                self.assertNotIn(invalid, self.stderr.getvalue())
                self.assertEqual(http.call_count, 1)
                self.assertFalse(self.google_dev_state.exists())

    def test_google_prod_state_is_separate_from_google_dev(self):
        config = self.state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-prod").write_text("google-prod-fixture", encoding="ascii")
        (config / "ingest-key-google-dev").write_text("google-dev-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.state.parent), "CONNECT_IP": ""}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                self.assertTrue(self.google_dev_state.exists())
                self.assertFalse(self.google_prod_state.exists())
                http.reset_mock()
                # 같은 소스도 prod 해시가 분리되어 있어 다시 전송한다.
                self.assertEqual(push.main(["--target", "google-prod"]), 0)
                self.assertTrue(self.google_prod_state.exists())
                self.assertEqual(self.google_dev_state.read_bytes(), self.google_prod_state.read_bytes())

    def test_concurrent_checks_send_one_snapshot(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(push.main, ([], []))), [0, 0])
        self.assertEqual(len(self.worker.puts), 1)

    def test_loop_retries_and_uses_requested_interval(self):
        self.worker.status = 503

        def next_tick(seconds):
            self.assertEqual(seconds, 15)
            if self.worker.status == 503:
                self.assertFalse(self.state.exists())
                self.worker.status = 200
            else:
                raise KeyboardInterrupt

        with patch.object(push, "sleep", side_effect=next_tick):
            self.assertRaises(KeyboardInterrupt, push.main, ["--loop"])
        self.assertTrue(self.state.exists())
        self.assertEqual(len(self.stderr.getvalue().splitlines()), 1)
        with patch.object(push, "sleep", side_effect=KeyboardInterrupt) as sleep:
            self.assertRaises(KeyboardInterrupt, push.main, ["--loop", "2"])
        sleep.assert_called_once_with(2)
        self.assertEqual(len(self.worker.puts), 2)
