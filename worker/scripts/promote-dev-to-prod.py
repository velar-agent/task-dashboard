#!/usr/bin/env python3
"""개발 Worker에서 확인한 두 모듈을 운영 Worker에 승격한다."""

import argparse
import email
import json
import os
import subprocess
import sys
import time
import tomllib
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

WORKER = Path(__file__).resolve().parents[1]
REPO = WORKER.parent
sys.path.insert(0, str(REPO))
from server import PAGE

PROD = "task-dashboard"
DEV = "task-dashboard-dev"
PROD_URL = f"https://{PROD}.velar-agent.workers.dev"
DEV_URL = f"https://{DEV}.velar-agent.workers.dev"
ACCOUNT = "a12d19c670e84a71f5acc26df16e47f7"
TOKEN_FILE = "~/.config/usage-deploy/cf-token"
MAIN = "index.js"
MODULES = (MAIN, "html.js")
STORE = {"type": "durable_object_namespace", "name": "STORE", "class_name": "TaskStore"}


class Failure(Exception):
    """응답 본문이나 인증 정보를 포함하지 않는 점검 오류."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(method, url, data=None, headers=None):
    """공개 점검과 관리 API가 사용하는 유일한 HTTP 경계."""
    try:
        req = Request(url, data=data, headers={"User-Agent": "task-dashboard-promote/1.0", **(headers or {})},
                      method=method)
        try:
            response = build_opener(NoRedirect).open(req, timeout=120 if method == "POST" else 20)
        except HTTPError as error:
            response = error
        with response:
            return response.code, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except (OSError, ValueError, HTTPException):
        raise Failure(f"{method} 연결 또는 응답 읽기에 실패했습니다.") from None


def read_token(path):
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not token:
        try:
            with Path(path).expanduser().open(encoding="ascii") as stream:
                if os.fstat(stream.fileno()).st_mode & 0o077:
                    raise Failure("API 토큰 파일 권한을 600으로 설정하세요.", 2)
                token = stream.read().strip()
        except (OSError, UnicodeError):
            raise Failure("API 토큰 파일을 읽을 수 없습니다.", 2) from None
    if not token or not token.isascii() or not token.isprintable() or " " in token:
        raise Failure("한 줄의 API 토큰이 필요합니다.", 2)
    return token


def parse_json(body):
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise Failure("올바른 JSON 응답이 필요합니다.") from None


def smoke(base, expected_page=None):
    for path in ("/healthz", "/", "/api/tasks", "/api/ingest"):
        if path == "/api/ingest":
            # 인증 오류가 있어도 스냅샷을 쓸 수 없는 본문으로 검사한다.
            status, _, _ = http("PUT", base + path, b"{}", {"Content-Type": "application/json"})
            ok = status == 401
        else:
            status, headers, body = http("GET", base + path)
            mime = headers.get("content-type", "").partition(";")[0].strip().lower()
            if path == "/":
                ok = status == 200 and mime == "text/html" and bool(body.strip())
                if ok and expected_page is not None and body != expected_page:
                    raise Failure(f"{base}/ 화면이 로컬 PAGE와 다릅니다.")
            else:
                value = parse_json(body) if status == 200 and mime == "application/json" else None
                if path == "/healthz":
                    ok = value == {"status": "ok"}
                else:
                    ok = isinstance(value, dict) and isinstance(value.get("tasks"), list)
        if not ok:
            raise Failure(f"{base}{path} 점검 실패 (HTTP {status})")
        print(f"OK    {base}{path} (HTTP {status})")


def precheck():
    for command in ([sys.executable, "worker/scripts/sync-html.py", "--check"],
                    ["node", "--test", "worker/test/"], [sys.executable, "-m", "unittest"]):
        print("점검  " + " ".join(command), flush=True)
        if subprocess.run(command, cwd=REPO).returncode:
            raise Failure("로컬 점검에 실패했습니다.")
    smoke(DEV_URL, PAGE.encode())


def unpack_code(headers, body):
    content_type = headers.get("content-type", "")
    entry = headers.get("cf-entrypoint", MAIN)
    if content_type.partition(";")[0].strip().lower() in (
        "application/javascript", "application/javascript+module", "text/javascript"
    ):
        return entry, {entry: body}
    msg = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    if not msg.is_multipart() or msg.defects:
        raise Failure("Worker 모듈 응답을 읽을 수 없습니다.")
    modules = {}
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition") or part.get_filename()
        data = part.get_payload(decode=True)
        if not name or name in modules or data is None:
            raise Failure("Worker 모듈의 이름 또는 본문이 잘못되었습니다.")
        if name == "metadata" and part.get_content_type() == "application/json":
            meta = parse_json(data)
            if not isinstance(meta, dict):
                raise Failure("Worker 모듈 메타데이터가 잘못되었습니다.")
            if "cf-entrypoint" in headers and meta.get("main_module", entry) != entry:
                raise Failure("Worker 진입점 정보가 일치하지 않습니다.")
            entry = meta.get("main_module", entry)
        else:
            modules[name] = data
    return entry, modules


def upload_form(modules):
    config = tomllib.loads((WORKER / "wrangler.toml").read_text(encoding="utf-8"))
    if config.get("name") != PROD or config.get("main") != f"src/{MAIN}":
        raise Failure("wrangler.toml의 운영 Worker 이름 또는 진입점을 확인하세요.")
    # observability는 스크립트 설정이다. Versions API의 허용 필드만 전송한다.
    meta = {"main_module": MAIN, "compatibility_date": config["compatibility_date"],
            "bindings": [STORE, {"type": "inherit", "name": "INGEST_KEY"}]}
    boundary = uuid.uuid4().hex
    parts = []

    def add(name, mime, data, filename=""):
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        parts.append(f"--{boundary}\r\n{disposition}\r\nContent-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")

    add("metadata", "application/json", json.dumps(meta).encode())
    for name, data in modules.items():
        add(name, "application/javascript+module", data, name)
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class WorkerAPI:
    def __init__(self, account, token):
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{quote(account, safe='')}/workers/scripts/"
        self.auth = {"Authorization": f"Bearer {token}"}

    def request(self, method, path, data=None, mime=None):
        headers = self.auth.copy()
        if mime:
            headers["Content-Type"] = mime
        status, response_headers, body = http(method, self.base + path, data, headers)
        if not 200 <= status < 300:
            raise Failure(f"{method} {path} 실패 (HTTP {status})")
        return response_headers, body

    def code(self, name):
        return unpack_code(*self.request("GET", name + "/content/v2"))

    def result(self, method, path, data=None, mime=None):
        _, body = self.request(method, PROD + path, data, mime)
        envelope = parse_json(body)
        if not isinstance(envelope, dict) or envelope.get("success") is not True or "result" not in envelope:
            raise Failure(f"{method} {path} API 응답을 확인할 수 없습니다.")
        return envelope["result"]

    def deployments(self):
        result = self.result("GET", "/deployments")
        deps = result.get("deployments") if isinstance(result, dict) else None
        if not isinstance(deps, list) or not deps or not all(isinstance(d, dict) for d in deps):
            raise Failure("운영 배포 기록을 읽을 수 없습니다.")
        return deps

    def active(self):
        versions = self.deployments()[0].get("versions")
        if isinstance(versions, list) and len(versions) == 1:
            version = versions[0]
            if isinstance(version, dict) and version.get("percentage") == 100:
                version_id = version.get("version_id")
                if isinstance(version_id, str) and version_id:
                    return version_id
        return None

    def latest(self):
        result = self.result("GET", "/versions")
        items = result.get("items") if isinstance(result, dict) else None
        if not items or not isinstance(items, list) or not isinstance(items[0], dict) or not items[0].get("id"):
            raise Failure("최근 업로드 버전을 읽을 수 없습니다.")
        return items[0]["id"]

    def bindings(self, version):
        result = self.result("GET", "/versions/" + quote(version, safe=""))
        resources = result.get("resources") if isinstance(result, dict) else None
        return resources.get("bindings") if isinstance(resources, dict) else None

    def upload(self, body, content_type):
        result = self.result("POST", "/versions?bindings_inherit=strict", body, content_type)
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
            raise Failure("업로드 응답에 버전 ID가 없습니다.")
        return result["id"]

    def activate(self, version, rollback=False, force=False):
        payload = {"strategy": "percentage", "versions": [{"version_id": version, "percentage": 100}],
                   "annotations": {"workers/message": "자정 승격 롤백" if rollback else "개발에서 운영으로 승격"}}
        self.result("POST", "/deployments" + ("?force=true" if force else ""),
                    json.dumps(payload).encode(), "application/json")


def valid_bindings(bindings):
    if not isinstance(bindings, list):
        return False
    by_name = {b["name"]: b for b in bindings if isinstance(b, dict) and isinstance(b.get("name"), str)}
    store = by_name.get("STORE", {})
    return (all(store.get(k) == v for k, v in STORE.items()) and store.get("script_name") in (None, PROD)
            and by_name.get("INGEST_KEY", {}).get("type") == "secret_text")


def retry(check):
    """전파 지연이나 응답 유실 시 읽기 작업만 재시도한다."""
    for delay in (0, 3, 10):
        if delay:
            time.sleep(delay)
        try:
            return check()
        except Failure as error:
            last = error
    raise last


def check_active(api, expected):
    if api.active() != expected:
        raise Failure("활성 배포가 예상 버전 100%와 다릅니다.")


def uploaded_since(api, previous):
    latest = api.latest()
    if latest == previous:
        raise Failure("새 업로드가 아직 보이지 않습니다.")
    return latest


def rollback(api, previous, new):
    print(f"ROLLBACK  이전 버전 {previous} 복구를 확인합니다.", file=sys.stderr)
    try:
        active = retry(api.active)
        if active == new:
            try:
                api.activate(previous, rollback=True)
            except Failure:
                # 롤백 응답이 유실됐으면 먼저 실제 트래픽을 조회한다.
                try:
                    retry(lambda: check_active(api, previous))
                except Failure:
                    check_active(api, new)
                    api.activate(previous, rollback=True, force=True)
        elif active != previous:
            raise Failure("다른 운영 배포가 활성 상태입니다.")
    except Failure as error:
        print(f"롤백 요청 확인 필요 — {error}", file=sys.stderr)

    def restored():
        check_active(api, previous)
        smoke(PROD_URL)  # 이전 버전의 화면은 현재 main과 다를 수 있다.

    try:
        retry(restored)
    except Failure as error:
        raise Failure(f"롤백 후 운영 상태를 확인할 수 없습니다. 수동 확인이 필요합니다. {error}", 6) from None
    raise Failure(f"이전 버전 {previous} 롤백 및 운영 점검을 마쳤습니다.", 5)


def deploy(api, dry_run, force):
    modules = {name: (WORKER / "src" / name).read_bytes() for name in MODULES}
    local = MAIN, modules
    if api.code(DEV) != local:
        raise Failure("개발 live 코드가 로컬 코드와 달라 승격을 거부합니다.", 3)
    previous = api.active()
    prod_code = api.code(PROD)
    if not previous or previous != api.latest():
        raise Failure("운영의 최근 업로드가 단일 활성 버전 100%와 일치해야 합니다.", 7)
    if not valid_bindings(api.bindings(previous)):
        raise Failure("운영 활성 버전의 STORE 또는 INGEST_KEY 바인딩을 확인하세요.", 7)
    print(f"OK    개발 코드 일치, 운영 활성 버전 {previous} 및 필수 바인딩 확인")
    if prod_code == local and not force:
        print("SKIP  개발과 운영 코드가 같습니다.")
        return
    body, content_type = upload_form(modules)
    if dry_run:
        print("DRY   새 버전 업로드 → 바인딩 확인 → 100% 배포 → 운영 점검 예정")
        return
    try:
        new = api.upload(body, content_type)
    except Failure as error:
        # 응답이 유실되면 최근 버전을 다시 읽되 출처가 불명확한 버전에 트래픽을 주지 않는다.
        try:
            latest = retry(lambda: uploaded_since(api, previous))
            print(f"최근 업로드 버전 {latest}을 수동으로 확인하세요.", file=sys.stderr)
        except Failure:
            print("새 업로드를 확인할 수 없습니다.", file=sys.stderr)
        raise Failure(f"버전 업로드 단계에서 중단했습니다. {error}", 4) from None
    print(f"업로드  {new} (직전 활성 버전 {previous})")
    try:
        if not valid_bindings(api.bindings(new)):
            raise Failure("새 버전의 STORE 또는 INGEST_KEY 바인딩이 잘못되었습니다.")
        check_active(api, previous)
        if api.latest() != new:
            raise Failure("확인 중 다른 버전이 업로드되었습니다.")
    except Failure as error:
        raise Failure(f"트래픽 전환 전 중단했습니다. {error}", 4) from None
    try:
        api.activate(new)
    except Failure:
        # POST 응답이 유실돼도 실제 활성 버전이면 점검·롤백 경로를 계속 탄다.
        try:
            retry(lambda: check_active(api, new))
        except Failure:
            try:
                check_active(api, previous)
            except Failure:
                raise Failure("배포 요청 후 활성 버전을 확인할 수 없습니다. 수동 확인이 필요합니다.", 6) from None
            raise Failure("새 배포를 확인할 수 없습니다. 이전 버전이 활성 상태입니다.", 4) from None

    def healthy():
        check_active(api, new)
        if api.code(PROD) != local or not valid_bindings(api.bindings(new)):
            raise Failure("배포 후 운영 코드 또는 바인딩이 다릅니다.")
        smoke(PROD_URL, PAGE.encode())

    try:
        retry(healthy)
    except Failure as error:
        print(f"FAIL  배포 후 점검 실패 — {error}", file=sys.stderr)
        rollback(api, previous, new)
    print(f"OK    운영 버전 {new} 배포 및 점검 완료")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(10, "명령과 옵션을 확인하세요. 도움말은 --help입니다.\n")


def main(argv=None):
    auth = argparse.ArgumentParser(add_help=False)
    auth.add_argument("--account", default=argparse.SUPPRESS, help="Cloudflare 계정 ID")
    auth.add_argument("--token-file", default=argparse.SUPPRESS, help="Cloudflare API 토큰 파일")
    parser = Parser(description=__doc__, parents=[auth])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("precheck", help="로컬 코드, 테스트와 개발 Worker 점검")
    commands.add_parser("postcheck", help="운영 Worker 점검")
    commands.add_parser("status", parents=[auth], help="최근 운영 배포와 버전 표시")
    promotion = commands.add_parser("deploy", parents=[auth], help="개발 코드를 운영에 승격")
    promotion.add_argument("--name", default=PROD, help="승격 대상 (운영 Worker만 허용)")
    promotion.add_argument("--dry-run", action="store_true", help="읽기 점검 후 배포 계획 표시")
    promotion.add_argument("--force", action="store_true", help="운영 코드가 같아도 새 버전 배포")
    args = parser.parse_args(argv)
    try:
        if args.command == "precheck":
            precheck()
        elif args.command == "postcheck":
            smoke(PROD_URL)
        else:
            if args.command == "deploy" and args.name != PROD:
                raise Failure(f"승격 대상은 운영 Worker {PROD}만 허용합니다.", 2)
            api = WorkerAPI(getattr(args, "account", ACCOUNT), read_token(getattr(args, "token_file", TOKEN_FILE)))
            if args.command == "deploy":
                deploy(api, args.dry_run, args.force)
            else:
                for dep in api.deployments()[:5]:
                    versions = ", ".join(f"{v['version_id']}@{v['percentage']}%" for v in dep["versions"])
                    print(f"{dep.get('created_on', '')} 배포 {dep['id']}  {versions}")
        return 0
    except Failure as error:
        print(error, file=sys.stderr)
        return error.code
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"설정, 파일 또는 응답 확인 실패 ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
