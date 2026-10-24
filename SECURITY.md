# Security policy

## Reporting a vulnerability

Please report it privately through GitHub: open this repository's **Security** tab and choose **Report a vulnerability**. Please don't open a public issue for it.

You'll hear back within a few days. The fix ships in a new release, and the advisory credits you unless you'd rather it didn't.

## Supported versions

Security fixes go into the latest release only.

## Scope

claude-prune sends signals to processes and deletes files from `~/.local/share/claude/versions`. Please report anything that makes it:

- signal a process it should leave alone, such as another user's process, the session running it, or a current-build process without `--all`
- delete or change any file outside that directory
