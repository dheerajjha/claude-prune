"""Unit tests for claude-prune.

Hermetic: every test runs against a fake Mac, a temporary home holding a
Claude Code install plus a process table, lsof answers and launchd jobs
supplied here. Signals and external commands go through stubs, so no test
can stop a real process or run ps, lsof, top or launchctl. Each rule in the
README's "How it decides" has a test below.
"""
import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import claude_prune as cp  # noqa: E402

ME = cp.UID
NODE = ("4001", "/opt/homebrew/bin/node")
SHELL = ("4002", "/bin/zsh")
PYTHON = ("4003", "/usr/bin/python3")
TERMINAL, SELF = 800, 900


class Clock:
    """time for stop(): sleeping moves the clock instead of waiting."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeMac(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        share = os.path.join(self.home, ".local/share/claude")
        self.versions = os.path.join(share, "versions")
        os.makedirs(self.versions)
        os.makedirs(os.path.join(self.home, ".local/bin"))
        for v in ("2.0.5", "2.1.0", "2.2.0"):
            with open(os.path.join(self.versions, v), "wb") as fh:
                fh.write(b"\0" * 1024)
        self.link = os.path.join(self.home, ".local/bin/claude")
        os.symlink(self.path("2.2.0"), self.link)
        os.makedirs(os.path.join(share, "ClaudeCode.app/Contents/MacOS"))
        self.app = os.path.join(share, "ClaudeCode.app/Contents/MacOS/claude")
        os.link(self.path("2.2.0"), self.app)  # the app bundle is a hardlink to the current build

        home = cp.HOME
        cp.configure(self.home)
        self.addCleanup(cp.configure, home)

        self.procs, self.maps, self.launchd = {}, {}, set()
        self.signals, self.commands = [], []
        self.stubborn, self.on_term, self.holders, self.outputs = set(), {}, {}, {}
        self.clock = Clock()
        for name, fake in (("snapshot", self.fake_snapshot), ("mapped_binaries", self.fake_lsof),
                           ("launchd_jobs", lambda: set(self.launchd)), ("send", self.fake_send),
                           ("run", self.fake_run), ("time", self.clock)):
            patcher = mock.patch.object(cp, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.spawn(TERMINAL, 1, "-zsh", SHELL)
        self.spawn(SELF, TERMINAL, "python3 /Users/me/.local/bin/claude-prune", PYTHON)

    # -- the fake machine

    def path(self, version):
        return os.path.realpath(os.path.join(self.versions, version))

    def build(self, version):
        """What lsof reports for a process running this build: (inode, path)."""
        return str(os.stat(self.path(version)).st_ino), self.path(version)

    def spawn(self, pid, ppid, cmd, binary, uid=ME):
        self.procs[pid] = {"ppid": ppid, "uid": uid, "etime": "1-02:03:04", "cmd": cmd}
        self.maps[pid] = [binary]
        return pid

    def fake_snapshot(self):
        return {pid: dict(info) for pid, info in self.procs.items()}

    def fake_lsof(self, pids):
        return {pid: list(self.maps[pid]) for pid in pids if pid in self.procs}

    def fake_send(self, pid, sig):
        if pid not in self.procs:
            raise ProcessLookupError(pid)
        if sig == 0:
            return
        self.signals.append((pid, sig))
        if sig == signal.SIGTERM and pid in self.on_term:
            self.on_term.pop(pid)()
        if sig == signal.SIGKILL or pid not in self.stubborn:
            del self.procs[pid]

    def fake_run(self, *cmd):
        self.commands.append(cmd)
        if cmd[:2] == ("lsof", "-t"):
            return self.holders.get(cmd[2], "")
        return self.outputs.get(cmd[0], "")

    # -- driving it

    def plan(self, everything=False):
        return cp.plan(everything, self_pid=SELF)

    def main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(os, "geteuid", return_value=501), \
                mock.patch.object(os, "getpid", return_value=SELF), \
                contextlib.redirect_stdout(out):
            cp.main(list(argv))
        return out.getvalue()

    def terms(self):
        return [pid for pid, sig in self.signals if sig == signal.SIGTERM]


class Recognition(FakeMac):
    def test_argv_that_mentions_claude_is_not_claude(self):
        self.spawn(10, TERMINAL, "grep claude", ("4004", "/usr/bin/grep"))
        self.spawn(11, 1, "/bin/sh /tmp/claude-501/update.sh 2.1.0", ("4005", "/bin/sh"))
        self.spawn(12, TERMINAL, "less /Users/me/.local/share/claude/versions/2.1.0", ("4006", "/usr/bin/less"))
        pl = self.plan()
        self.assertEqual(pl["claude"], {})
        self.assertEqual(pl["targets"] | pl["orphans"], set())

    def test_an_old_build_goes_with_everything_it_started(self):
        old = self.build("2.1.0")
        self.spawn(20, 1, "claude bg-pty-host --bg-pty-host /tmp/s.sock", old)
        self.spawn(21, 20, "claude bg-spare --bg-spare /tmp/c.sock", old)
        self.spawn(22, 21, "node /x/.bin/mcp-server-github", NODE)
        self.spawn(23, 21, "caffeinate -i", ("4007", "/usr/bin/caffeinate"))
        pl = self.plan()
        self.assertEqual(pl["targets"], {20, 21, 22, 23})
        self.assertEqual((pl["claude"][20]["role"], pl["claude"][21]["role"]), ("pty-host", "background session"))
        self.assertEqual(pl["claude"][21]["ver"], "2.1.0")

    def test_the_current_build_is_whatever_the_link_points_to(self):
        self.spawn(30, 1, "claude daemon run --json-path x", self.build("2.2.0"))
        pl = self.plan()
        self.assertEqual(pl["cur"], "2.2.0")
        self.assertTrue(pl["claude"][30]["current"])
        self.assertEqual(pl["targets"], set())

    def test_the_app_bundle_hardlink_is_the_current_build(self):
        self.spawn(31, 1, "claude", (str(os.stat(self.app).st_ino), self.app))
        pl = self.plan()
        self.assertTrue(pl["claude"][31]["current"])
        self.assertEqual(pl["targets"], set())

    def test_an_app_bundle_replaced_since_is_old(self):
        self.spawn(32, 1, "claude", ("999999", self.app))
        pl = self.plan()
        self.assertEqual(pl["claude"][32]["ver"], "old app build")
        self.assertIn(32, pl["targets"])

    def test_a_build_rewritten_under_the_current_name_is_old(self):
        self.spawn(33, 1, "claude", ("999998", self.path("2.2.0")))
        pl = self.plan()
        self.assertEqual(pl["claude"][33]["ver"], "2.2.0 (replaced)")
        self.assertIn(33, pl["targets"])

    def test_without_an_inode_a_process_is_never_called_stale_on_a_guess(self):
        self.spawn(34, 1, "claude", (None, self.app))
        self.spawn(35, 1, "claude", (None, self.path("2.2.0")))
        self.spawn(36, 1, "claude", (None, self.path("2.1.0")))
        pl = self.plan()
        self.assertTrue(pl["claude"][34]["current"])
        self.assertTrue(pl["claude"][35]["current"])
        self.assertEqual(pl["targets"], {36})

    def test_other_users_and_the_desktop_app_are_left_alone(self):
        self.spawn(37, 1, "claude bg-spare --bg-spare /tmp/c.sock", self.build("2.1.0"), uid=ME + 1)
        desktop = "/Applications/Claude.app/Contents/MacOS/Claude"
        self.spawn(38, 1, desktop, ("4008", desktop))
        pl = self.plan()
        self.assertEqual(pl["claude"], {})
        self.assertEqual(pl["targets"] | pl["orphans"], set())


class Trees(FakeMac):
    def test_an_old_window_does_not_take_the_current_daemon_with_it(self):
        old, cur = self.build("2.1.0"), self.build("2.2.0")
        self.spawn(40, TERMINAL, "claude agents", old)
        self.spawn(41, 40, "node /x/.bin/mcp-server-playwright", NODE)
        self.spawn(42, 40, "claude daemon run --json-path x", cur)  # the current daemon, started by the old window
        self.spawn(43, 42, "claude bg-pty-host --bg-pty-host /tmp/s.sock", cur)
        self.spawn(44, 43, "claude bg-spare --bg-spare /tmp/c.sock", cur)
        self.spawn(45, 44, "node /x/.bin/mcp-server-github", NODE)
        pl = self.plan()
        self.assertEqual(pl["claude"][40]["role"], "agents window")
        self.assertEqual(pl["targets"], {40, 41})
        self.assertEqual(self.plan(everything=True)["targets"], {40, 41, 42, 43, 44, 45})


class Labels(FakeMac):
    def rows(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cp.print_trees(self.plan(), {})
        return out.getvalue().splitlines()

    def test_a_tree_is_labelled_by_its_top_claude_process(self):
        old = self.build("2.1.0")
        # PIDs wrap, so a daemon can have a higher PID than the spares it started.
        self.spawn(99, 1, "claude daemon run --json-path x", old)
        self.spawn(97, 99, "claude bg-pty-host --bg-pty-host /tmp/a.sock", old)
        self.spawn(92, 97, "claude bg-spare --bg-spare /tmp/a.claim", old)
        self.spawn(93, 92, "node /x/.bin/mcp-server-github", NODE)
        # An orphaned pty-host is labelled by the session it hosts.
        self.spawn(95, 1, "claude bg-pty-host --bg-pty-host /tmp/b.sock", old)
        self.spawn(96, 95, "claude bg-spare --bg-spare /tmp/b.claim", old)
        rows = self.rows()
        daemon = next(r for r in rows if "pid 99," in r)
        orphan = next(r for r in rows if "pid 95," in r)
        self.assertRegex(daemon, r"^\s+2\.1\.0\s+daemon\s")
        self.assertTrue(daemon.endswith("4 procs  (background session, mcp-server-github, pty-host)"), daemon)
        self.assertRegex(orphan, r"^\s+2\.1\.0\s+background session\s")
        self.assertTrue(orphan.endswith("2 procs  (pty-host)"), orphan)


class Protection(FakeMac):
    def setUp(self):
        super().setUp()
        old = self.build("2.1.0")
        # This program runs in an old-build background session, under an old daemon.
        self.spawn(50, 1, "claude daemon run --json-path x", old)
        self.spawn(51, 50, "claude bg-pty-host --bg-pty-host /tmp/a.sock", old)
        self.spawn(52, 51, "claude bg-spare --bg-spare /tmp/a.claim", old)
        self.spawn(53, 52, "node /x/.bin/mcp-server-github", NODE)
        self.spawn(54, 52, "/bin/zsh -c claude-prune", SHELL)
        self.procs[SELF]["ppid"] = 54
        # Another session of the same daemon.
        self.spawn(55, 50, "claude bg-pty-host --bg-pty-host /tmp/b.sock", old)
        self.spawn(56, 55, "claude bg-spare --bg-spare /tmp/b.claim", old)
        self.spawn(57, 56, "node /x/.bin/mcp-server-github", NODE)

    def test_the_hosting_session_its_pty_host_and_its_mcp_servers_are_kept(self):
        for everything in (False, True):
            pl = self.plan(everything)
            self.assertEqual(pl["host"], 52)
            self.assertTrue({SELF, 54, 52, 51, 53} <= pl["keep"])
            self.assertEqual(pl["keep"] & (pl["targets"] | pl["orphans"]), set())
            self.assertEqual(pl["targets"], {50, 55, 56, 57})  # the daemon above it is fair game

    def test_it_says_when_the_hosting_session_is_itself_old(self):
        self.assertIn("Note: the session running this is on old build 2.1.0", self.main("-n"))


class Orphans(FakeMac):
    def test_orphaned_mcp_servers(self):
        with open(os.path.join(self.home, ".claude.json"), "w") as fh:
            json.dump({"mcpServers": {"tools": {"command": "/opt/tools/serve", "args": ["--stdio"]}},
                       "projects": {"/w": {"mcpServers": {"x": {"command": "npx", "args": ["-y", "x"]}}}}}, fh)
        self.spawn(60, 1, "node /x/.bin/mcp-server-playwright", NODE)  # argv[1] names an MCP server
        self.spawn(61, 60, "/x/chrome-headless-shell --remote-debugging-pipe", ("4009", "/x/chrome-headless-shell"))
        self.spawn(62, 1, "/usr/local/bin/github-mcp-server stdio", ("4010", "/usr/local/bin/github-mcp-server"))
        self.spawn(63, 1, "/opt/tools/serve --stdio", ("4011", "/opt/tools/serve"))  # configured in ~/.claude.json
        self.spawn(64, 1, "python3 -m http.server", PYTHON)  # not an MCP server
        self.spawn(65, 1, "/usr/local/bin/docs-mcp", ("4012", "/usr/local/bin/docs-mcp"))
        self.launchd.add(65)  # a LaunchAgent
        self.spawn(66, 1, "node /x/.bin/mcp-server-memory", NODE, uid=ME + 1)  # someone else's
        self.spawn(67, TERMINAL, "node /x/.bin/mcp-server-time", NODE)  # still has a parent
        self.spawn(68, 62, "claude -p summarize", self.build("2.2.0"))  # current Claude under an orphan
        self.assertEqual(self.plan()["orphans"], {60, 61, 62, 63})


class Stopping(FakeMac):
    def test_daemons_go_first_and_leftovers_get_sigkill_after_the_grace(self):
        old = self.build("2.1.0")
        self.spawn(70, 1, "claude bg-spare --bg-spare /tmp/c.sock", old)
        self.spawn(71, 1, "claude daemon run --json-path x", old)
        self.spawn(72, 1, "claude bg-pty-host --bg-pty-host /tmp/s.sock", old)
        self.stubborn.add(72)
        self.assertEqual(cp.stop_all(self.plan(), everything=False), 3)
        self.assertEqual(self.terms()[0], 71)
        self.assertEqual(sorted(self.terms()), [70, 71, 72])
        self.assertEqual([pid for pid, sig in self.signals if sig == signal.SIGKILL], [72])
        self.assertGreaterEqual(self.clock.now - 1000.0, 3)
        self.assertEqual(set(self.procs), {TERMINAL, SELF})

    def test_a_reused_pid_is_never_signalled(self):
        self.spawn(73, 1, "claude bg-spare --bg-spare /tmp/c.sock", self.build("2.1.0"))
        pl = self.plan()
        self.procs[73]["cmd"] = "/usr/bin/vim notes.txt"  # 73 exited and its PID went to someone else
        cp.stop({73}, pl)
        self.assertEqual(self.signals, [])

    def test_the_pid_is_checked_again_before_sigkill(self):
        self.spawn(74, 1, "claude bg-spare --bg-spare /tmp/c.sock", self.build("2.1.0"))
        self.stubborn.add(74)

        def reused():  # gone on SIGTERM, and the PID handed straight to another program
            self.procs[74]["cmd"] = "/usr/sbin/cupsd -l"
        self.on_term[74] = reused
        cp.stop({74}, self.plan())
        self.assertEqual(self.signals, [(74, signal.SIGTERM)])

    def test_respawned_spares_are_caught_by_two_rescans_and_no_more(self):
        old = self.build("2.1.0")

        def respawner(pid):  # each spare that dies is replaced, like a daemon refilling its pool
            def respawn():
                self.spawn(pid + 1, 1, f"claude bg-spare --bg-spare /tmp/{pid + 1}.sock", old)
                self.on_term[pid + 1] = respawner(pid + 1)
            return respawn
        self.spawn(75, 1, "claude bg-spare --bg-spare /tmp/75.sock", old)
        self.on_term[75] = respawner(75)
        self.assertEqual(cp.stop_all(self.plan(), everything=False), 3)
        self.assertEqual(self.terms(), [75, 76, 77])
        self.assertIn(78, self.procs)


class Builds(FakeMac):
    def test_old_builds_never_include_the_current_one_under_any_name(self):
        os.link(self.path("2.2.0"), os.path.join(self.versions, "2.2.0.bak"))
        os.symlink(self.path("2.1.0"), os.path.join(self.versions, "latest"))
        os.mkdir(os.path.join(self.versions, ".staging"))
        cur_ino = os.stat(self.path("2.2.0")).st_ino
        self.assertEqual([v for v, _, _ in cp.old_builds("2.2.0", cur_ino)], ["2.0.5", "2.1.0"])

    def test_refuses_to_guess_the_current_build(self):
        os.remove(self.link)
        os.symlink("/usr/local/lib/claude/claude", self.link)
        with self.assertRaises(SystemExit) as caught:
            self.plan()
        self.assertIn("refusing to guess the current build", str(caught.exception.code))

    def test_a_build_still_in_use_is_deleted_and_the_holder_named(self):
        self.holders[self.path("2.1.0")] = "4242\n"
        out = self.main()
        self.assertEqual(sorted(os.listdir(self.versions)), ["2.2.0"])
        self.assertTrue(os.path.exists(self.app))
        self.assertIn("2.1.0 was still mapped by pid 4242; its disk space returns when that exits", out)


class Runs(FakeMac):
    def test_a_dry_run_changes_nothing(self):
        self.spawn(80, 1, "claude bg-spare --bg-spare /tmp/c.sock", self.build("2.1.0"))
        out = self.main("-n")
        self.assertEqual(self.signals, [])
        self.assertIn(80, self.procs)
        self.assertEqual(sorted(os.listdir(self.versions)), ["2.0.5", "2.1.0", "2.2.0"])
        self.assertIn("Old builds on disk: 2.0.5 (1 KB), 2.1.0 (1 KB)", out)
        self.assertIn("Dry run: run again without -n to apply.", out)

    def test_a_real_run(self):
        old = self.build("2.1.0")
        self.spawn(81, 1, "claude bg-pty-host --bg-pty-host /tmp/s.sock", old)
        self.spawn(82, 81, "claude bg-spare --bg-spare /tmp/c.sock", old)
        self.spawn(83, 82, "node /x/.bin/mcp-server-github", NODE)
        self.spawn(84, 1, "claude daemon run --json-path x", self.build("2.2.0"))
        out = self.main()
        self.assertEqual(sorted(self.terms()), [81, 82, 83])
        self.assertIn(84, self.procs)
        self.assertEqual(sorted(os.listdir(self.versions)), ["2.2.0"])
        self.assertIn("Done: stopped 3 processes", out)
        self.assertIn("deleted 2 old builds", out)
        self.assertIn("reopen one with `claude --resume`", out)

    def test_nothing_to_prune(self):
        for v in ("2.0.5", "2.1.0"):
            os.remove(self.path(v))
        self.assertIn("Nothing to prune.", self.main())
        self.assertEqual(self.signals, [])

    def test_memory_is_physical_footprint_from_top(self):
        self.outputs["top"] = "Processes: 4 total\nPID    MEM\n101    373M+\n102    1.5G\n103    512K-\n104    0B\n"
        self.assertEqual(cp.footprints(), {101: 373, 102: 1536, 103: 0.5, 104: 0})
        self.assertIn(("top", "-l", "1", "-s", "0", "-n", "5000", "-stats", "pid,mem"), self.commands)

    def test_refuses_to_run_as_root(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(os, "geteuid", return_value=0):
            with self.assertRaises(SystemExit) as caught:
                cp.main(["-n"])
        self.assertIn("not root", str(caught.exception.code))

    def test_refuses_to_run_off_macos(self):
        with mock.patch.object(sys, "platform", "linux"):
            with self.assertRaises(SystemExit) as caught:
                cp.main(["-n"])
        self.assertIn("macOS only", str(caught.exception.code))

    def test_version_matches_pyproject(self):
        with open(os.path.join(ROOT, "pyproject.toml")) as fh:
            self.assertIn(f'version = "{cp.__version__}"', fh.read())


if __name__ == "__main__":
    unittest.main()
