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
