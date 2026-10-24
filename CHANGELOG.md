# Changelog

## 0.1.0 — 2026-10-24

First release.

- Stops Claude Code processes still running an old build, together with everything they started. That covers orphaned background sessions, old `claude agents` windows and old daemons. Daemons go first, so their spare pools can't refill, and the scan repeats to catch respawns.
- Stops orphaned MCP servers whose Claude session has exited, leaving launchd jobs alone.
- Deletes old builds from `~/.local/share/claude/versions`. It never deletes the current build or anything sharing its inode.
- Never touches the session it runs from, or that session's pty host and MCP servers.
- `-n` dry run, `--all` to include the current build's processes, `--sim` for booted iOS simulators.
- macOS only; refuses to run elsewhere, and as root.
