#!/usr/bin/env python3
"""구글 VM용 서버 (dev/prod 공통, 표준 라이브러리만 사용).

worker/src/index.js 와 동일한 API 를 제공하며, HTML 화면은 같은
저장소의 server.py 의 PAGE 를 재사용한다.
"""

import hmac
import json
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit

# 상위 디렉터리의 server.py 에서 PAGE 를 가져온다. server.py 는
# __main__ 가드가 있어 import 시 서버를 띄우지 않는다.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import server as _server_module  # noqa: E402

PAGE = _server_module.PAGE

KST = timezone(timedelta(hours=9), "KST")
MAX_BODY_BYTES = 1024 * 1024
TASK_PREFIX = "/api/tasks/"


def _empty_snapshot():
    return {"tasks": [], "generated_at": None, "pushed_at": None}

_SAVE_LOCK = threading.Lock()
_AUTH_RE = re.compile(r"^Bearer ([^\s]+)$", re.IGNORECASE)
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _authorized(authorization, secret):
    if not secret:
        return False
    match = _AUTH_RE.match(authorization or "")
    if not match:
        return False
    token = match.group(1)
    try:
        return hmac.compare_digest(
            token.encode("utf-8", "surrogatepass"),
            secret.encode("utf-8", "surrogatepass"),
        )
    except Exception:
        return False


def load_snapshot(data_file):
    try:
        raw = Path(data_file).read_bytes()
    except (OSError, ValueError):
        return _empty_snapshot()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _empty_snapshot()
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return _empty_snapshot()
    snapshot = dict(data)
    snapshot.setdefault("tasks", [])
    snapshot.setdefault("generated_at", None)
    snapshot.setdefault("pushed_at", None)
    # 검증 실패한 항목을 그대로 노출하지 않기 위해 tasks 가 리스트임을 보장한다.
    if not isinstance(snapshot.get("tasks"), list):
        return _empty_snapshot()
    return snapshot


def save_snapshot(data_file, snapshot):
    path = Path(data_file)
    parent = path.parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    with _SAVE_LOCK:
        tmp_name = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(parent) if str(parent) else ".",
                prefix=path.name + ".",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_name, path)
                tmp_name = None
            finally:
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        except Exception:
            raise


def _valid_task(task):
    return (
        isinstance(task, dict)
        and not isinstance(task, list)
        and isinstance(task.get("task_id"), str)
    )


