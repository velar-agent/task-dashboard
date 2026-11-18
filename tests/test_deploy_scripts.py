import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "worker" / "scripts"


class DeployScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.scripts = self.root / "repo" / "worker" / "scripts"
        self.scripts.mkdir(parents=True)
        self.calls = self.root / "calls"
        self.state = self.root / ".local" / "state" / "task-dashboard-deploy"
        self.env = {"HOME": str(self.root), "PATH": f"{self.bin}:{os.defpath}", "CALLS": str(self.calls)}
        for name in ("nightly-promote.sh", "deploy-dev.sh"):
            shutil.copy(SCRIPTS / name, self.scripts / name)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o700)
        return str(path)

    def invoke(self, name, *args, **env):
        return subprocess.run([self.scripts / name, *args], env=self.env | env, cwd=self.root,
                              capture_output=True, text=True, timeout=10)

    def set_up_runner(self):
        self.stub("git", """
echo "git $*" >> "$CALLS"
case "$*" in
  "rev-parse --abbrev-ref HEAD") echo "${BRANCH:-main}" ;;
  "status --porcelain --untracked-files=normal") echo "${CHANGES:-}" ;;
  "pull --ff-only") exit "${PULL_EXIT:-0}" ;;
  "rev-parse --verify HEAD") echo main-sha ;;
  "rev-parse --verify origin/main") echo "${REMOTE:-main-sha}" ;;
  *) exit 80 ;;
esac
""")
        (self.scripts / "promote-dev-to-prod.py").write_text("""
import os, sys
with open(os.environ["CALLS"], "a") as calls:
    print(*sys.argv[1:], file=calls)
sys.exit(int(os.environ.get(sys.argv[1].upper() + "_EXIT", 0)))
""")

    def test_nightly_uses_relative_repo_records_exit_and_keeps_60_days(self):
        self.set_up_runner()
        self.state.mkdir(parents=True)
        expired = self.state / "2000-01-01.log"
        expired.touch()
        old = time.time() - 61 * 86400
        os.utime(expired, (old, old))
        result = self.invoke("nightly-promote.sh", "--dry-run", "--force", DEPLOY_EXIT="5")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertFalse(expired.exists())
        last = json.loads((self.state / "last-run.json").read_text())
        self.assertEqual((last["exit"], last["args"]), (5, "--dry-run --force"))
        self.assertIn("exit=5", Path(last["log"]).read_text())
        calls = self.calls.read_text().splitlines()
        self.assertLess(calls.index("git pull --ff-only"), calls.index("precheck"))
        self.assertEqual(calls[-1], f"deploy --token-file {self.root}/.config/usage-deploy/cf-token --dry-run --force")

    def test_nightly_blocks_git_or_precheck_failures(self):
        self.set_up_runner()
        for env in ({"BRANCH": "topic"}, {"CHANGES": "?? local.js"}, {"PULL_EXIT": "1"},
                    {"REMOTE": "different"}, {"PRECHECK_EXIT": "1"}):
            with self.subTest(env=env):
                self.calls.write_text("")
                self.assertEqual(self.invoke("nightly-promote.sh", **env).returncode, 1)
                self.assertEqual(json.loads((self.state / "last-run.json").read_text())["exit"], 1)
                self.assertNotIn("deploy --", self.calls.read_text())

    def test_nightly_lock_and_usage_do_not_overwrite_running_status(self):
        self.assertEqual(self.invoke("nightly-promote.sh", "--unknown").returncode, 10)
        self.state.mkdir(parents=True)
        with (self.state / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.invoke("nightly-promote.sh").returncode, 9)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.state / "last-run.json").exists())

    def test_dev_helper_orders_local_checks_before_dev_only_deployment(self):
        token = self.root / ".config" / "usage-deploy" / "cf-token"
        token.parent.mkdir(parents=True)
        token.write_text("offline-fixture\n")
        token.chmod(0o600)
        for name in ("python3", "node"):
            self.stub(name, 'echo "$(basename "$0") $*" >> "$CALLS"\nexit "${TEST_EXIT:-0}"\n')
        wrangler = self.stub("wrangler", """
test "$CLOUDFLARE_API_TOKEN" = offline-fixture
test "$(basename "$PWD")" = worker
echo "wrangler $*" >> "$CALLS"
""")
        result = self.invoke("deploy-dev.sh", WRANGLER=wrangler)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("offline-fixture", result.stdout + result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), [
            "python3 worker/scripts/sync-html.py", "node --test worker/test/", "python3 -m unittest",
            "wrangler deploy --env dev"])
        self.calls.unlink()
        self.assertEqual(self.invoke("deploy-dev.sh", "--env", "prod", WRANGLER=wrangler).returncode, 10)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.invoke("deploy-dev.sh", WRANGLER=wrangler, TEST_EXIT="1").returncode, 1)
        self.assertNotIn("wrangler", self.calls.read_text())
