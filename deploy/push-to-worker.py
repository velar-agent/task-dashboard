#!/usr/bin/env python3
"""box의 작업 스냅샷이 바뀌면 Worker에 전송한다."""

import argparse
import fcntl
import hashlib
import json
import math
import os
import sys
import tempfile
from http.client import HTTPException
from pathlib import Path
from time import sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen


STATE_FILE = Path(f"/tmp/task-push-{os.getuid()}.sha")


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


def push_once(force):
    source = os.environ.get("SOURCE_URL", "http://127.0.0.1:8787/api/tasks")
    worker = os.environ.get("WORKER_URL", "https://task-dashboard.velar-agent.workers.dev")

    # 두 프로세스가 겹쳐도 소스 조회, PUT, 해시 기록 순서가 뒤집히지 않게 한다.
    with STATE_FILE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        body = request_body(source, "소스 조회")
        snapshot = json.loads(body)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tasks"), list):
            raise PushError("소스 응답에 tasks 배열이 필요합니다.")
        tasks = json.dumps(snapshot["tasks"], sort_keys=True, separators=(",", ":")).encode("ascii")
        digest = hashlib.sha256(tasks).hexdigest().encode("ascii")
        try:
            if not force and STATE_FILE.read_bytes().strip() == digest:
                return
        except FileNotFoundError:
            pass

        try:
            key_file = Path(os.environ.get("KEY_FILE", "~/.config/task-dashboard/ingest-key")).expanduser()
            key = key_file.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise PushError(f"KEY_FILE 읽기 실패 ({type(error).__name__})") from None
        if not key or not key.isprintable() or " " in key:
            raise PushError("KEY_FILE에 한 줄의 Bearer 키가 필요합니다.")
        upload = Request(worker.rstrip("/") + "/api/ingest", method="PUT", data=body, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "task-dashboard-push/1.0",
        })
        request_body(upload, "Worker 전송")
        with tempfile.TemporaryDirectory(dir=STATE_FILE.parent) as directory:
            staged = Path(directory) / STATE_FILE.name
            staged.write_bytes(digest + b"\n")
            staged.replace(STATE_FILE)


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("간격은 양수여야 합니다.")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", nargs="?", const=15, type=positive_seconds, metavar="초",
                        help="지정한 간격으로 반복합니다 (기본 15초).")
    parser.add_argument("--force", action="store_true", help="같은 작업도 다시 전송합니다.")
    args = parser.parse_args(argv)
    while True:
        status = 0
        try:
            push_once(args.force)
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
