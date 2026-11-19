#!/usr/bin/env python3
"""box의 작업 스냅샷이 바뀌면 구글 VM dev/prod 에 https:// 로 전송한다.

google-dev/google-prod 대상은 HTTPS 전송을 하며 서버 인증서를 검증한다.
CONNECT_IP가 있으면 TCP 연결은 해당 IP로 열지만 SNI와 인증서 검증은 원래 호스트명으로 수행한다.
"""

import argparse
import errno
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import socket
import ssl
import sys
import tempfile
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener, urlopen


GOOGLE_DEV_STATE_FILE = Path(f"/tmp/task-push-google-dev-{os.getuid()}.sha")
GOOGLE_PROD_STATE_FILE = Path(f"/tmp/task-push-google-prod-{os.getuid()}.sha")


class PushError(Exception):
    """키나 응답 본문을 포함하지 않는 사용자용 오류."""


def _is_loopback_host(host):
    if not host:
        return False
    lowered = str(host).strip().lower()
    if lowered in ("localhost",):
        return True
    if lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


class _PinnedHTTPSConnection(HTTPSConnection):
    """TCP는 CONNECT_IP로 접속하지만 SNI/인증서 검증은 원래 호스트명으로 한다."""

    def __init__(self, host, *args, connect_ip=None, connect_port=None, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._connect_ip = connect_ip
        self._connect_port = connect_port

    def connect(self):
        target_port = self._connect_port if self._connect_port is not None else self.port
        sock = socket.create_connection((self._connect_ip, target_port), timeout=self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as error:
            if error.errno != errno.ENOPROTOOPT:
                raise
        server_hostname = self._tunnel_host if self._tunnel_host else self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, *args, connect_ip=None, connect_port=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_connect_ip = connect_ip
        self._pinned_connect_port = connect_port

    def https_open(self, req):
        return self.do_open(
            _PinnedHTTPSConnection,
            req,
            context=self._context,
            connect_ip=self._pinned_connect_ip,
            connect_port=self._pinned_connect_port,
        )


def request_body(request, operation):
    connect_ip = getattr(request, "_connect_ip", None) if isinstance(request, Request) else None
    try:
        if connect_ip:
            parsed = urlparse(request.full_url)
            pinned_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            explicit_port = getattr(request, "_connect_port", None)
            if explicit_port:
                pinned_port = explicit_port
            context = ssl.create_default_context()
            try:
                context.set_alpn_protocols(["http/1.1"])
            except NotImplementedError:
                pass
            handler = _PinnedHTTPSHandler(connect_ip=connect_ip, connect_port=pinned_port, context=context)
            opener = build_opener(ProxyHandler({}), handler)
            with opener.open(request, timeout=10) as response:
                return response.read()
        with urlopen(request, timeout=10) as response:
            return response.read()
    except HTTPError as error:
        error.close()
        raise PushError(f"{operation} (HTTP {error.code})") from None
    except (OSError, ValueError, HTTPException) as error:
        raise PushError(f"{operation} ({type(error).__name__})") from None


DEFAULT_URLS = {
    "google-dev": "https://task-dashboard-dev.velar-agent.duckdns.org",
    "google-prod": "https://task-dashboard.velar-agent.duckdns.org",
}


def push_once(force, target="google-prod"):
    if target == "google-dev":
        suffix = "-google-dev"
        state_file = GOOGLE_DEV_STATE_FILE
    else:
        suffix = "-google-prod"
        state_file = GOOGLE_PROD_STATE_FILE
    source = os.environ.get("SOURCE_URL", "http://127.0.0.1:8787/api/tasks")
    base_url = os.environ.get("WORKER_URL", DEFAULT_URLS.get(target, DEFAULT_URLS["google-prod"]))

    # 두 프로세스가 겹쳐도 소스 조회, PUT, 해시 기록 순서가 뒤집히지 않게 한다.
    with state_file.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        body = request_body(source, "소스 조회")
        snapshot = json.loads(body)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tasks"), list):
            raise PushError("소스 응답에 tasks 배열이 필요합니다.")
        tasks = json.dumps(snapshot["tasks"], sort_keys=True, separators=(",", ":")).encode("ascii")
        digest = hashlib.sha256(tasks).hexdigest().encode("ascii")
        try:
            if not force and state_file.read_bytes().strip() == digest:
                return
        except FileNotFoundError:
            pass

        try:
            key_file = Path(os.environ.get("KEY_FILE", f"~/.config/task-dashboard/ingest-key{suffix}")).expanduser()
            key = key_file.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise PushError(f"KEY_FILE 읽기 실패 ({type(error).__name__})") from None
        if not key or not key.isprintable() or " " in key:
            raise PushError("KEY_FILE에 한 줄의 Bearer 키가 필요합니다.")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "task-dashboard-push/1.0",
        }
        connect_ip = os.environ.get("CONNECT_IP", "").strip()
        ip = None
        if connect_ip:
            try:
                ip = ipaddress.ip_address(connect_ip)
            except ValueError:
                raise PushError("CONNECT_IP 는 IP 주소여야 합니다") from None
        parsed = urlparse(base_url)
        if parsed.scheme.lower() != "https":
            if not (_is_loopback_host(parsed.hostname) or _is_loopback_host(connect_ip)):
                raise PushError("WORKER_URL는 루프백 주소가 아니면 https://여야 합니다")
        if connect_ip:
            original_host = parsed.hostname
            if parsed.scheme.lower() == "https":
                upload = Request(base_url.rstrip("/") + "/api/ingest", method="PUT", data=body, headers=headers)
                upload._connect_ip = str(ip)
                upload._connect_port = parsed.port or 443
            else:
                if original_host:
                    if isinstance(ip, ipaddress.IPv6Address):
                        ip_host = f"[{ip}]"
                    else:
                        ip_host = str(ip)
                    new_netloc = ip_host
                    if parsed.port:
                        new_netloc = f"{ip_host}:{parsed.port}"
                    base_url = urlunparse(parsed._replace(netloc=new_netloc))
                    if parsed.port and parsed.port not in (80, 443):
                        headers["Host"] = f"{original_host}:{parsed.port}"
                    else:
                        headers["Host"] = original_host
                upload = Request(base_url.rstrip("/") + "/api/ingest", method="PUT", data=body, headers=headers)
        else:
            upload = Request(base_url.rstrip("/") + "/api/ingest", method="PUT", data=body, headers=headers)
        request_body(upload, "VM 전송")
        with tempfile.TemporaryDirectory(dir=state_file.parent) as directory:
            staged = Path(directory) / state_file.name
            staged.write_bytes(digest + b"\n")
            staged.replace(state_file)


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("간격은 양수여야 합니다.")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("google-dev", "google-prod"), default="google-prod", help="전송 대상 (기본 google-prod).")
    parser.add_argument("--loop", nargs="?", const=15, type=positive_seconds, metavar="초",
                        help="지정한 간격으로 반복합니다 (기본 15초).")
    parser.add_argument("--force", action="store_true", help="같은 작업도 다시 전송합니다.")
    args = parser.parse_args(argv)
    while True:
        status = 0
        try:
            push_once(args.force, args.target)
        except Exception as error:
            # 원래 예외 메시지에는 서버가 돌려준 키나 헤더 값이 들어갈 수 있다.
            reason = str(error) if isinstance(error, PushError) else type(error).__name__
            print(f"스냅샷 푸시 실패 - {reason}", file=sys.stderr, flush=True)
            status = 1
        if args.loop is None:
            return status
        sleep(args.loop)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
