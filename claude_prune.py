#!/usr/bin/env python3
"""claude-prune: free the RAM and disk that stale Claude Code leaves behind.

Run it after every Claude Code update, or whenever RAM feels tight.

By default it:
  * stops Claude Code processes still running an OLD build - orphaned
    background sessions, old agents windows, old daemons - together with
    everything they spawned (MCP servers, sourcekit-lsp, caffeinate)
  * stops orphaned MCP servers whose Claude session has already exited
  * deletes old builds from ~/.local/share/claude/versions

It never touches the current build (unless --all), the session or terminal
you run it from, launchd-managed servers (LaunchAgents), or the Claude
desktop app. Conversation history stays on disk, so any stopped session can
be reopened with `claude --resume`.
"""
import argparse
import collections
import json
import os
import re
import signal
import subprocess
import sys
import time

__version__ = "0.1.0"

APP_BINARY = "ClaudeCode.app/Contents/MacOS/claude"
ISSUES = "https://github.com/dheerajjha/claude-prune/issues"
UID = os.getuid() if hasattr(os, "getuid") else None


def configure(home):
    """Point every path at one home directory: ~, or CLAUDE_PRUNE_HOME in tests."""
    global HOME, LINK, VERDIR
    HOME = home
    LINK = os.path.join(home, ".local/bin/claude")
    VERDIR = os.path.realpath(os.path.join(home, ".local/share/claude/versions"))


configure(os.environ.get("CLAUDE_PRUNE_HOME") or os.path.expanduser("~"))


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def grab(pattern, text, default="?"):
    m = re.search(pattern, text)
    return m.group(1) if m else default


def send(pid, sig):
    """Every signal this program sends goes through here."""
    os.kill(pid, sig)


# ------------------------------------------------------------------ processes

def snapshot():
    procs = {}
    for line in run("ps", "-axo", "pid=,ppid=,uid=,etime=,command=").splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
        if m:
            procs[int(m.group(1))] = {"ppid": int(m.group(2)), "uid": int(m.group(3)),
                                      "etime": m.group(4), "cmd": m.group(5)}
    return procs


def children(procs):
    kids = collections.defaultdict(list)
    for pid, p in procs.items():
        kids[p["ppid"]].append(pid)
    return kids


def subtree(root, kids, halt=None):
    """All descendants of root, without walking into processes for which halt() is true."""
    found, stack = set(), [root]
    while stack:
        for k in kids.get(stack.pop(), ()):
            if k not in found and not (halt and halt(k)):
                found.add(k)
                stack.append(k)
    return found


def mapped_binaries(pids):
    """pid -> [(inode, path)] of the executables each process has mapped (lsof txt)."""
    maps, pid, inode = {}, None, None
    if not pids:
        return maps
    out = run("lsof", "-a", "-d", "txt", "-Fpfin", "-p", ",".join(str(p) for p in sorted(pids)))
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid, inode = int(val), None
            maps.setdefault(pid, [])
        elif tag == "f":
            inode = None
        elif tag == "i":
            inode = val
        elif tag == "n" and pid is not None:
            maps[pid].append((inode, val))
    return maps


def role(cmd):
    if "daemon run" in cmd:
        return "daemon"
    if "bg-pty-host" in cmd:
        return "pty-host"
    if "bg-spare" in cmd:
        return "background session"
    if re.search(r"(^|\s)agents(\s|$)", cmd):
        return "agents window"
    return "session"


def classify(procs, cur, cur_ino):
    """Claude Code processes, identified by the binary they run rather than their argv."""
    candidates = [p for p, i in procs.items() if i["uid"] == UID and "claude" in i["cmd"]]
    found = {}
    for pid, files in mapped_binaries(candidates).items():
        if pid not in procs:
            continue
        ver, current = None, False
        for inode, path in files:
            in_versions = path.startswith(VERDIR + "/")
            if not (in_versions or path.endswith(APP_BINARY)):
                continue
            if inode is None:
                # no inode reported: go by file name, and never call a process stale on a guess
                if not in_versions or os.path.basename(path) == cur:
                    ver, current = cur, True
                    break
                ver = os.path.basename(path)
                continue
            if inode == str(cur_ino):
                ver, current = cur, True
                break
            ver = os.path.basename(path) if in_versions else "old app build"
            if ver == cur:
                ver = cur + " (replaced)"
        if ver:
            found[pid] = {"ver": ver, "current": current, "role": role(procs[pid]["cmd"])}
    return found


