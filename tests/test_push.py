import importlib.util
import io
import json
import os
import shutil
import ssl
import subprocess
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


@contextmanager
def serve_tls(handler, cert_file, key_file, sni=None):
    server = HTTPServer(("127.0.0.1", 0), handler)
    # 핸드셰이크 실패가 테스트 stderr를 오염시키지 않게 한다.
    def _silence(*args, **kwargs):
        pass

    server.handle_error = _silence
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    if sni is not None and hasattr(ctx, "sni_callback"):
        def _sni_callback(sock, server_name, sctx):
            sni.append(server_name)

        try:
            ctx.sni_callback = _sni_callback
        except Exception:
            pass
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _generate_cert(cert_dir, hostname):
    cert = cert_dir / "cert.pem"
    key = cert_dir / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", f"/CN={hostname}",
            "-addext", f"subjectAltName=DNS:{hostname}",
        ],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return cert, key


class PushTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
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
        # 기본값은 google-prod 이다.
        self.assertEqual(push.main([]), 0)
        (path, headers, body), = self.worker.puts
        self.assertEqual(path, "/api/ingest")
        self.assertEqual(headers["Authorization"], "Bearer " + self.key)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["User-Agent"], "task-dashboard-push/1.0")
        self.assertEqual(body, snapshot)
        first_hash = self.google_prod_state.read_text().strip()
        self.assertRegex(first_hash, r"^[0-9a-f]{64}$")

        snapshot["generated_at"] = "2026-11-18T12:00:15+09:00"
        snapshot["tasks"] = [{"events": [{"note": "확인", "stage": "점검"}], "task_id": "one"}]
        self.assertEqual(push.main([]), 0)
        self.assertEqual(len(self.worker.puts), 1)
        self.assertEqual(self.google_prod_state.read_text().strip(), first_hash)

        snapshot["tasks"][0]["events"][0]["note"] = "변경"
        self.assertEqual(push.main([]), 0)
        self.assertEqual(len(self.worker.puts), 2)
        self.assertEqual(self.worker.puts[-1][2], snapshot)
        self.assertNotEqual(self.google_prod_state.read_text().strip(), first_hash)
        self.assertEqual(push.main(["--force"]), 0)
        self.assertEqual(len(self.worker.puts), 3)
        self.assertEqual(self.worker.puts[-1][2], snapshot)

    def test_failed_update_preserves_previous_hash(self):
        self.assertEqual(push.main([]), 0)
        previous = self.google_prod_state.read_bytes()
        self.source.snapshot["tasks"] = []
        self.worker.status = 503
        self.assertEqual(push.main([]), 1)
        self.assertEqual(self.google_prod_state.read_bytes(), previous)
        self.assertEqual(len(self.stderr.getvalue().splitlines()), 1)
        self.assertIn("HTTP 503", self.stderr.getvalue())
        self.worker.status = 200
        self.assertEqual(push.main([]), 0)
        self.assertNotEqual(self.google_prod_state.read_bytes(), previous)
        self.assertEqual(self.worker.puts[-1][2]["tasks"], [])

    def test_google_dev_uses_own_default_url_key_hash_and_lock(self):
        config = self.google_dev_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        (config / "ingest-key-google-prod").write_text("google-prod-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_dev_state.parent), "CONNECT_IP": ""}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(
                    upload.full_url,
                    "https://task-dashboard-dev.velar-agent.duckdns.org/api/ingest",
                )
                self.assertEqual(upload.get_header("Authorization"), "Bearer google-fixture")
                self.assertEqual(upload.data, body)
                self.assertRegex(self.google_dev_state.read_text().strip(), r"^[0-9a-f]{64}$")
                self.assertTrue(self.google_dev_state.with_suffix(".lock").is_file())
                self.assertFalse(self.google_prod_state.exists())
                http.reset_mock()
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                self.assertEqual(http.call_count, 1)  # 소스 조회만 수행

    def test_google_dev_honors_url_and_key_overrides(self):
        self.assertEqual(push.main(["--target", "google-dev"]), 0)
        (_, headers, body), = self.worker.puts
        self.assertEqual(headers["Authorization"], "Bearer " + self.key)
        self.assertEqual(body, self.source.snapshot)
        self.assertTrue(self.google_dev_state.exists())
        self.assertFalse(self.google_prod_state.exists())

    def test_google_dev_connect_ip_rewrites_host(self):
        config = self.google_dev_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_dev_state.parent), "CONNECT_IP": "192.0.2.99"}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(upload.full_url, "https://task-dashboard-dev.velar-agent.duckdns.org/api/ingest")
                self.assertEqual(getattr(upload, "_connect_ip", None), "192.0.2.99")
                self.assertEqual(getattr(upload, "_connect_port", None), 443)
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
        config = self.google_prod_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-prod").write_text("google-prod-fixture", encoding="ascii")
        (config / "ingest-key-google-dev").write_text("google-dev-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_prod_state.parent), "CONNECT_IP": ""}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-prod"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(
                    upload.full_url,
                    "https://task-dashboard.velar-agent.duckdns.org/api/ingest",
                )
                self.assertEqual(upload.get_header("Authorization"), "Bearer " + "google-prod-fixture")
                self.assertEqual(upload.data, body)
                self.assertRegex(self.google_prod_state.read_text().strip(), r"^[0-9a-f]{64}$")
                self.assertTrue(self.google_prod_state.with_suffix(".lock").is_file())
                self.assertFalse(self.google_dev_state.exists())

    def test_google_prod_honors_url_and_key_overrides(self):
        self.assertEqual(push.main(["--target", "google-prod"]), 0)
        (_, headers, body), = self.worker.puts
        self.assertEqual(headers["Authorization"], "Bearer " + self.key)
        self.assertEqual(body, self.source.snapshot)
        self.assertTrue(self.google_prod_state.exists())
        self.assertFalse(self.google_dev_state.exists())

    def test_google_prod_connect_ip_rewrites_host(self):
        config = self.google_prod_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-prod").write_text("google-prod-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_prod_state.parent), "CONNECT_IP": "192.0.2.99"}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-prod"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(upload.full_url, "https://task-dashboard.velar-agent.duckdns.org/api/ingest")
                self.assertEqual(getattr(upload, "_connect_ip", None), "192.0.2.99")
                self.assertEqual(getattr(upload, "_connect_port", None), 443)
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
        config = self.google_dev_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_dev_state.parent), "CONNECT_IP": "203.0.113.10"}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(upload.full_url, "https://task-dashboard-dev.velar-agent.duckdns.org/api/ingest")
                self.assertEqual(getattr(upload, "_connect_ip", None), "203.0.113.10")
                self.assertEqual(upload.get_header("Authorization"), "Bearer " + "google-fixture")

    def test_google_dev_connect_ip_ipv6_rewrites_host(self):
        config = self.google_dev_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_dev_state.parent), "CONNECT_IP": "2001:db8::1"}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(upload.full_url, "https://task-dashboard-dev.velar-agent.duckdns.org/api/ingest")
                self.assertEqual(getattr(upload, "_connect_ip", None), "2001:db8::1")
                self.assertEqual(upload.get_header("Authorization"), "Bearer " + "google-fixture")

    def test_google_dev_connect_ip_with_port_is_rejected(self):
        invalid = "192.0.2.99:8080"
        config = self.google_dev_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_dev_state.parent), "CONNECT_IP": invalid}):
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
        config = self.google_dev_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_dev_state.parent), "CONNECT_IP": invalid}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 1)
                self.assertEqual(self.stderr.getvalue().strip(), "스냅샷 푸시 실패 - CONNECT_IP 는 IP 주소여야 합니다")
                self.assertNotIn(invalid, self.stderr.getvalue())
                self.assertEqual(http.call_count, 1)
                self.assertFalse(self.google_dev_state.exists())

    def test_google_prod_state_is_separate_from_google_dev(self):
        config = self.google_prod_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-prod").write_text("google-prod-fixture", encoding="ascii")
        (config / "ingest-key-google-dev").write_text("google-dev-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_prod_state.parent), "CONNECT_IP": ""}):
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
                self.assertFalse(self.google_prod_state.exists())
                self.worker.status = 200
            else:
                raise KeyboardInterrupt

        with patch.object(push, "sleep", side_effect=next_tick):
            self.assertRaises(KeyboardInterrupt, push.main, ["--loop"])
        self.assertTrue(self.google_prod_state.exists())
        self.assertEqual(len(self.stderr.getvalue().splitlines()), 1)
        with patch.object(push, "sleep", side_effect=KeyboardInterrupt) as sleep:
            self.assertRaises(KeyboardInterrupt, push.main, ["--loop", "2"])
        sleep.assert_called_once_with(2)
        self.assertEqual(len(self.worker.puts), 2)


    def test_default_https_connect_ip_keeps_hostname_url_and_carries_pin(self):
        config = self.google_dev_state.parent / ".config" / "task-dashboard"
        config.mkdir(parents=True)
        (config / "ingest-key-google-dev").write_text("google-fixture", encoding="ascii")
        with patch.dict(os.environ, {"HOME": str(self.google_dev_state.parent), "CONNECT_IP": "192.0.2.99"}):
            del os.environ["KEY_FILE"], os.environ["WORKER_URL"]
            body = json.dumps(self.source.snapshot).encode()
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
                upload = http.call_args.args[0]
                self.assertEqual(upload.full_url, "https://task-dashboard-dev.velar-agent.duckdns.org/api/ingest")
                self.assertNotIn("192.0.2.99", upload.full_url)
                self.assertEqual(getattr(upload, "_connect_ip", None), "192.0.2.99")
                self.assertEqual(getattr(upload, "_connect_port", None), 443)

    def test_non_https_non_loopback_worker_url_is_rejected(self):
        body = json.dumps(self.source.snapshot).encode()
        with patch.dict(os.environ, {"WORKER_URL": "http://task-dashboard-dev.velar-agent.duckdns.org/", "CONNECT_IP": ""}):
            with patch.object(push, "request_body", return_value=body) as http:
                with self.assertRaises(push.PushError) as ctx:
                    push.push_once(True, "google-dev")
                message = str(ctx.exception)
                self.assertIn("https://", message)
                self.assertNotIn(self.key, message)
                self.assertEqual(http.call_count, 1)
                self.assertFalse(self.google_dev_state.exists())
        with patch.dict(os.environ, {"WORKER_URL": "http://task-dashboard.velar-agent.duckdns.org/", "CONNECT_IP": ""}):
            with patch.object(push, "request_body", return_value=body) as http:
                self.assertEqual(push.main(["--target", "google-prod"]), 1)
                self.assertIn("https://", self.stderr.getvalue())
                self.assertNotIn(self.key, self.stderr.getvalue())
                self.assertEqual(http.call_count, 1)
                self.assertFalse(self.google_prod_state.exists())

    def test_https_connect_ip_tls_pinned_success(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl 없음")
        hostname = "task-dashboard-dev.velar-agent.duckdns.org"
        cert_dir = self.google_dev_state.parent / "tls-good"
        cert_dir.mkdir()
        cert, key = _generate_cert(cert_dir, hostname)
        sni = []
        with serve_tls(FakeWorker, cert, key, sni=sni) as tls_worker:
            tls_worker.status = 200
            tls_worker.puts = []
            port = tls_worker.server_port
            worker_url = f"https://{hostname}:{port}/"
            with patch.dict(os.environ, {"WORKER_URL": worker_url, "CONNECT_IP": "127.0.0.1", "SSL_CERT_FILE": str(cert)}):
                self.assertEqual(push.main(["--target", "google-dev"]), 0)
            self.assertEqual(len(tls_worker.puts), 1)
            path, headers, seen_body = tls_worker.puts[0]
            self.assertEqual(path, "/api/ingest")
            self.assertEqual(headers["Authorization"], "Bearer " + self.key)
            self.assertEqual(headers["Host"], f"{hostname}:{port}")
            self.assertIn(hostname, sni)
            self.assertEqual(seen_body, self.source.snapshot)
            self.assertTrue(self.google_dev_state.exists())

    def test_https_connect_ip_tls_hostname_mismatch_fails(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl 없음")
        hostname = "task-dashboard-dev.velar-agent.duckdns.org"
        wrong = "wrong-host.invalid"
        cert_dir = self.google_dev_state.parent / "tls-wrong"
        cert_dir.mkdir()
        cert, key = _generate_cert(cert_dir, wrong)
        with serve_tls(FakeWorker, cert, key) as tls_worker:
            tls_worker.status = 200
            tls_worker.puts = []
            port = tls_worker.server_port
            worker_url = f"https://{hostname}:{port}/"
            with patch.dict(os.environ, {"WORKER_URL": worker_url, "CONNECT_IP": "127.0.0.1", "SSL_CERT_FILE": str(cert)}):
                self.assertEqual(push.main(["--target", "google-dev"]), 1)
            self.assertIn("스냅샷 푸시 실패", self.stderr.getvalue())
            self.assertNotIn(self.key, self.stderr.getvalue())
            self.assertEqual(tls_worker.puts, [])
            self.assertFalse(self.google_dev_state.exists())