class DevHandler(BaseHTTPRequestHandler):
    # 기본 로그는 한 줄로만 남기며 Authorization/키는 절대 기록하지 않는다.
    def log_message(self, format, *args):
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.client_address[0], self.log_date_time_string(), format % args)
        )

    def _send(self, status, content_type, body, extra_headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status, value, extra_headers=None):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body, extra_headers)

    def _method_not_allowed(self, allow):
        self._send_json(
            405,
            {"error": "허용되지 않은 메서드입니다."},
            {"Allow": allow},
        )

    def _path(self):
        try:
            return urlsplit(self.path).path
        except Exception:
            return self.path.partition("?")[0]

    def _read_exact(self, count):
        # count 바이트를 읽는다. 소켓이 일찍 닫히면 짧게 읽힐 수 있다.
        remaining = count
        chunks = []
        while remaining > 0:
            try:
                chunk = self.rfile.read(remaining)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_body(self):
        """(body_bytes, error) 를 반환한다. error 는 (status, payload, headers) 이다."""
        content_length = self.headers.get("Content-Length")
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if transfer_encoding == "chunked":
            # 청크 인코딩 본문: 실제 바이트 수로 제한을 검사한다.
            size = 0
            chunks = []
            while True:
                try:
                    line = self.rfile.readline(65536)
                except (OSError, ValueError):
                    return None, (400, {"error": "올바른 UTF-8 JSON 본문이 필요합니다."}, None)
                if not line:
                    return None, (400, {"error": "올바른 UTF-8 JSON 본문이 필요합니다."}, None)
                # "0xd"·"+1"·"1_0" 같은 관대한 표기를 막기 위해 16진 숫자만 허용한다.
                size_field = line.strip().split(b";", 1)[0].strip()
                if not re.fullmatch(rb"[0-9A-Fa-f]+", size_field):
                    return None, (400, {"error": "올바른 UTF-8 JSON 본문이 필요합니다."}, None)
                try:
                    chunk_size = int(size_field, 16)
                except ValueError:
                    return None, (400, {"error": "올바른 UTF-8 JSON 본문이 필요합니다."}, None)
                # 음수(-1)나 상한 초과 선언은 읽기를 시작하기 전에 400 으로 거부한다.
                if chunk_size < 0 or chunk_size > MAX_BODY_BYTES:
                    return None, (400, {"error": "올바른 UTF-8 JSON 본문이 필요합니다."}, None)
                if chunk_size == 0:
                    # 트레일러 줄을 건너뛰고 마지막 CRLF 를 읽는다.
                    try:
                        while True:
                            trailer = self.rfile.readline(65536)
                            if trailer in (b"\r\n", b"\n", b""):
                                break
                    except (OSError, ValueError):
                        pass
                    break
                remaining = chunk_size
                chunk_parts = []
                while remaining > 0:
                    try:
                        piece = self.rfile.read(min(remaining, 65536))
                    except (OSError, ValueError):
                        piece = b""
                    if not piece:
                        break
                    chunk_parts.append(piece)
                    size += len(piece)
                    if size > MAX_BODY_BYTES:
                        return None, (413, {"error": "본문이 1 MiB를 초과합니다."}, None)
                    remaining -= len(piece)
                chunks.append(b"".join(chunk_parts))
                try:
                    self.rfile.readline(65536)  # 청크 뒤 CRLF
                except (OSError, ValueError):
                    pass
            body = b"".join(chunks)
            if len(body) > MAX_BODY_BYTES:
                return None, (413, {"error": "본문이 1 MiB를 초과합니다."}, None)
            return body, None

        if content_length is None:
            return b"", None
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            declared = 0
        if declared < 0:
            declared = 0
        if declared > MAX_BODY_BYTES:
            return None, (413, {"error": "본문이 1 MiB를 초과합니다."}, None)
        if declared == 0:
            return b"", None
        body = self._read_exact(declared)
        if len(body) > MAX_BODY_BYTES:
            return None, (413, {"error": "본문이 1 MiB를 초과합니다."}, None)
        return body, None

    def _do_ingest(self):
        secret = getattr(self.server, "ingest_key", None)
        if not secret:
            self._send_json(503, {"error": "수집 키가 설정되지 않았습니다."})
            return
        if not _authorized(self.headers.get("Authorization"), secret):
            self._send_json(
                401,
                {"error": "인증에 실패했습니다."},
                {"WWW-Authenticate": "Bearer"},
            )
            return
        # Content-Length 헤더로 먼저 제한을 검사한다.
        content_length = self.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except (TypeError, ValueError):
                declared = 0
            if declared > MAX_BODY_BYTES:
                self._send_json(413, {"error": "본문이 1 MiB를 초과합니다."})
                return
        body, error = self._read_body()
        if error is not None:
            status, payload, headers = error
            self._send_json(status, payload, headers)
            return
        if not body:
            self._send_json(400, {"error": "JSON 본문이 필요합니다."})
            return
        try:
            text = body.decode("utf-8")
            snapshot = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": "올바른 UTF-8 JSON 본문이 필요합니다."})
            return
        tasks = snapshot.get("tasks") if isinstance(snapshot, dict) else None
        if not isinstance(tasks, list) or not all(_valid_task(t) for t in tasks):
            self._send_json(
                400,
                {"error": "tasks 배열의 각 작업에는 문자열 task_id가 필요합니다."},
            )
            return
        pushed_at = datetime.now(KST).isoformat(timespec="milliseconds")
        snapshot["pushed_at"] = pushed_at
        if snapshot.get("generated_at") is None:
            snapshot["generated_at"] = None
        data_file = getattr(self.server, "data_file", "")
        try:
            save_snapshot(data_file, snapshot)
        except (OSError, ValueError):
            self._send_json(500, {"error": "저장에 실패했습니다."})
            return
        self._send_json(200, {"ok": True, "count": len(tasks)})

    def _handle(self):
        path = self._path()
        method = self.command
        # worker/src/index.js 와 동일한 순서: ingest 경로 분리, 그 다음
        # 경로 존재 검사, 그 다음 메서드 검사.
        if path == "/api/ingest":
            if method != "PUT":
                self._method_not_allowed("PUT")
                return
            self._do_ingest()
            return
        if (
            path != "/"
            and path != "/healthz"
            and path != "/api/tasks"
            and not path.startswith(TASK_PREFIX)
        ):
            self._send_json(404, {"error": "경로를 찾을 수 없습니다."})
            return
        if method != "GET":
            self._method_not_allowed("GET")
            return
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            return
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        data_file = getattr(self.server, "data_file", "")
        snapshot = load_snapshot(data_file)
        if path == "/api/tasks":
            self._send_json(200, snapshot)
            return
        raw_id = path[len(TASK_PREFIX):]
        if _BAD_PERCENT_RE.search(raw_id):
            self._send_json(404, {"error": "작업을 찾을 수 없습니다."})
            return
        try:
            task_id = unquote_to_bytes(raw_id).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            self._send_json(404, {"error": "작업을 찾을 수 없습니다."})
            return
        for entry in snapshot.get("tasks", []):
            if isinstance(entry, dict) and entry.get("task_id") == task_id:
                self._send_json(200, entry)
                return
        self._send_json(404, {"error": "작업을 찾을 수 없습니다."})

    def do_GET(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()

    def do_TRACE(self):
        self._handle()

    def do_CONNECT(self):
        self._handle()


def make_server(host, port, data_file, ingest_key):
    class _Server(ThreadingHTTPServer):
        daemon_threads = True

    server = _Server((host, port), DevHandler)
    server.data_file = str(data_file)
    server.ingest_key = ingest_key
    return server


def main():
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8790"))
    data_file = os.environ.get("DATA_FILE", "./data/snapshot.json")
    ingest_key = os.environ.get("INGEST_KEY", "")
    # 시작 시 데이터 디렉터리를 만든다. 파일이 없어도 서버는 뜬다.
    try:
        parent = Path(data_file).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    with make_server(host, port, data_file, ingest_key) as httpd:
        print(f"task-dashboard가 {httpd.server_address[1]} 포트에서 실행 중입니다.", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