def protected(procs, kids, claude, self_pid):
    """This program, the shells above it, and the Claude session hosting it (with its own MCP servers)."""
    chain, p = [], self_pid
    while p in procs and p > 1 and p not in chain:
        chain.append(p)
        p = procs[p]["ppid"]
    host = next((x for x in chain[1:] if x in claude and claude[x]["role"] != "daemon"), None)
    if host is None:
        return set(chain), None
    keep = set(chain[:chain.index(host) + 1])
    keep |= subtree(host, kids, halt=lambda k: k in claude)
    parent = procs[host]["ppid"]
    if claude.get(parent, {}).get("role") == "pty-host":
        keep.add(parent)
    return keep, host


def launchd_jobs():
    pids = set()
    for line in run("launchctl", "list").splitlines()[1:]:
        f = line.split()
        if f and f[0].isdigit():
            pids.add(int(f[0]))
    return pids


def configured_mcp_commands():
    try:
        with open(os.path.join(HOME, ".claude.json")) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return set()
    scopes = [cfg.get("mcpServers") or {}]
    scopes += [(p or {}).get("mcpServers") or {} for p in (cfg.get("projects") or {}).values()]
    return {s["command"] for scope in scopes for s in scope.values()
            if isinstance(s, dict) and os.path.isabs(s.get("command") or "")}


def looks_like_mcp(cmd, configured):
    return any(h in configured or "mcp" in os.path.basename(h).lower() for h in cmd.split()[:2])


def orphaned_mcp_servers(procs, kids, claude, targets, halt):
    """Your MCP servers that launchd adopted (ppid 1) after their session exited, with what they started."""
    managed = set()
    for job in launchd_jobs():  # KeepAlive LaunchAgents are meant to run, and respawn at once anyway
        managed |= {job} | subtree(job, kids)
    configured = configured_mcp_commands()
    orphans = set()
    for p, i in procs.items():
        if (i["uid"] == UID and i["ppid"] == 1 and p not in managed and not halt(p)
                and p not in claude and p not in targets and looks_like_mcp(i["cmd"], configured)):
            orphans |= {p} | subtree(p, kids, halt)
    return orphans - targets


def plan(everything, self_pid=None):
    real = os.path.realpath(LINK)
    if os.path.dirname(real) != VERDIR:
        sys.exit(f"claude-prune: {LINK} -> {real} is not inside {VERDIR}; refusing to guess the current build")
    cur, cur_ino = os.path.basename(real), os.stat(real).st_ino
    self_pid = self_pid or os.getpid()
    procs = snapshot()
    kids = children(procs)
    claude = classify(procs, cur, cur_ino)
    keep, host = protected(procs, kids, claude, self_pid)

    def halt(k):
        return k in keep or (not everything and k in claude and claude[k]["current"])

    targets = set()
    for p, c in claude.items():
        if p not in keep and (everything or not c["current"]):
            targets.add(p)
            targets |= subtree(p, kids, halt)
    return {"cur": cur, "cur_ino": cur_ino, "self_pid": self_pid, "procs": procs, "kids": kids,
            "claude": claude, "keep": keep, "host": host, "targets": targets,
            "orphans": orphaned_mcp_servers(procs, kids, claude, targets, halt)}


def old_builds(cur, cur_ino):
    """Builds in the versions directory other than the current one, and never another name for it."""
    old = []
    for v in sorted(os.listdir(VERDIR)):
        path = os.path.join(VERDIR, v)
        if os.path.isfile(path) and not os.path.islink(path):
            st = os.stat(path)
            if v != cur and st.st_ino != cur_ino:
                old.append((v, path, st.st_size / 2**20))
    return old


# --------------------------------------------------------------------- memory

def to_mb(s):
    m = re.match(r"([\d.]+)([BKMGT]?)", s)
    if not m:
        return 0.0
    return float(m.group(1)) * {"B": 2**-20, "K": 2**-10, "M": 1, "G": 2**10, "T": 2**20, "": 2**-20}[m.group(2)]


def footprints():
    """pid -> MB of physical footprint (what Activity Monitor calls Memory)."""
    fp, body = {}, False
    for line in run("top", "-l", "1", "-s", "0", "-n", "5000", "-stats", "pid,mem").splitlines():
        f = line.split()
        if f[:1] == ["PID"]:
            body = True
        elif body and len(f) >= 2 and f[0].isdigit():
            fp[int(f[0])] = to_mb(f[1])
    return fp


def memory():
    top = run("top", "-l", "1", "-s", "0", "-n", "0")
    return "{} free · {} compressed · {} swap · pressure {}% free".format(
        grab(r"([\d.]+[KMGT]) unused", top), grab(r"([\d.]+[KMGT]) compressor", top),
        grab(r"used = ([\d.]+[KMGT])", run("sysctl", "-n", "vm.swapusage")),
        grab(r"free percentage: (\d+)%", run("memory_pressure")))


# --------------------------------------------------------------------- output

