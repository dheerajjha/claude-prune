"""End to end: claude-prune against real processes and a fake Claude Code install.

Stand-in "Claude" builds are compiled into a temporary home and started the
way stale sessions end up, their parent gone and launchd their new parent.
claude-prune then runs on them for real. It starts and stops processes, so
it only runs on macOS with CLAUDE_PRUNE_E2E=1; CI runs it on macos-latest.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "claude_prune.py")

# macOS launch constraints SIGKILL copies of system binaries such as /bin/sleep
# run from another path, so the stand-in for a Claude build is compiled.
# With no arguments it forks a `sleep`; given a program, it forks that.
STANDIN = r"""
#include <unistd.h>
int main(int argc, char **argv) {
    if (fork() == 0) {
        if (argc > 1) execv(argv[1], argv + 1);
        else execl("/bin/sleep", "sleep", "900", (char *)0);
        _exit(1);
    }
    sleep(900);
    return 0;
}
"""


def table():
    rows = {}
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,stat=,command="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4:
            rows[int(fields[0])] = {"ppid": int(fields[1]), "stat": fields[2], "cmd": fields[3]}
    return rows


def running(pid):
    row = table().get(pid)
    return row is not None and not row["stat"].startswith("Z")


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("CLAUDE_PRUNE_E2E") == "1",
                     "starts and stops real processes: macOS with CLAUDE_PRUNE_E2E=1 only")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="prune-e2e-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(self.stop_leftovers)
        share = os.path.join(self.home, ".local/share/claude")
        versions = os.path.join(share, "versions")
        app_dir = os.path.join(share, "ClaudeCode.app/Contents/MacOS")
        for d in (versions, app_dir, os.path.join(self.home, ".local/bin")):
            os.makedirs(d)
        source = os.path.join(self.home, "standin.c")
        with open(source, "w") as fh:
            fh.write(STANDIN)
        self.old, self.cur = os.path.join(versions, "2.1.0"), os.path.join(versions, "2.2.0")
        for build in (self.old, self.cur):
            subprocess.run(["cc", "-o", build, source], check=True)
        os.symlink(self.cur, os.path.join(self.home, ".local/bin/claude"))
        self.app = os.path.join(app_dir, "claude")
        os.link(self.cur, self.app)
        self.mcp = os.path.join(self.home, "fake-mcp")
        with open(self.mcp, "w") as fh:
            fh.write("#!/bin/sh\nsleep 900\n")
        os.chmod(self.mcp, 0o755)

    def launch(self, *argv, shown=None):
        """Start argv the way a stale session ends up: its parent gone, adopted by launchd."""
        subprocess.run(["/bin/sh", "-c", 'nohup "$@" >/dev/null 2>&1 &', "sh", *argv], check=True)
        cmd = shown or " ".join(argv)
        return self.find(lambda row: row["cmd"] == cmd and row["ppid"] == 1, cmd)

    def child(self, pid):
        return self.find(lambda row: row["ppid"] == pid, f"a child of {pid}")

    def find(self, match, what):
        deadline = time.time() + 10
        while time.time() < deadline:
            for pid, row in table().items():
                if match(row):
                    return pid
            time.sleep(0.1)
        self.fail(f"{what} never appeared")

    def prune(self, *args):
        env = dict(os.environ, CLAUDE_PRUNE_HOME=self.home)
        result = subprocess.run([sys.executable, SCRIPT, *args], env=env, capture_output=True, text=True,
                                timeout=120)
        sys.stdout.write(result.stdout)  # keep the real output in the CI log
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_gone(self, pids):
        deadline = time.time() + 10
        while time.time() < deadline and any(running(p) for p in pids):
            time.sleep(0.2)
        self.assertEqual([p for p in pids if running(p)], [])

    def stop_leftovers(self):
        """Kill whatever a failed run left behind, children included, and check that it worked."""
        rows = table()
        found, stack = set(), [pid for pid, row in rows.items() if self.home in row["cmd"]]
        while stack:
            pid = stack.pop()
            if pid not in found:
                found.add(pid)
                stack.extend(p for p, row in rows.items() if row["ppid"] == pid)
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.assert_gone(sorted(found))

    def test_prunes_old_builds_and_orphans_and_nothing_else(self):
        old_tree = self.launch(self.old)  # a stale session on an old build
        old_sleep = self.child(old_tree)
        old_parent = self.launch(self.old, self.cur)  # an old window whose child runs the current build
        cur_child = self.child(old_parent)
        cur_sleep = self.child(cur_child)
        app = self.launch(self.app)  # the current build, started through the app bundle's hardlink
        app_sleep = self.child(app)
        mcp = self.launch(self.mcp, shown="/bin/sh " + self.mcp)  # an MCP server whose session is gone
        mcp_sleep = self.child(mcp)
        current = [cur_child, cur_sleep, app, app_sleep]

        dry = self.prune("-n")
        self.assertIn(f"pid {old_tree}, 2 procs  (sleep)", dry)
        self.assertIn(f"pid {old_parent}, 1 proc\n", dry)
        self.assertIn(f"pid {mcp}  (+ sleep)", dry)
        self.assertIn("Old builds on disk: 2.1.0", dry)
        self.assertTrue(all(running(p) for p in [old_tree, old_sleep, old_parent, mcp, mcp_sleep] + current))
        self.assertTrue(os.path.exists(self.old))

        out = self.prune()
        self.assertIn("Done: stopped 5 processes", out)
        self.assertIn("deleted 1 old build", out)
        self.assert_gone([old_tree, old_sleep, old_parent, mcp, mcp_sleep])
        self.assertTrue(all(running(p) for p in current))
        self.assertFalse(os.path.exists(self.old))
        self.assertEqual(os.stat(self.cur).st_ino, os.stat(self.app).st_ino)

        self.prune("--all")
        self.assert_gone(current)
        self.assertTrue(os.path.exists(self.cur))


if __name__ == "__main__":
    unittest.main()
