#!/bin/sh
# Install claude-prune, a single Python file, into ~/.local/bin (or $CLAUDE_PRUNE_BIN).
#   curl -fsSL https://raw.githubusercontent.com/dheerajjha/claude-prune/main/install.sh | sh
# Set CLAUDE_PRUNE_VERSION=0.1.0 to pin a release instead of taking the latest.
set -eu

repo="dheerajjha/claude-prune"
bin_dir="${CLAUDE_PRUNE_BIN:-$HOME/.local/bin}"
if [ -n "${CLAUDE_PRUNE_VERSION:-}" ]; then
  url="https://github.com/$repo/releases/download/v$CLAUDE_PRUNE_VERSION/claude_prune.py"
else
  url="https://github.com/$repo/releases/latest/download/claude_prune.py"
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "claude-prune needs python3 (3.9 or newer). On macOS: xcode-select --install" >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  echo "claude-prune needs Python 3.9 or newer; python3 here is $(python3 -V 2>&1)" >&2
  exit 1
fi

mkdir -p "$bin_dir"
tmp="$(mktemp "$bin_dir/.claude-prune.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
curl -fsSL "$url" -o "$tmp"
# A truncated or wrong download won't compile; check before it replaces anything.
python3 -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$tmp"
chmod 755 "$tmp"
mv "$tmp" "$bin_dir/claude-prune"
trap - EXIT

echo "Installed $("$bin_dir/claude-prune" --version) to $bin_dir/claude-prune"
case ":$PATH:" in
  *":$bin_dir:"*) echo "Try it with: claude-prune -n" ;;
  *) echo "$bin_dir is not on your PATH; add it, or run $bin_dir/claude-prune -n" ;;
esac
