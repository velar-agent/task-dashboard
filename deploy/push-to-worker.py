#!/usr/bin/env python3
"""box의 작업 스냅샷이 바뀌면 구글 VM dev/prod 에 전송한다.

google-dev/google-prod 대상은 plain HTTP 전송을 하며 Bearer 키가 평문으로 네트워크에 노출된다.
"""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import sys
import tempfile
from http.client import HTTPException
from pathlib import Path
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


GOOGLE_DEV_STATE_FILE = Path(f"/tmp/task-push-google-dev-{os.getuid()}.sha")
GOOGLE_PROD_STATE_FILE = Path(f"/tmp/task-push-google-prod-{os.getuid()}.sha")


class PushError(Exception):
    """키나 응답 본문을 포함하지 않는 사용자용 오류."""


def request_body(request, operation):
    try:
        with urlopen(request, timeout=10) as response:
            return response.read()
    except HTTPError as error:
        error.close()
        raise PushError(f"{operation} (HTTP {error.code})") from None
    except (OSError, ValueError, HTTPException) as error:
        raise PushError(f"{operation} ({type(error).__name__})") from None


DEFAULT_URLS = {
    "google-dev": "http://task-dashboard-dev.velar-agent.duckdns.org",
    "google-prod": "http://task-dashboard.velar-agent.duckdns.org",
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
        if connect_ip:
            try:
                ip = ipaddress.ip_address(connect_ip)
            except ValueError:
                raise PushError("CONNECT_IP 는 IP 주소여야 합니다") from None
            parsed = urlparse(base_url)
            original_host = parsed.hostname
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
