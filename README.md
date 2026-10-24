# claude-prune

[![CI](https://github.com/dheerajjha/claude-prune/actions/workflows/ci.yml/badge.svg)](https://github.com/dheerajjha/claude-prune/actions/workflows/ci.yml)

Every Claude Code update leaves the previous build running. `claude-prune` stops those
processes and everything they started, cleans up MCP servers whose session is already gone,
and deletes the old builds from disk. It never touches the session you run it from.

```sh
claude update && claude-prune
```

## Why

Old builds keep running after an update:

- When a background session's daemon goes away, its `bg-pty-host` → `bg-spare` pair is left
  orphaned.
- Old `claude agents` windows stay open.
- Each of these keeps its MCP servers running, because every Claude process starts all of your
  configured stdio servers.
- `~/.local/share/claude/versions` keeps every old binary, at about 250 MB each.

Nothing ever cleans any of this up. On one 32 GB M1 Pro, over a month of updates:

- At the peak, 73 Claude processes were using 8.6 GB of RAM.
- One to two days after an update, five orphaned session trees from the two previous builds
  were still holding about 3.1 GB.
- About 1 GB of old binaries sat on disk.
- One build had already been deleted from disk but was still held in RAM by a 3-day-old
  orphan.

Cleaning up by hand goes wrong in surprising ways (see [How it decides](#how-it-decides)).
That's why this is a tool and not a one-liner.

## Install

You need:

- macOS
- Python 3.9 or newer (the `python3` that comes with the Xcode command line tools is fine)
- Claude Code installed with the native installer (`~/.local/bin/claude`)

It has no other dependencies.

```sh
brew install dheerajjha/tap/claude-prune
```

```sh
uv tool install git+https://github.com/dheerajjha/claude-prune
# or
pipx install git+https://github.com/dheerajjha/claude-prune
```

Or put the single file in `~/.local/bin`:

```sh
curl -fsSL https://raw.githubusercontent.com/dheerajjha/claude-prune/main/install.sh | sh
```

## Usage

```sh
claude-prune -n      # dry run: show what it would do, change nothing
claude-prune         # stop old-build processes and orphaned MCP servers, delete old builds
claude-prune --all   # also stop the current build's daemon, idle spares and other sessions
claude-prune --sim   # also shut down booted iOS simulators (not while a build or test is running)
```

Start with `-n`. After that, `claude update && claude-prune` is a good habit.

## What a run looks like

This is a real run against a staged scenario. `2.1.0` is a stand-in binary placed in
`versions/`, and `fake-mcp` is a script that runs `sleep`. Both were left running with their
parent gone, so launchd adopted them:

```
claude-prune · current build 2.1.310
memory  78M free · 2508M compressed · 218.00M swap · pressure 81% free

Claude Code on old builds: 2 processes, 2 MB
  2.1.0      session             up 0m         2 MB   pid 9543, 2 procs  (sleep)
Orphaned MCP servers: 2 processes, 2 MB
  fake-mcp                       up 0m         2 MB   pid 9544  (+ sleep)
Old builds on disk: 2.1.0 (33 KB)
Left running: current build 2.1.310, 49 processes, 3.9 GB (--all stops these too)

Done: stopped 4 processes (~5 MB of RAM); deleted 1 old build (33 KB of disk).
  Stopped sessions keep their history: reopen one with `claude --resume`.
memory  123M free · 2508M compressed · 218.00M swap · pressure 81% free
```

On a working machine, `claude-prune --all -n` shows how much one build's process trees add up
to. Two MCP server names below are replaced with placeholders:

```
claude-prune · current build 2.1.310   [dry run: nothing will change]
memory  238M free · 3386M compressed · 218.00M swap · pressure 79% free

Claude Code, every build except this session: 33 processes, 3.0 GB
  2.1.310    daemon              up 14h50m   2.8 GB   pid 92204, 31 procs  (<mcp-a> x4, background session x5, pty-host x7, session x2, <mcp-b> x6, xcodebuildmcp x6)
  2.1.310    agents window       up 24m      126 MB   pid 8622, 1 proc
  2.1.310    agents window       up 45m      115 MB   pid 98031, 1 proc
Orphaned MCP servers: none
Old builds on disk: none

Dry run: run again without -n to apply.
```

On macOS the "free" number always drifts toward zero, because idle RAM is used as file cache.
Watch "pressure N% free" instead.

## What it never touches

- **The current build**, unless you pass `--all`. It also leaves alone anything that shares the
  current build's inode, such as the `ClaudeCode.app` hardlink to it.
- **The session you run it from.** That covers the nearest Claude process above it, that
  session's `bg-pty-host`, and that session's own MCP servers.
- **Anything running under a current-build process.** An old `claude agents` window can be the
  parent of the current daemon. Stopping "it and everything under it" would end every live
  session.
- **launchd jobs (LaunchAgents)** and everything they start. They are meant to be running,
  and with KeepAlive they come straight back anyway.
- **The Claude desktop app** (`/Applications/Claude.app`).
- **Other users' processes.** It never needs root, and it refuses to run as root.
- **Your conversations.** History stays on disk, so you can reopen any stopped session with
  `claude --resume`.

## How it decides

- **It recognizes Claude processes by the binary they run, not by their command line.** For
  each candidate it compares the inode of the mapped executable (`lsof`, `txt`) with the inode
  of the build that `~/.local/bin/claude` points to. Matching on the command line catches far
  too much: `grep`'s own arguments, a shell running a script that mentions a version number,
  temp paths like `/tmp/claude-501/...`. All of them "contain claude".
- **The current build is whatever `~/.local/bin/claude` points to** inside
  `~/.local/share/claude/versions`. If the link points anywhere else, it stops rather than
  guess.
- **It stops daemons first.** A daemon keeps a pool of pre-warmed `bg-spare` processes and
  refills it within seconds.
  - Each process gets SIGTERM first.
  - Anything still running 3 seconds later gets SIGKILL.
  - The whole scan then runs up to twice more, to catch anything that respawned.
  - Right before each signal, it checks the PID against a fresh process list, because PIDs
    are reused quickly on a busy Mac.
- **An orphaned MCP server** is one of your processes that meets all of these:
  - Its parent is launchd (ppid 1).
  - Its program name contains `mcp`, or matches a stdio server command in `~/.claude.json`.
  - It is not a launchd job.
- **It deletes a build even if a process still uses it.** That is safe: the data stays until
  the last process using the file exits, and it tells you which PID that is.
- **It reports memory as physical footprint** (`top -stats mem`, what Activity Monitor calls
  Memory), which counts compressed pages. RSS once showed 0 MB for a process that held
  373 MB in the compressor.

## FAQ

**Is it safe to run from inside a Claude Code session?** Yes. It finds the session it is
running in and leaves that session, its pty host and its MCP servers alone. If that session is
itself on an old build, it tells you. Exit it and start a new one to finish the job.

**Will I lose conversation history?** No. Stopping a process doesn't touch your saved
conversations. Reopen any session with `claude --resume`.

**Does it work on Linux, or with an npm install of Claude Code?** Not yet. On Linux it refuses
to run rather than guess. [#1](https://github.com/dheerajjha/claude-prune/issues/1) tracks
Linux support. It only knows the native installer's layout.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The unit tests can't touch real processes. They run against a fake machine, and every
signal and external command goes through a stub. The end-to-end test does start and stop real
processes, using a fake Claude Code install in a temporary home. It only runs on macOS with
`CLAUDE_PRUNE_E2E=1`, and CI runs it on `macos-latest`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. Not affiliated with Anthropic.