def size(mb):
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB" if mb >= 1 else f"{mb * 1024:.0f} KB"


def age(etime):
    days, _, clock = etime.rpartition("-")
    parts = [int(x) for x in clock.split(":")]
    hours, mins = (parts[0], parts[1]) if len(parts) == 3 else (0, parts[0])
    if days:
        return f"{int(days)}d{hours}h"
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def short_name(cmd):
    words = cmd.split()
    if not words:
        return "?"
    exe = os.path.basename(words[0])
    if exe in ("node", "bun", "deno", "sh", "bash", "zsh", "ruby", "perl") or exe.lower().startswith("python"):
        script = next((w for w in words[1:] if not w.startswith("-")), "")
        if script:
            return os.path.basename(script)
    return exe


def by_root(pids, procs):
    """Group pids into trees keyed by their top-most member."""
    trees = collections.defaultdict(list)
    for p in pids:
        root = p
        while procs[root]["ppid"] in pids:
            root = procs[root]["ppid"]
        trees[root].append(p)
    return trees


def print_trees(pl, fp):
    procs, claude = pl["procs"], pl["claude"]
    rows = []
    for root, members in by_root(pl["targets"], procs).items():
        # Label a tree by its top Claude process, a pty-host by the session it hosts;
        # set order would let a spare's label stand for its whole daemon.
        heads = [m for m in [root] + sorted(members) if m in claude and claude[m]["role"] != "pty-host"]
        main = heads[0] if heads else root
        info = claude.get(main, {"ver": "-", "role": short_name(procs[main]["cmd"])})
        extras = collections.Counter(claude[m]["role"] if m in claude else short_name(procs[m]["cmd"])
                                     for m in members if m != main)
        extra = ", ".join(n if c == 1 else f"{n} x{c}" for n, c in sorted(extras.items()))
        mb = sum(fp.get(m, 0) for m in members)
        rows.append((mb, f"  {info['ver']:<10} {info['role']:<19} up {age(procs[root]['etime']):<7} "
                         f"{size(mb):>7}   pid {root}, {len(members)} proc{'s' if len(members) > 1 else ''}"
                         + (f"  ({extra})" if extra else "")))
    for _, row in sorted(rows, key=lambda r: -r[0]):
        print(row)


def print_orphans(pl, fp):
    procs = pl["procs"]
    trees = by_root(pl["orphans"], procs)
    for root, members in sorted(trees.items(), key=lambda t: -sum(fp.get(m, 0) for m in t[1])):
        kids = collections.Counter(short_name(procs[m]["cmd"]) for m in members if m != root)
        extra = ", ".join(n if c == 1 else f"{n} x{c}" for n, c in sorted(kids.items()))
        mb = sum(fp.get(m, 0) for m in members)
        print(f"  {short_name(procs[root]['cmd'])[:30]:<30} up {age(procs[root]['etime']):<7} {size(mb):>7}   pid {root}"
              + (f"  (+ {extra})" if extra else ""))


# ----------------------------------------------------------------------- stop

