#!/usr/bin/env bash
# Install or update arta as a per-user LaunchAgent on macOS.
#
#   scripts/install_macos.sh            install / reinstall and (re)start
#   scripts/install_macos.sh --remove   stop and remove the LaunchAgent
#
# It does not create or fill the configuration: copy config.example.toml to
# ~/.config/arta/config.toml (or $ARTA_CONFIG) and fill it first. It does not
# install Homebrew packages either; it names what is missing and stops.

set -euo pipefail

LABEL="com.github.dsgnrfhmn.arta"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/arta"
CONFIG="${ARTA_CONFIG:-$HOME/.config/arta/config.toml}"
DOMAIN="gui/$(id -u)"

fail() { printf 'install: %s\n' "$*" >&2; exit 1; }
mode() { stat -f '%Lp' "$1"; }

[ "$(uname -s)" = "Darwin" ] || fail "this installs a macOS LaunchAgent; this is $(uname -s)."

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL (config, data and logs are left in place)"
  exit 0
fi

# Prerequisites: named, not installed behind the user's back.
command -v python3 >/dev/null || fail "python3 is missing (3.11 or newer)."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || fail "python3 is older than 3.11 (tomllib is needed)."
command -v pdftoppm >/dev/null || fail "pdftoppm is missing: brew install poppler"

# Configuration: present and private.
[ -f "$CONFIG" ] || fail "no configuration at $CONFIG. Copy $REPO/config.example.toml there and fill it."
cdir="$(dirname "$CONFIG")"
[ "$(mode "$cdir")" = "700" ] || fail "$cdir must be mode 700: chmod 700 '$cdir'"
[ "$(mode "$CONFIG")" = "600" ] || fail "$CONFIG must be mode 600: chmod 600 '$CONFIG'"

# Python environment inside the clone (venv/ is git-ignored).
[ -x "$REPO/venv/bin/python" ] || python3 -m venv "$REPO/venv"
"$REPO/venv/bin/python" -m pip install --quiet --upgrade pip
"$REPO/venv/bin/python" -m pip install --quiet -r "$REPO/requirements.txt"
"$REPO/venv/bin/python" -m playwright install chromium

# Logs, then the LaunchAgent.
mkdir -p "$LOGS" && chmod 700 "$LOGS"
mkdir -p "$(dirname "$PLIST")"
sed -e "s|@LABEL@|$LABEL|g" -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME|g" \
  "$REPO/deploy/macos/arta.plist.template" > "$PLIST"
plutil -lint "$PLIST" >/dev/null || fail "rendered $PLIST is not a valid plist."

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "started $LABEL"
echo "  log: $LOGS/arta.log"
echo "  check: launchctl print $DOMAIN/$LABEL | grep -E 'state|last exit'"
