#!/usr/bin/env bash
# Install (or update) Relay on a Mac in one go:
#
#   curl -fsSL https://raw.githubusercontent.com/Dhruba531/AutoresearchHarnessAgent/claude/festive-fermi-gb3xr7/mac-agent/install.sh | bash
#
# It gets the code, sets up Python, asks for your API key once (kept in
# ~/.relay/env, chmod 600, never printed), checks permissions, starts Relay at
# login, shares it over Tailscale if you have it, and prints a pairing code.
# Run it again any time to update.
#
# Options (environment variables):
#   RELAY_DIR=~/Relay            where the code lives
#   RELAY_BRANCH=…               which branch to install
#   ANTHROPIC_API_KEY / DEEPSEEK_API_KEY   skip the key prompt
set -euo pipefail

REPO="https://github.com/Dhruba531/AutoresearchHarnessAgent.git"
BRANCH="${RELAY_BRANCH:-claude/festive-fermi-gb3xr7}"
DIR="${RELAY_DIR:-$HOME/Relay}"
HOME_DIR="${RELAY_HOME:-$HOME/.relay}"
PORT="${RELAY_PORT:-8765}"

say() { printf '\033[1m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Everything runs inside main(), so a half-downloaded script runs nothing and no
# command can swallow the rest of it from stdin under `curl | bash`.
main() {
  [ "$(uname -s)" = "Darwin" ] || die "Relay runs on macOS. (On other systems use: python -m macagent serve --fake-host)"
  command -v git >/dev/null || die "git is missing. Run: xcode-select --install"

  # A Python 3.10+; Homebrew's first, then the system one.
  PY=""
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$c"; break; fi
  done
  [ -n "$PY" ] || die "Python 3.10 or newer is needed. Install it with: brew install python"

  # 1. The code: use this checkout if the script is run from one, else clone or update.
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [ -n "$SRC" ] && [ -f "$SRC/macagent/__main__.py" ]; then
    APP="$SRC"
    say "Using this checkout: $APP"
  else
    if [ -d "$DIR/.git" ]; then
      say "Updating $DIR"
      git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q "$BRANCH" && git -C "$DIR" pull -q --ff-only origin "$BRANCH"
    else
      say "Downloading Relay into $DIR"
      git clone -q -b "$BRANCH" "$REPO" "$DIR"
    fi
    APP="$DIR/mac-agent"
  fi
  cd "$APP"

  # 2. Python environment.
  say "Installing Python packages"
  [ -x .venv/bin/python ] || "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  VPY="$APP/.venv/bin/python"

  # 3. API key, asked once. Read from the terminal so it works under `curl | bash`.
  mkdir -p "$HOME_DIR" && chmod 700 "$HOME_DIR"
  ENV_FILE="$HOME_DIR/env"
  touch "$ENV_FILE" && chmod 600 "$ENV_FILE"
  put() {  # put KEY VALUE: replace or append, without echoing the value
    grep -v "^$1=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE" && chmod 600 "$ENV_FILE"
  }
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then put ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"; fi
  if [ -n "${DEEPSEEK_API_KEY:-}" ]; then put DEEPSEEK_API_KEY "$DEEPSEEK_API_KEY"; grep -q '^RELAY_PROVIDER=' "$ENV_FILE" || put RELAY_PROVIDER deepseek; fi
  if ! grep -qE '^(ANTHROPIC|DEEPSEEK|OPENAI)_API_KEY=.+' "$ENV_FILE"; then
    (: < /dev/tty) 2>/dev/null || die "No API key and no terminal to ask in. Re-run with ANTHROPIC_API_KEY=sk-ant-… in front of the command."
    printf 'Paste your Anthropic API key (sk-ant-…), or a DeepSeek key (sk-…). It is not shown: '
    read -rs KEY < /dev/tty || die "Could not read the key."; echo
    [ -n "$KEY" ] || die "No key entered."
    case "$KEY" in
      sk-ant-*) put ANTHROPIC_API_KEY "$KEY" ;;
      *) put DEEPSEEK_API_KEY "$KEY"; put RELAY_PROVIDER deepseek
         echo "  Saved as a DeepSeek key. (DeepSeek can't see the screen; Relay uses native Mac tools with it.)" ;;
    esac
    unset KEY
  fi
  say "Key saved in $ENV_FILE (owner-only)"

  # 4. Permissions check (macOS asks for these once).
  say "Checking macOS permissions"
  RELAY_HOME="$HOME_DIR" "$VPY" -m macagent doctor || true
  REAL_PY="$("$VPY" -c 'import os, sys; print(os.path.realpath(sys.executable))')"

  # 5. Start at login, keep running.
  say "Starting Relay (and at every login)"
  RELAY_HOME="$HOME_DIR" "$VPY" -m macagent install-launchagent >/dev/null

  # 6. Reach it from your phone, privately, over Tailscale.
  TS="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
  if [ -x "$TS" ] && "$TS" status >/dev/null 2>&1; then
    "$TS" serve --bg "$PORT" >/dev/null 2>&1 && say "Shared on your tailnet (tailscale serve $PORT)" || true
  else
    echo "  Tailscale not found or not signed in: the phone works on the same Wi-Fi only."
    echo "  For anywhere access, install Tailscale on the Mac and phone, then run this script again."
  fi

  # 7. Wait for it, then pair the phone.
  for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 1; done
  curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 || die "Relay did not start. See $HOME_DIR/relay.log"
  say "Relay is running"
  RELAY_HOME="$HOME_DIR" "$VPY" -m macagent pair

  cat <<EOF

Done. If the permission check above showed ✗, open System Settings → Privacy & Security and add
  $REAL_PY
to Screen Recording and Accessibility (that is what the background service runs), then run:
  launchctl kickstart -k gui/$(id -u)/dev.relay.agent
Logs: $HOME_DIR/relay.log   ·   Update later: run the same one-line command again.
EOF
}

main "$@"