def alive(pid):
    try:
        send(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def unchanged(pid, before, now):
    """Still the process the plan saw, not a newer one that was handed its PID."""
    p = now.get(pid)
    return p is not None and p["uid"] == UID and p["cmd"] == before[pid]["cmd"]


def stop(pids, pl):
    """SIGTERM (daemons first, so nothing respawns), then SIGKILL anything still there after 3s.

    Each PID is checked against a fresh process list right before it is
    signalled, both times: PIDs wrap quickly on a busy Mac.
    """
    procs = pl["procs"]
    now = snapshot()
    live = [p for p in pids if unchanged(p, procs, now)]
    live.sort(key=lambda p: pl["claude"].get(p, {}).get("role") != "daemon")
    for p in live:
        try:
            send(p, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 3
    while time.time() < deadline and any(alive(p) for p in live):
        time.sleep(0.2)
    leftovers = [p for p in live if alive(p)]
    if leftovers:
        now = snapshot()
        for p in leftovers:
            if unchanged(p, procs, now):
                try:
                    send(p, signal.SIGKILL)
                except OSError:
                    pass
    return len(live)


def stop_all(pl, everything):
    """Stop the plan's targets, then re-scan up to twice for anything respawned meanwhile."""
    pids = pl["targets"] | pl["orphans"]
    stopped = stop(pids, pl) if pids else 0
    for _ in range(2):  # a daemon refills its pool of spares within seconds
        again = plan(everything, pl["self_pid"])
        extra = again["targets"] | again["orphans"]
        if not extra:
            break
        stopped += stop(extra, again)
    return stopped


# ----------------------------------------------------------------------- main

def parse_args(argv):
    ap = argparse.ArgumentParser(prog="claude-prune", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--dry-run", action="store_true", help="show what would happen, change nothing")
    ap.add_argument("--all", action="store_true",
                    help="also stop current-build Claude Code (daemon, idle spares, other sessions) - "
                         "everything except the session running this")
    ap.add_argument("--sim", action="store_true",
                    help="also shut down booted iOS simulators (skipped while xcodebuild/xctest/simctl runs)")
    ap.add_argument("-V", "--version", action="version", version=f"claude-prune {__version__}")
    return ap.parse_args(argv)


def prune(args):
    if sys.platform != "darwin":
        sys.exit(f"claude-prune: macOS only for now; Linux support is tracked in {ISSUES}/1")
    if os.geteuid() == 0:
        sys.exit("claude-prune: run it as yourself, not root; it only ever touches your own processes")
    if not os.path.isdir(VERDIR) or not os.path.exists(LINK):
        sys.exit("claude-prune: no native Claude Code install found at ~/.local/bin/claude")

    pl = plan(args.all)
    cur, claude = pl["cur"], pl["claude"]
    targets, orphans = pl["targets"], pl["orphans"]
    print(f"claude-prune · current build {cur}" + ("   [dry run: nothing will change]" if args.dry_run else ""))
    print("memory  " + memory())
    print()

    fp = footprints()
    heading = "Claude Code, every build except this session" if args.all else "Claude Code on old builds"
    if targets:
        print(f"{heading}: {len(targets)} processes, {size(sum(fp.get(p, 0) for p in targets))}")
        print_trees(pl, fp)
    else:
        print(f"{heading}: none")

    if orphans:
        print(f"Orphaned MCP servers: {len(orphans)} processes, {size(sum(fp.get(p, 0) for p in orphans))}")
        print_orphans(pl, fp)
    else:
        print("Orphaned MCP servers: none")

    old = old_builds(cur, pl["cur_ino"])
    print("Old builds on disk: " + (", ".join(f"{v} ({size(mb)})" for v, _, mb in old) if old else "none"))

    booted, busy = [], []
    if args.sim:
        booted = re.findall(r"\(([0-9A-F-]{36})\) \(Booted\)", run("xcrun", "simctl", "list", "devices", "booted"))
        busy = [n for n in ("xcodebuild", "xctest", "simctl", "XCBBuildService") if run("pgrep", "-x", n).strip()]
        if not booted:
            print("Simulators: none booted")
        elif busy:
            print(f"Simulators: {len(booted)} booted, left alone while {', '.join(busy)} is running")
        else:
            print(f"Simulators: {len(booted)} booted, will shut down")
    shut_sims = bool(booted) and not busy

    if not args.all:
        current = [p for p, c in claude.items() if c["current"]]
        running = set(current)
        for p in current:
            running |= subtree(p, pl["kids"])
        running -= targets
        if current:
            print(f"Left running: current build {cur}, {len(running)} processes, "
                  f"{size(sum(fp.get(p, 0) for p in running))} (--all stops these too)")
    host = pl["host"]
    if host and not claude[host]["current"]:
        print(f"Note: the session running this is on old build {claude[host]['ver']}; "
              "exit it and start a new one to finish the job.")

    if not (targets or orphans or old or shut_sims):
        print("\nNothing to prune.")
        return
    if args.dry_run:
        print("\nDry run: run again without -n to apply.")
        return

    freed = sum(fp.get(p, 0) for p in targets | orphans)
    had_sessions = any(claude[p]["role"] not in ("daemon", "pty-host") for p in targets if p in claude)
    stopped = stop_all(pl, args.all)

    removed = []
    for v, path, mb in old:
        holders = run("lsof", "-t", path).split()
        try:
            os.remove(path)
            removed.append((v, mb, holders))
        except OSError as e:
            print(f"  could not delete {v}: {e}")

    if shut_sims:
        run("xcrun", "simctl", "shutdown", "all")

    time.sleep(2)
    done = []
    if stopped:
        done.append(f"stopped {stopped} processes (~{size(freed)} of RAM)")
    if removed:
        done.append(f"deleted {len(removed)} old build{'s' if len(removed) > 1 else ''} "
                    f"({size(sum(mb for _, mb, _ in removed))} of disk)")
    if shut_sims:
        done.append(f"shut down {len(booted)} simulator{'s' if len(booted) > 1 else ''}")
    print("\nDone: " + ("; ".join(done) if done else "nothing was left to stop") + ".")
    for v, _, holders in removed:
        if holders:
            print(f"  {v} was still mapped by pid {', '.join(holders)}; its disk space returns when that exits")
    if had_sessions:
        print("  Stopped sessions keep their history: reopen one with `claude --resume`.")
    print("memory  " + memory())


def main(argv=None):
    args = parse_args(argv)
    try:
        prune(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
