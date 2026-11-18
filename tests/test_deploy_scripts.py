import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


BOX_START = Path(__file__).resolve().parents[1] / "deploy" / "box-start.sh"


class BoxStartTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / ".local" / "bin"
        self.bin.mkdir(parents=True)
        # box-start.sh와 함께 띄워야 할 저장소 루트 구조 재생
        self.repo = self.root / "repo"
        self.deploy = self.repo / "deploy"
        self.deploy.mkdir(parents=True)
        shutil.copy(BOX_START, self.deploy / "box-start.sh")
        self.script = self.deploy / "box-start.sh"
        self.script.chmod(0o755)
        # 스크립트가 cd로 이동하는 저장소 루트에 server.py와 push 스크립트가 있다고 가정
        (self.repo / "server.py").write_text("# dummy\n")
        (self.deploy / "push-to-worker.py").write_text("# dummy\n")

        self.calls = self.root / "calls.log"
        self.calls.write_text("")
        self.env = {
            "HOME": str(self.root),
            "PATH": f"{self.bin}:{os.defpath}",
            "CALLS": str(self.calls),
        }

    def _stub_pgrep_default(self, server_exit="1", dev_exit="1", prod_exit="1"):
        self._stub("pgrep", f"""
echo "pgrep $*" >> "$CALLS"
# pgrep 인자 패턴으로 종료 코드를 결정한다
if echo "$*" | grep -q "server.py"; then
  exit {server_exit}
elif echo "$*" | grep -q "google-dev"; then
  exit {dev_exit}
elif echo "$*" | grep -q "google-prod"; then
  exit {prod_exit}
fi
exit {server_exit}
""")

    def _stub_setsid(self):
        self._stub("setsid", """
# 환경변수와 인자를 그대로 기록하고 실제 nohup/python은 실행하지 않는다
echo "setsid PORT=${PORT:-} CONNECT_IP=${CONNECT_IP:-} CMD=$*" >> "$CALLS"
exit 0
""")

    def _stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -eu\n" + body)
        path.chmod(0o755)
        return str(path)

    def _invoke(self, **env):
        e = dict(self.env)
        e.update(env)
        result = subprocess.run([str(self.script)], env=e, capture_output=True, text=True, timeout=10, cwd=str(self.repo))
        # 백그라운드 setsid 호출이 CALLS에 기록될 시간을 준다.
        time.sleep(0.2)
        return result

    def test_script_syntax(self):
        result = subprocess.run(["bash", "-n", str(BOX_START)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        # 실행 권한
        self.assertTrue(os.access(BOX_START, os.X_OK))

    def test_starts_all_missing_processes(self):
        self._stub_pgrep_default(server_exit="1", dev_exit="1", prod_exit="1")
        self._stub_setsid()
        result = self._invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        # pgrep가 3번 호출되어야 한다
        pgrep_calls = [c for c in calls if c.startswith("pgrep")]
        setsid_calls = [c for c in calls if c.startswith("setsid")]
        self.assertEqual(len(pgrep_calls), 3)
        self.assertEqual(len(setsid_calls), 3)
        # 서버 시작 확인 (PORT=8787)
        self.assertTrue(any("PORT=8787" in c and "server.py" in c for c in setsid_calls))
        # 두 푸시 루프 모두 CONNECT_IP 기본값 포함 및 target 확인
        self.assertTrue(any("google-dev" in c and "--loop 15" in c for c in setsid_calls))
        self.assertTrue(any("google-prod" in c and "--loop 15" in c for c in setsid_calls))
        self.assertTrue(all("CONNECT_IP=35.252.213.245" in c for c in setsid_calls if "push-to-worker" in c))

    def test_idempotent_when_all_running(self):
        self._stub_pgrep_default(server_exit="0", dev_exit="0", prod_exit="0")
        self._stub_setsid()
        result = self._invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertTrue(any(c.startswith("pgrep") for c in calls))
        # 이미 떠 있으면 setsid가 한 번도 호출되지 않는다
        self.assertFalse(any(c.startswith("setsid") for c in calls))

    def test_only_missing_pushes_start(self):
        # 서버만 떠 있고 푸시는 없는 경우
        self._stub_pgrep_default(server_exit="0", dev_exit="1", prod_exit="1")
        self._stub_setsid()
        result = self._invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        setsid_calls = [c for c in calls if c.startswith("setsid")]
        self.assertEqual(len(setsid_calls), 2)
        self.assertTrue(any("google-dev" in c for c in setsid_calls))
        self.assertTrue(any("google-prod" in c for c in setsid_calls))
        self.assertFalse(any("server.py" in c for c in setsid_calls))

    def test_connect_ip_env_overrides_default(self):
        self._stub_pgrep_default(server_exit="1", dev_exit="1", prod_exit="1")
        self._stub_setsid()
        result = self._invoke(CONNECT_IP="203.0.113.99")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        setsid_calls = [c for c in calls if c.startswith("setsid") and "push-to-worker" in c]
        self.assertEqual(len(setsid_calls), 2)
        self.assertTrue(all("CONNECT_IP=203.0.113.99" in c for c in setsid_calls))

    def test_no_secret_in_output(self):
        self._stub_pgrep_default(server_exit="1", dev_exit="1", prod_exit="1")
        self._stub_setsid()
        # 키/토큰이 환경에 있어도 스크립트 출력에 없어야 한다
        result = self._invoke(KEY_FILE="/tmp/secret-key", INGEST_KEY="should-not-appear")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("should-not-appear", result.stdout + result.stderr)
        # 스크립트 본문에도 키 패턴이 없어야 한다
        text = BOX_START.read_text()
        self.assertNotIn("INGEST_KEY=", text)

