import http.client
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import server

ROOT = Path(__file__).resolve().parents[1]
VM_SERVER = ROOT / "vm" / "server.py"
VM_DEV_SERVICE = ROOT / "vm" / "task-dashboard-dev.service"
VM_PROD_SERVICE = ROOT / "vm" / "task-dashboard.service"
VM_NGINX = ROOT / "vm" / "nginx-task-dashboard.conf"
VM_INSTALL = ROOT / "vm" / "install.sh"
VM_PROMOTE = ROOT / "vm" / "promote.sh"
VM_DEPLOY = ROOT / "vm" / "deploy.sh"
VM_CRON = ROOT / "vm" / "cron" / "task-dashboard-nightly"

spec = importlib.util.spec_from_file_location("vm_server", VM_SERVER)
gdev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gdev)

MAX_BODY = 1024 * 1024
SECRET = "test-ingest-secret-123"


@contextmanager
def run_server(data_file, ingest_key):
    httpd = gdev.make_server("127.0.0.1", 0, str(data_file), ingest_key)
    port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def req(port, method, path, body=None, headers=None):
    if isinstance(body, str):
        body = body.encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    status = resp.status
    conn.close()
    return status, hdrs, data


def req_json(port, method, path, body=None, headers=None):
    status, hdrs, data = req(port, method, path, body=body, headers=headers)
    return status, hdrs, json.loads(data.decode("utf-8"))


def put_ok_body(tasks, **extra):
    payload = {"tasks": tasks}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def call_read_body(raw, headers=None):
    """DevHandler._read_body 를 가짜 rfile/헤더로 직접 호출한다."""
    handler = object.__new__(gdev.DevHandler)
    handler.headers = (
        headers if headers is not None else {"Transfer-Encoding": "chunked"}
    )
    handler.rfile = io.BytesIO(raw)
    return handler._read_body()


class VmApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data_file = self.root / "data" / "snapshot.json"

    def assert_common_headers(self, headers, content_type):
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertTrue(
            headers.get("content-type", "").startswith(content_type),
            f"content-type {headers.get('content-type')!r} != {content_type}",
        )

    def test_page_identical_to_server_page(self):
        self.assertEqual(gdev.PAGE, server.PAGE)

    def test_empty_snapshot_and_basic_routes(self):
        with run_server(self.data_file, SECRET) as port:
            status, headers, body = req(port, "GET", "/")
            self.assertEqual(status, 200)
            self.assert_common_headers(headers, "text/html")
            self.assertEqual(body.decode("utf-8"), server.PAGE)

            status, headers, body = req_json(port, "GET", "/healthz")
            self.assertEqual((status, body), (200, {"status": "ok"}))
            self.assert_common_headers(headers, "application/json")

            status, headers, body = req_json(port, "GET", "/api/tasks")
            self.assertEqual(status, 200)
            self.assertEqual(body, {"tasks": [], "generated_at": None, "pushed_at": None})
            self.assert_common_headers(headers, "application/json")

            status, headers, body = req_json(port, "GET", "/api/tasks/missing")
            self.assertEqual((status, body), (404, {"error": "작업을 찾을 수 없습니다."}))
            self.assert_common_headers(headers, "application/json")

            status, headers, body = req_json(port, "GET", "/missing")
            self.assertEqual((status, body), (404, {"error": "경로를 찾을 수 없습니다."}))
            self.assert_common_headers(headers, "application/json")

            # 잘못된 percent 인코딩도 작업 없음 404
            status, _, body = req_json(port, "GET", "/api/tasks/%C3")
            self.assertEqual((status, body), (404, {"error": "작업을 찾을 수 없습니다."}))

            # 쿼리 문자열 무시
            status, _, _ = req(port, "GET", "/?refresh=1")
            self.assertEqual(status, 200)
            status, _, body = req_json(port, "GET", "/api/tasks?refresh=1")
            self.assertEqual(status, 200)

    def test_method_not_allowed_with_allow(self):
        with run_server(self.data_file, SECRET) as port:
            cases = [
                ("/", "POST", "GET"),
                ("/", "PUT", "GET"),
                ("/", "DELETE", "GET"),
                ("/healthz", "DELETE", "GET"),
                ("/healthz", "PUT", "GET"),
                ("/api/tasks", "PUT", "GET"),
                ("/api/tasks", "POST", "GET"),
                ("/api/tasks/1", "POST", "GET"),
                ("/api/tasks/1", "DELETE", "GET"),
                ("/api/ingest", "GET", "PUT"),
                ("/api/ingest", "POST", "PUT"),
                ("/api/ingest", "DELETE", "PUT"),
            ]
            for path, method, allow in cases:
                with self.subTest(path=path, method=method):
                    status, headers, body = req_json(port, method, path)
                    self.assertEqual(status, 405)
                    self.assertEqual(headers.get("allow"), allow)
                    self.assertEqual(body, {"error": "허용되지 않은 메서드입니다."})
                    self.assert_common_headers(headers, "application/json")
            # 존재하지 않는 경로는 메서드와 무관하게 404
            status, _, body = req_json(port, "POST", "/missing")
            self.assertEqual((status, body), (404, {"error": "경로를 찾을 수 없습니다."}))

    def test_ingest_auth_503_and_401(self):
        with run_server(self.data_file, "") as port:
            status, headers, body = req_json(
                port,
                "PUT",
                "/api/ingest",
                body='{"tasks":[]}',
                headers={"Authorization": f"Bearer {SECRET}"},
            )
            self.assertEqual(status, 503)
            self.assertEqual(body, {"error": "수집 키가 설정되지 않았습니다."})
            self.assert_common_headers(headers, "application/json")
            # 키 없어도 GET 은 동작
            status, _, _ = req(port, "GET", "/healthz")
            self.assertEqual(status, 200)

        with run_server(self.data_file, SECRET) as port:
            bad_headers = [
                {},
                {"Authorization": f"Basic {SECRET}"},
                {"Authorization": "Bearer"},
                {"Authorization": "Bearer "},
                {"Authorization": "Bearer  " + SECRET},
                {"Authorization": "Bearer short"},
            ]
            for headers in bad_headers:
                with self.subTest(headers=headers):
                    status, hdrs, body = req_json(
                        port, "PUT", "/api/ingest", body="broken JSON", headers=headers
                    )
                    self.assertEqual(status, 401)
                    self.assertEqual(body, {"error": "인증에 실패했습니다."})
                    self.assertEqual(hdrs.get("www-authenticate"), "Bearer")
                    self.assert_common_headers(hdrs, "application/json")
            # 소문자 bearer 는 허용
            status, _, body = req_json(
                port,
                "PUT",
                "/api/ingest",
                body='{"tasks":[]}',
                headers={"Authorization": f"bearer {SECRET}"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, {"ok": True, "count": 0})

    def test_validation_errors_preserve_snapshot(self):
        with run_server(self.data_file, SECRET) as port:
            auth = {"Authorization": f"Bearer {SECRET}"}
            status, _, body = req_json(
                port, "PUT", "/api/ingest", body='{"tasks":[{"task_id":"keep"}]}', headers=auth
            )
            self.assertEqual(status, 200)
            original_status, _, original = req_json(port, "GET", "/api/tasks")
            self.assertEqual(original_status, 200)

            bad_bodies = [
                "",
                "{",
                "null",
                "[]",
                "42",
                "{}",
                '{"tasks":{}}',
                '{"tasks":[null]}',
                '{"tasks":[42]}',
                '{"tasks":[[]]}',
                '{"tasks":[{}]}',
                '{"tasks":[{"task_id":1}]}',
                '{"tasks":[{"task_id":null}]}',
            ]
            for bad in bad_bodies:
                with self.subTest(body=bad):
                    status, _, body = req_json(port, "PUT", "/api/ingest", body=bad, headers=auth)
                    self.assertEqual(status, 400)
                    self.assertIn("error", body)
            # 잘못된 UTF-8
            status, _, body = req_json(port, "PUT", "/api/ingest", body=b"\xff\xfe{}", headers=auth)
            self.assertEqual(status, 400)

            status, _, current = req_json(port, "GET", "/api/tasks")
            self.assertEqual(current, original)

            # tasks 메시지 동일성
            status, _, body = req_json(
                port, "PUT", "/api/ingest", body='{"tasks":[{}]}', headers=auth
            )
            self.assertEqual(
                body,
                {"error": "tasks 배열의 각 작업에는 문자열 task_id가 필요합니다."},
            )

    def test_size_limits(self):
        with run_server(self.data_file, SECRET) as port:
            auth = {"Authorization": f"Bearer {SECRET}"}
            # 선행 저장으로 상태 고정
            status, _, _ = req_json(
                port, "PUT", "/api/ingest", body='{"tasks":[{"task_id":"keep"}]}', headers=auth
            )
            self.assertEqual(status, 200)
            _, _, original = req_json(port, "GET", "/api/tasks")

            def assert_oversize_rejected(declared_length, payload):
                # 서버는 Content-Length를 보고 즉시 413을 보내고 연결을 닫을 수 있다.
                # 클라이언트는 그 사이 큰 본문을 아직 보내고 있을 수 있어 전송 중
                # BrokenPipeError/ConnectionResetError가 나는 레이스가 있다.
                # 이때는 응답을 읽어 413을 확인하거나, 응답 자체를 못 읽으면
                # 거부됨으로 간주하고 스냅샷 무변경을 검증한다.
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    try:
                        conn.putrequest("PUT", "/api/ingest")
                        conn.putheader("Authorization", f"Bearer {SECRET}")
                        conn.putheader("Content-Length", str(declared_length))
                        conn.endheaders()
                        if payload:
                            conn.send(payload)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    try:
                        resp = conn.getresponse()
                        self.assertEqual(resp.status, 413)
                        data = resp.read()
                        self.assertEqual(
                            json.loads(data.decode("utf-8")),
                            {"error": "본문이 1 MiB를 초과합니다."},
                        )
                    except (BrokenPipeError, ConnectionResetError, http.client.HTTPException):
                        # RST/조기 종료로 413 응답을 못 읽으면 거부됨으로 간주하고
                        # 아래 스냅샷 무변경 검증으로 확인한다.
                        pass
                finally:
                    conn.close()
                _, _, current = req_json(port, "GET", "/api/tasks")
                self.assertEqual(current, original)

            # Content-Length 로 초과 (헤더검사: 본문은 적게 보내도 413이어야 한다)
            assert_oversize_rejected(MAX_BODY + 1, b"{}")

            # 실제 바이트로 초과
            big = ('{"tasks":[{"task_id":"x"}]}' + " " * MAX_BODY).encode("utf-8")
            big = big[: MAX_BODY + 1]
            self.assertGreater(len(big), MAX_BODY)
            assert_oversize_rejected(len(big), big)

            # 정확히 1MiB 는 통과 (JSON 뒤 공백 패딩)
            source = '{"tasks":[{"task_id":"한"}]}'
            exact = source + " " * (MAX_BODY - len(source.encode("utf-8")))
            self.assertEqual(len(exact.encode("utf-8")), MAX_BODY)
            status, _, body = req_json(port, "PUT", "/api/ingest", body=exact, headers=auth)
            self.assertEqual(status, 200, body)
            self.assertEqual(body, {"ok": True, "count": 1})

            # 413 후에도 이전 스냅샷 유지 확인 (exact 저장 후와 비교는 패딩 차이 무시하고 task 만 본다)
            _, _, current = req_json(port, "GET", "/api/tasks")
            self.assertEqual(current["tasks"][0]["task_id"], "한")

    def test_save_then_query_and_timestamp_format(self):
        with run_server(self.data_file, SECRET) as port:
            auth = {"Authorization": f"Bearer {SECRET}"}
            task = {
                "task_id": "작업/1 ?+#",
                "bot": "운영",
                "title": "<em>점검</em>",
                "stage": "완료",
                "events": [{"stage": "계획"}, {"stage": "완료", "note": "확인"}],
            }
            snapshot = {"tasks": [task, {"task_id": "second"}], "generated_at": "2026-11-18T12:00:00+09:00"}
            status, _, body = req_json(
                port,
                "PUT",
                "/api/ingest",
                body=json.dumps({**snapshot, "pushed_at": "client time"}, ensure_ascii=False),
                headers=auth,
            )
            self.assertEqual((status, body), (200, {"ok": True, "count": 2}))

            status, headers, listed = req_json(port, "GET", "/api/tasks?refresh=1")
            self.assertEqual(status, 200)
            self.assert_common_headers(headers, "application/json")
            self.assertEqual(listed["tasks"], snapshot["tasks"])
            self.assertEqual(listed["generated_at"], snapshot["generated_at"])
            pushed = listed["pushed_at"]
            self.assertRegex(pushed, r"^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+09:00$")
            # 밀리초 포함 ISO
            from datetime import datetime

            datetime.fromisoformat(pushed)

            status, _, detail = req_json(
                port,
                "GET",
                "/api/tasks/" + quote(task["task_id"], safe="") + "?v=1",
            )
            self.assertEqual((status, detail), (200, task))

            # generated_at 없으면 null
            status, _, body = req_json(
                port, "PUT", "/api/ingest", body='{"tasks":[]}', headers=auth
            )
            self.assertEqual((status, body), (200, {"ok": True, "count": 0}))
            _, _, replaced = req_json(port, "GET", "/api/tasks")
            self.assertEqual(replaced["tasks"], [])
            self.assertIsNone(replaced["generated_at"])
            status, _, body = req_json(port, "GET", "/api/tasks/second")
            self.assertEqual((status, body), (404, {"error": "작업을 찾을 수 없습니다."}))

    def test_restart_keeps_snapshot(self):
        with run_server(self.data_file, SECRET) as port:
            auth = {"Authorization": f"Bearer {SECRET}"}
            status, _, _ = req_json(
                port,
                "PUT",
                "/api/ingest",
                body=put_ok_body([{"task_id": "persist"}], generated_at="2026-11-18T12:00:00+09:00"),
                headers=auth,
            )
            self.assertEqual(status, 200)
        # 파일이 존재하고 JSON 으로 읽힌다
        raw = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertEqual(raw["tasks"], [{"task_id": "persist"}])
        with run_server(self.data_file, SECRET) as port:
            status, _, body = req_json(port, "GET", "/api/tasks")
            self.assertEqual(status, 200)
            self.assertEqual(body["tasks"], [{"task_id": "persist"}])
            status, _, detail = req_json(port, "GET", "/api/tasks/persist")
            self.assertEqual(detail, {"task_id": "persist"})

    def test_ingest_preserves_bot_identity_fields(self):
        # vm/server.py는 접힌 JSON을 그대로 저장하므로 bot_id/bot_name/
        # recorded_bot_name이 목록과 상세 조회에서 그대로 보여야 한다.
        task = {
            "task_id": "renamed",
            "bot_id": "bot-1",
            "bot_name": "NewBot",
            "bot": "NewBot",
            "title": "이름 변경",
            "stage": "코딩",
            "status": "진행 중",
            "started": "2026-11-18T09:00:00+09:00",
            "updated": "2026-11-18T10:00:00+09:00",
            "note": "",
            "next": "",
            "events": [
                {
                    "task_id": "renamed",
                    "bot_id": "bot-1",
                    "bot_name": "NewBot",
                    "bot": "NewBot",
                    "recorded_bot_name": "OldBot",
                    "title": "이름 변경",
                    "stage": "계획",
                    "note": "",
                    "next": "",
                    "ts": "2026-11-18T09:00:00+09:00",
                },
                {
                    "task_id": "renamed",
                    "bot_id": "bot-1",
                    "bot_name": "NewBot",
                    "bot": "NewBot",
                    "recorded_bot_name": "NewBot",
                    "title": "",
                    "stage": "코딩",
                    "note": "확인",
                    "next": "",
                    "ts": "2026-11-18T10:00:00+09:00",
                },
            ],
        }
        snapshot = {"tasks": [task], "generated_at": "2026-11-18T12:00:00+09:00"}
        with run_server(self.data_file, SECRET) as port:
            auth = {"Authorization": f"Bearer {SECRET}"}
            status, _, body = req_json(
                port,
                "PUT",
                "/api/ingest",
                body=json.dumps(snapshot, ensure_ascii=False),
                headers=auth,
            )
            self.assertEqual((status, body), (200, {"ok": True, "count": 1}))
            status, _, listed = req_json(port, "GET", "/api/tasks")
            self.assertEqual(status, 200)
            self.assertEqual(listed["tasks"], [task])
            status, _, detail = req_json(port, "GET", "/api/tasks/" + quote(task["task_id"], safe=""))
            self.assertEqual((status, detail), (200, task))
        raw = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertEqual(raw["tasks"], [task])

    def test_corrupt_file_is_safe(self):
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_file.write_bytes(b"\xff\xfe not json {")
        with run_server(self.data_file, SECRET) as port:
            status, _, body = req_json(port, "GET", "/api/tasks")
            self.assertEqual((status, body), (200, {"tasks": [], "generated_at": None, "pushed_at": None}))
            status, _, _ = req(port, "GET", "/healthz")
            self.assertEqual(status, 200)
            auth = {"Authorization": f"Bearer {SECRET}"}
            status, _, body = req_json(
                port, "PUT", "/api/ingest", body='{"tasks":[{"task_id":"after"}]}', headers=auth
            )
            self.assertEqual((status, body), (200, {"ok": True, "count": 1}))
            _, _, listed = req_json(port, "GET", "/api/tasks")
            self.assertEqual(listed["tasks"], [{"task_id": "after"}])

        # 모양만 맞는 손상도 빈 스냅샷
        self.data_file.write_text('{"tasks":{}}', encoding="utf-8")
        with run_server(self.data_file, SECRET) as port:
            _, _, body = req_json(port, "GET", "/api/tasks")
            self.assertEqual(body, {"tasks": [], "generated_at": None, "pushed_at": None})

    def test_atomic_write_no_temp_left(self):
        with run_server(self.data_file, SECRET) as port:
            auth = {"Authorization": f"Bearer {SECRET}"}
            status, _, _ = req_json(
                port,
                "PUT",
                "/api/ingest",
                body=put_ok_body([{"task_id": "a"}]),
                headers=auth,
            )
            self.assertEqual(status, 200)
            entries = list(self.data_file.parent.iterdir())
            self.assertFalse(any(p.suffix == ".tmp" for p in entries), entries)
            self.assertFalse(any(".tmp" in p.name for p in entries), entries)
            # 파일 전체가 한 JSON 이다
            json.loads(self.data_file.read_text(encoding="utf-8"))
        # 모듈의 save_snapshot 도 원자 교체한다
        gdev.save_snapshot(str(self.data_file), {"tasks": [], "generated_at": None, "pushed_at": None})
        self.assertFalse(any(".tmp" in p.name for p in self.data_file.parent.iterdir()))

    def test_log_does_not_contain_secret(self):
        import sys

        buffer = io.StringIO()
        with run_server(self.data_file, SECRET) as port:
            with patch.object(sys, "stderr", buffer):
                auth = {"Authorization": f"Bearer {SECRET}"}
                req_json(port, "PUT", "/api/ingest", body='{"tasks":[]}', headers=auth)
                req(port, "GET", "/api/tasks")
        self.assertNotIn(SECRET, buffer.getvalue())

    def test_empty_snapshot_independent_per_call(self):
        # 전역 상수를 팩토리로: 호출마다 tasks 리스트를 새로 만든다.
        self.assertFalse(hasattr(gdev, "EMPTY_SNAPSHOT"))
        first = gdev._empty_snapshot()
        second = gdev._empty_snapshot()
        self.assertEqual(first, {"tasks": [], "generated_at": None, "pushed_at": None})
        self.assertIsNot(first["tasks"], second["tasks"])
        first["tasks"].append({"task_id": "x"})
        self.assertEqual(second["tasks"], [])

        def check_independent(label, data_file):
            a = gdev.load_snapshot(str(data_file))
            b = gdev.load_snapshot(str(data_file))
            self.assertEqual(
                a, {"tasks": [], "generated_at": None, "pushed_at": None}, label
            )
            self.assertIsNot(a["tasks"], b["tasks"], label)
            a["tasks"].append({"task_id": "mut"})
            self.assertEqual(b["tasks"], [], label)
            # 이전 호출의 변경이 전역 상태로 남아 다음 반환에도 번지면 안 된다.
            c = gdev.load_snapshot(str(data_file))
            self.assertEqual(c["tasks"], [], label)

        # 파일 없음
        check_independent("missing", self.data_file)
        # 깨진 파일
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_file.write_bytes(b"\xff\xfe not json")
        check_independent("corrupt", self.data_file)
        # 모양만 맞는 손상
        self.data_file.write_text('{"tasks":{}}', encoding="utf-8")
        check_independent("wrong-shape", self.data_file)
        # 정상 파일도 호출마다 새 리스트: 한쪽 변경이 다른 쪽에 안 번진다.
        self.data_file.write_text(
            json.dumps(
                {
                    "tasks": [{"task_id": "a"}],
                    "generated_at": None,
                    "pushed_at": None,
                }
            ),
            encoding="utf-8",
        )
        a = gdev.load_snapshot(str(self.data_file))
        b = gdev.load_snapshot(str(self.data_file))
        self.assertIsNot(a["tasks"], b["tasks"])
        a["tasks"].append({"task_id": "mut"})
        self.assertEqual(b["tasks"], [{"task_id": "a"}])

    def test_chunked_bad_chunk_sizes_rejected(self):
        payload = b'{"tasks":[]}'  # 12바이트
        # 정상 청크(확장 포함)는 그대로 통과해야 한다.
        body, error = call_read_body(b"c\r\n" + payload + b"\r\n0\r\n\r\n")
        self.assertIsNone(error)
        self.assertEqual(body, payload)
        body, error = call_read_body(b"c;ext=1\r\n" + payload + b"\r\n0;done=1\r\n\r\n")
        self.assertIsNone(error)
        self.assertEqual(body, payload)

        bad_streams = {
            # 음수 + 다음 줄 밀킹: 구 코드는 data 를 읽지 않고 넘어가
            # 뒤의 z/d 를 소비해 body 를 그대로 통과시켰다(프레이밍 어긋남).
            "-1": b"-1\r\nz\r\nc\r\n" + payload + b"\r\n0\r\n\r\n",
            "-a": b"-a\r\nz\r\nc\r\n" + payload + b"\r\n0\r\n\r\n",
            # 숫자 접두사·부호·밑줄은 int(...,16) 이 관대하게 통과시킨 비정상 표기.
            "0xd": b"0xd\r\n" + payload + b"\r\n0\r\n\r\n",
            "+d": b"+d\r\n" + payload + b"\r\n0\r\n\r\n",
            "1_0": b"1_0\r\n" + payload + b"   \r\n0\r\n\r\n",
            # 파싱 불가·빈 줄·선언 크기 초과.
            "d d": b"d d\r\n" + payload + b"\r\n0\r\n\r\n",
            "zz": b"zz\r\n" + payload + b"\r\n0\r\n\r\n",
            "empty": b"\r\n" + payload + b"\r\n0\r\n\r\n",
            "huge": b"ffffffffffffffff\r\n",
            "over-limit": b"100001\r\n",  # 0x100001 = 1MiB + 1
        }
        for label, stream in bad_streams.items():
            with self.subTest(size=label):
                body, error = call_read_body(stream)
                self.assertIsNone(body, error)
                self.assertIsNotNone(error)
                status, payload_body, _ = error
                self.assertEqual(status, 400)
                self.assertEqual(
                    payload_body, {"error": "올바른 UTF-8 JSON 본문이 필요합니다."}
                )

    def test_chunked_negative_size_over_http_rejected(self):
        with run_server(self.data_file, SECRET) as port:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.putrequest("PUT", "/api/ingest")
            conn.putheader("Authorization", f"Bearer {SECRET}")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.putheader("Connection", "close")
            conn.endheaders()
            try:
                conn.send(b"-1\r\nz\r\nc\r\n" + b'{"tasks":[]}' + b"\r\n0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            resp = conn.getresponse()
            data = resp.read()
            self.assertEqual(resp.status, 400)
            self.assertEqual(
                json.loads(data.decode("utf-8")),
                {"error": "올바른 UTF-8 JSON 본문이 필요합니다."},
            )
            conn.close()
            # 거부된 요청은 스냅샷을 건드리지 않았다.
            status, _, body = req_json(port, "GET", "/api/tasks")
            self.assertEqual(
                (status, body),
                (200, {"tasks": [], "generated_at": None, "pushed_at": None}),
            )


class VmConfigTests(unittest.TestCase):
    def test_install_script_syntax(self):
        for script in (VM_INSTALL, VM_PROMOTE, VM_DEPLOY):
            result = subprocess.run(
                ["bash", "-n", str(script)], capture_output=True, text=True, timeout=10
            )
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")

    def test_service_files(self):
        dev = VM_DEV_SERVICE.read_text(encoding="utf-8")
        prod = VM_PROD_SERVICE.read_text(encoding="utf-8")
        hardening = [
            "User=collector",
            "Environment=HOST=127.0.0.1",
            "Environment=PYTHONDONTWRITEBYTECODE=1",
            "Restart=always",
            "RestartSec=3",
            "WantedBy=multi-user.target",
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "ProtectKernelTunables=yes",
            "ProtectKernelModules=yes",
            "ProtectControlGroups=yes",
            "RestrictSUIDSGID=yes",
            "CapabilityBoundingSet=",
            "MemoryMax=128M",
        ]
        for needle in hardening:
            self.assertIn(needle, dev)
            self.assertIn(needle, prod)
        for needle in [
            "WorkingDirectory=/workspace/task-dashboard-dev",
            "EnvironmentFile=/home/collector/.config/task-dashboard-dev/env",
            "Environment=PORT=8790",
            "Environment=DATA_FILE=/var/lib/task-dashboard-dev/snapshot.json",
            "StateDirectory=task-dashboard-dev",
            "ExecStart=/usr/bin/python3 vm/server.py",
        ]:
            self.assertIn(needle, dev)
        for needle in [
            "WorkingDirectory=/workspace/task-dashboard",
            "EnvironmentFile=/home/collector/.config/task-dashboard/env",
            "Environment=PORT=8791",
            "Environment=DATA_FILE=/var/lib/task-dashboard/snapshot.json",
            "StateDirectory=task-dashboard",
            "ExecStart=/usr/bin/python3 vm/server.py",
        ]:
            self.assertIn(needle, prod)
        # 분리: 각 환경의 전용 라인이 상대방에는 없어야 한다 (ExecStart는 공통).
        self.assertNotIn("WorkingDirectory=/workspace/task-dashboard-dev", prod)
        self.assertNotIn("EnvironmentFile=/home/collector/.config/task-dashboard-dev/env", prod)
        self.assertNotIn("Environment=PORT=8790", prod)
        self.assertNotIn("Environment=DATA_FILE=/var/lib/task-dashboard-dev/snapshot.json", prod)
        self.assertNotIn("StateDirectory=task-dashboard-dev", prod)
        self.assertNotIn("EnvironmentFile=/home/collector/.config/task-dashboard/env\n", dev)
        self.assertNotIn("Environment=PORT=8791", dev)
        # StateDirectory는 접두사 겹침 때문에 라인 단위로 확인한다.
        self.assertNotIn("StateDirectory=task-dashboard\n", dev)
        # 분리 확인: 포트·경로·env·StateDirectory가 서로 다름
        self.assertIn("Environment=PORT=8790", dev)
        self.assertIn("Environment=PORT=8791", prod)
        self.assertNotIn("Environment=PORT=8791", dev)
        self.assertNotIn("Environment=PORT=8790", prod)
        self.assertNotIn("task-dashboard-dev", prod)
        self.assertNotIn("/workspace/task-dashboard\n", dev)  # prod WorkingDirectory와 혼동 금지
        for text in (dev, prod):
            for forbidden in ["8787", "8788", "usage-dashboard", "cloudflare", "workers.dev", "cloudflared", "WRANGLER", "default_server"]:
                self.assertNotIn(forbidden, text.lower() if forbidden.islower() else text)
            self.assertNotIn("8787", text)
            self.assertNotIn("8788", text)

    def test_nginx_file(self):
        text = VM_NGINX.read_text(encoding="utf-8")
        # 두 server 블록, 둘 다 80->443 리다이렉트 전용
        self.assertEqual(text.count("server {"), 2)
        for needle in [
            "listen 80;",
            "listen [::]:80;",
            "server_name task-dashboard-dev.velar-agent.duckdns.org;",
            "server_name task-dashboard.velar-agent.duckdns.org;",
            "return 301 https://$host$request_uri;",
            "/etc/nginx/sites-available/task-dashboard-https",
        ]:
            self.assertIn(needle, text)
        # 두 블록 모두에 있어야 한다
        self.assertEqual(text.count("return 301 https://$host$request_uri;"), 2)
        # 리다이렉트 전용: 앱으로 가는 프록시가 없어야 한다
        for forbidden in ["proxy_pass", "8790", "8791", "X-Forwarded-Proto", "client_max_body_size"]:
            self.assertNotIn(forbidden, text)
        self.assertNotIn("default_server", text)
        for forbidden in ["8787", "8788", "usage-dashboard"]:
            self.assertNotIn(forbidden, text)
        self.assertNotIn("cloudflare", text.lower())
        self.assertNotIn("workers.dev", text.lower())

    def test_install_script_content(self):
        text = VM_INSTALL.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        for needle in [
            "/home/collector/.config/task-dashboard-dev/env",
            "/home/collector/.config/task-dashboard/env",
            "daemon-reload",
            "systemctl enable",
            "restart",
            "/etc/systemd/system/",
            "task-dashboard-dev.service",
            "/etc/nginx/sites-available/task-dashboard",
            "/etc/nginx/sites-enabled/task-dashboard",
            "nginx -t",
            "reload nginx",
            "/workspace/task-dashboard-dev",
            "/workspace/task-dashboard",
        ]:
            self.assertIn(needle, text)
        # enable --now 직후 restart 중복 금지: enable(옵션 없음) 다음 restart
        self.assertNotIn("enable --now", text)
        self.assertLess(text.index("systemctl enable"), text.index("systemctl restart"))
        # 인자 dev|prod 검사
        self.assertIn("dev|prod", text)
        # prod 유닛도 설치 경로에 포함
        self.assertIn("task-dashboard.service", text)
        # 실패 시 링크 제거
        self.assertRegex(text, r"rm\s+-f.*sites-enabled/task-dashboard")
        # 비밀 출력 금지: 스크립트 안에 실제 키 패턴이 없어야 한다
        self.assertNotIn("INGEST_KEY=", text.replace("INGEST_KEY=%s", ""))
        for forbidden in ["8787", "8788", "usage-dashboard"]:
            self.assertNotIn(forbidden, text)
        self.assertNotIn("cloudflare", text.lower())
        self.assertNotIn("workers.dev", text.lower())
        self.assertNotIn("default_server", text)

    def test_cron_file(self):
        text = VM_CRON.read_text(encoding="utf-8")
        self.assertIn("CRON_TZ=Asia/Seoul", text)
        self.assertIn("0 0 * * * collector /workspace/task-dashboard-dev/vm/promote.sh", text)
        for forbidden in ["8787", "8788", "usage-dashboard", "default_server"]:
            self.assertNotIn(forbidden, text)

    def test_promote_and_deploy_text(self):
        promote = VM_PROMOTE.read_text(encoding="utf-8")
        for needle in [
            "flock",
            "Asia/Seoul",
            ".local/state/task-dashboard-deploy",
            "8790",
            "8791",
            "merge --ff-only",
            "reset --hard",
        ]:
            self.assertIn(needle, promote)
        for forbidden in ["8787", "8788", "default_server", "usage-dashboard"]:
            self.assertNotIn(forbidden, promote)
        self.assertNotIn("cloudflare", promote.lower())
        deploy_text = VM_DEPLOY.read_text(encoding="utf-8")
        for needle in [
            "gce-collector",
            "/workspace/task-dashboard-dev",
            "/workspace/task-dashboard",
            "vm/install.sh",
            "git clone git@github.com:velar-agent/task-dashboard.git",
        ]:
            self.assertIn(needle, deploy_text)
        for forbidden in ["8787", "8788", "usage-dashboard", "default_server"]:
            self.assertNotIn(forbidden, deploy_text)

    def test_server_is_stdlib_only_and_no_cloudflare(self):
        text = VM_SERVER.read_text(encoding="utf-8")
        lowered = text.lower()
        for forbidden in ["cloudflare", "workers.dev", "cloudflared", "wrangler", "tunnel"]:
            self.assertNotIn(forbidden, lowered)
        # import 행이 표준 라이브러리만 사용
        std = sys.stdlib_module_names if hasattr(sys, "stdlib_module_names") else set()
        for match in re.finditer(r"^(?:import|from)\s+([a-zA-Z0-9_.]+)", text, re.M):
            top = match.group(1).split(".")[0]
            if top in {"server"}:
                continue
            if std:
                self.assertIn(top, std, f"non-stdlib import {top}")


class PromoteTests(unittest.TestCase):
    """vm/promote.sh를 임시 git 저장소 두 개로 검증한다."""

    def git(self, cwd, *args, check=True):
        result = subprocess.run(
            ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result

    def setup_repos(self, tmp):
        root = Path(tmp.name)
        remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        # bare 기본 브랜치를 main으로 고정
        subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        seed = root / "seed"
        seed.mkdir()
        self.git(seed, "init", "-b", "main")
        (seed / "file.txt").write_text("v1\n", encoding="utf-8")
        (seed / "test_dummy.py").write_text(
            "import unittest\nclass Dummy(unittest.TestCase):\n    def test_ok(self):\n        self.assertEqual(1, 1)\n",
            encoding="utf-8",
        )
        self.git(seed, "add", "file.txt", "test_dummy.py")
        self.git(seed, "commit", "-m", "v1")
        self.git(seed, "remote", "add", "origin", str(remote))
        self.git(seed, "push", "-u", "origin", "main")
        dev = root / "dev"
        prod = root / "prod"
        subprocess.run(["git", "clone", str(remote), str(dev)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(remote), str(prod)], check=True, capture_output=True)
        state = root / "state"
        state.mkdir()
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        self.root = root
        self.remote = remote
        self.dev = dev
        self.prod = prod
        self.state = state
        self.fake_bin = fake_bin
        self.systemctl_log = root / "systemctl.log"
        self.health_log = root / "health.log"
        # fake systemctl: 모든 호출 성공 + 기록
        fake_ctl = fake_bin / "systemctl"
        fake_ctl.write_text(
            "#!/usr/bin/env bash\n"
            'echo "systemctl $*" >> "' + str(self.systemctl_log) + '"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake_ctl.chmod(0o700)
        return root

    def make_fake_health_always_ok(self):
        fake = self.root / "fake-health-ok.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "port, repo = sys.argv[1], sys.argv[2]\n"
            "# dev와 prod 모두 성공\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        return f"python3 {fake}"

    def make_fake_health_prod_always_fail(self):
        fake = self.root / "fake-health-fail.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "port, repo = sys.argv[1], sys.argv[2]\n"
            "sys.exit(0 if port == '8790' else 1)\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        return f"python3 {fake}"

    def make_fake_health_fail_until_prev(self, fail_head_path):
        fake = self.root / "fake-health-fail-until.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, subprocess, pathlib\n"
            "port, repo = sys.argv[1], sys.argv[2]\n"
            "if port == '8790':\n"
            "    sys.exit(0)\n"
            f"fail = pathlib.Path({str(fail_head_path)!r}).read_text().strip()\n"
            "head = subprocess.check_output(['git','-C',repo,'rev-parse','--verify','HEAD'], text=True).strip()\n"
            "sys.exit(1 if head == fail else 0)\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        return f"python3 {fake}"

    def promote_env(self, health_cmd):
        env = dict(os.environ)
        env["DEV_DIR"] = str(self.dev)
        env["PROD_DIR"] = str(self.prod)
        env["PROMOTE_STATE"] = str(self.state)
        env["SYSTEMCTL"] = str(self.fake_bin / "systemctl")
        env["HEALTH_CMD"] = health_cmd
        env["HOME"] = str(self.root)
        env["PROMOTE_SLEEP"] = "0"
        return env

    def run_promote(self, health_cmd, *args):
        env = self.promote_env(health_cmd)
        result = subprocess.run(
            [str(VM_PROMOTE), *args],
            env=env,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result

    def commit_in(self, repo, message, content):
        (Path(repo) / "file.txt").write_text(content, encoding="utf-8")
        self.git(repo, "add", "file.txt")
        self.git(repo, "commit", "-m", message)

    def test_dev_not_main_exits_3(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        # dev를 feature 브랜치로 이동
        self.git(self.dev, "checkout", "-b", "feature")
        health = self.make_fake_health_always_ok()
        result = self.run_promote(health)
        self.assertEqual(result.returncode, 3, f"stdout={result.stdout} stderr={result.stderr}")

    def test_dev_dirty_exits_3(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        (self.dev / "file.txt").write_text("dirty\n", encoding="utf-8")
        health = self.make_fake_health_always_ok()
        result = self.run_promote(health)
        self.assertEqual(result.returncode, 3, f"stdout={result.stdout} stderr={result.stderr}")

    def test_same_head_skips_0(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        health = self.make_fake_health_always_ok()
        before = self.git(self.prod, "rev-parse", "--verify", "HEAD").stdout.strip()
        result = self.run_promote(health)
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout} stderr={result.stderr}")
        after = self.git(self.prod, "rev-parse", "--verify", "HEAD").stdout.strip()
        self.assertEqual(before, after)

    def test_fast_forward_success_0(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        self.commit_in(self.dev, "v2", "v2\n")
        self.git(self.dev, "push", "origin", "main")
        dev_head = self.git(self.dev, "rev-parse", "--verify", "HEAD").stdout.strip()
        health = self.make_fake_health_always_ok()
        result = self.run_promote(health)
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout} stderr={result.stderr}")
        prod_head = self.git(self.prod, "rev-parse", "--verify", "HEAD").stdout.strip()
        self.assertEqual(prod_head, dev_head)
        # 정상 경로에서는 systemctl restart가 1회 호출되어야 한다.
        if self.systemctl_log.exists():
            self.assertIn("restart task-dashboard", self.systemctl_log.read_text())

    def test_check_fail_rollback_5(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        self.commit_in(self.dev, "v2", "v2\n")
        self.git(self.dev, "push", "origin", "main")
        dev_head = self.git(self.dev, "rev-parse", "--verify", "HEAD").stdout.strip()
        prev = self.git(self.prod, "rev-parse", "--verify", "HEAD").stdout.strip()
        fail_head = self.root / "fail_head"
        fail_head.write_text(dev_head + "\n", encoding="utf-8")
        health = self.make_fake_health_fail_until_prev(fail_head)
        result = self.run_promote(health)
        self.assertEqual(result.returncode, 5, f"stdout={result.stdout} stderr={result.stderr}")
        after = self.git(self.prod, "rev-parse", "--verify", "HEAD").stdout.strip()
        self.assertEqual(after, prev)

    def test_rollback_also_fails_6(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        self.commit_in(self.dev, "v2", "v2\n")
        self.git(self.dev, "push", "origin", "main")
        prev = self.git(self.prod, "rev-parse", "--verify", "HEAD").stdout.strip()
        health = self.make_fake_health_prod_always_fail()
        result = self.run_promote(health)
        self.assertEqual(result.returncode, 6, f"stdout={result.stdout} stderr={result.stderr}")
        after = self.git(self.prod, "rev-parse", "--verify", "HEAD").stdout.strip()
        self.assertEqual(after, prev)

    def test_fast_forward_impossible_7(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        # prod에 로컬 divergent 커밋
        self.commit_in(self.prod, "prod-local", "prod-local\n")
        self.git(self.prod, "add", "file.txt")
        # dev도 독립 커밋 후 push
        self.commit_in(self.dev, "dev-2", "dev-2\n")
        self.git(self.dev, "push", "origin", "main")
        health = self.make_fake_health_always_ok()
        result = self.run_promote(health)
        self.assertEqual(result.returncode, 7, f"stdout={result.stdout} stderr={result.stderr}")

    def test_lock_duplicate_9(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        lock = self.state / "lock"
        lock_path = str(lock)
        # flock을 잡은 채로 promote 실행
        # python이 flock을 잡고 sleep 대신 곧바로 호출한다.
        holder = subprocess.Popen(["flock", "-n", lock_path, "sleep", "5"])
        try:
            # 잠시 기다려 락 획득 확인
            import time
            time.sleep(0.5)
            health = self.make_fake_health_always_ok()
            result = self.run_promote(health)
            self.assertEqual(result.returncode, 9, f"stdout={result.stdout} stderr={result.stderr}")
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()

    def test_dry_run_changes_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        self.commit_in(self.dev, "v2", "v2\n")
        self.git(self.dev, "push", "origin", "main")
        prev = self.git(self.prod, "rev-parse", "--verify", "HEAD").stdout.strip()
        health = self.make_fake_health_always_ok()
        result = self.run_promote(health, "--dry-run")
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout} stderr={result.stderr}")
        after = self.git(self.prod, "rev-parse", "--verify", "HEAD").stdout.strip()
        self.assertEqual(after, prev)
        # dry-run에서는 systemctl이 호출되지 않아야 한다.
        if self.systemctl_log.exists():
            self.assertNotIn("restart", self.systemctl_log.read_text())

    def test_usage_error_10(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.setup_repos(tmp)
        health = self.make_fake_health_always_ok()
        result = self.run_promote(health, "--bogus")
        self.assertEqual(result.returncode, 10, f"stdout={result.stdout} stderr={result.stderr}")


if __name__ == "__main__":
    unittest.main()
