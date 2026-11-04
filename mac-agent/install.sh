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
#   ANTHROPIC_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY   skip the key prompt
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
  if [ -z "$PY" ]; then
    # No Homebrew needed: uv installs a private Python 3.12 into ~/.local (nothing system-wide).
    say "Python 3.10+ not found; installing Python 3.12 with uv"
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null </dev/null
    command -v uv >/dev/null || die "Could not install uv. Install Python 3.10+ from python.org, then run this again."
    uv python install 3.12 </dev/null >/dev/null
    PY="$(uv python find 3.12)"
  fi

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
  if [ -n "${OPENROUTER_API_KEY:-}" ]; then put OPENROUTER_API_KEY "$OPENROUTER_API_KEY"; put RELAY_PROVIDER openrouter; fi
  if [ -n "${OPENAI_API_KEY:-}" ]; then put OPENAI_API_KEY "$OPENAI_API_KEY"; put RELAY_PROVIDER openai; fi
  if [ -n "${DEEPSEEK_API_KEY:-}" ]; then put DEEPSEEK_API_KEY "$DEEPSEEK_API_KEY"; grep -q '^RELAY_PROVIDER=' "$ENV_FILE" || put RELAY_PROVIDER deepseek; fi
  # an OpenRouter key that an older version of this script filed as DeepSeek
  OLD="$(grep '^DEEPSEEK_API_KEY=sk-or-' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
  if [ -n "$OLD" ]; then
    grep -v '^DEEPSEEK_API_KEY=' "$ENV_FILE" > "$ENV_FILE.tmp" || true; mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
    put OPENROUTER_API_KEY "$OLD"; put RELAY_PROVIDER openrouter; echo "  Moved your OpenRouter key to OPENROUTER_API_KEY."
  fi
  unset OLD
  if ! grep -qE '^(ANTHROPIC|DEEPSEEK|OPENAI|OPENROUTER)_API_KEY=.+' "$ENV_FILE"; then
    (: < /dev/tty) 2>/dev/null || die "No API key and no terminal to ask in. Re-run with ANTHROPIC_API_KEY=sk-ant-… in front of the command."
    printf 'Paste your API key: Anthropic (sk-ant-…), OpenRouter (sk-or-…), OpenAI or DeepSeek. It is not shown: '
    read -rs KEY < /dev/tty || die "Could not read the key."; echo
    [ -n "$KEY" ] || die "No key entered."
    case "$KEY" in
      sk-ant-*) put ANTHROPIC_API_KEY "$KEY"; put RELAY_PROVIDER anthropic ;;
      sk-or-*) put OPENROUTER_API_KEY "$KEY"; put RELAY_PROVIDER openrouter
         echo "  Saved as an OpenRouter key: Claude Opus 5.5 for tasks, Haiku 5.5 for chat. Through OpenRouter Relay"
         echo "  uses native Mac tools (apps, files, shell, Mail, Calendar), not screen clicking; that needs an Anthropic key." ;;
      *)
        # OpenAI and DeepSeek keys both start with sk-: project keys are OpenAI, 32 hex digits are DeepSeek
        WHO=""
        case "$KEY" in sk-proj-*|sk-svcacct-*|sk-admin-*) WHO=openai ;; esac
        if [ -z "$WHO" ] && printf '%s' "$KEY" | grep -qE '^sk-[0-9a-f]{32}$'; then WHO=deepseek; fi
        if [ -z "$WHO" ]; then
          printf 'Is that an OpenAI or a DeepSeek key? [o/d]: '
          read -r ANS < /dev/tty || ANS=""
          case "$ANS" in [oO]*) WHO=openai ;; [dD]*) WHO=deepseek ;; *) die "Run it again and answer o or d." ;; esac
        fi
        if [ "$WHO" = openai ]; then put OPENAI_API_KEY "$KEY"; put RELAY_PROVIDER openai; else put DEEPSEEK_API_KEY "$KEY"; put RELAY_PROVIDER deepseek; fi
        echo "  Saved as a$( [ "$WHO" = openai ] && echo "n OpenAI" || echo " DeepSeek") key. Relay uses native Mac tools with it"
        echo "  (apps, files, shell, Mail, Calendar), not screen clicking; that needs an Anthropic key." ;;
    esac
    unset KEY
  fi
  say "Key saved in $ENV_FILE (owner-only)"

  # 4. Permissions check (macOS asks for these once).
  say "Checking macOS permissions"
  RELAY_HOME="$HOME_DIR" "$VPY" -m macagent doctor || true
  REAL_PY="$("$VPY" -c 'import os, sys; print(os.path.realpath(sys.executable))')"

  # 5. How the phone reaches it. Tailscale is private and works anywhere; without it,
  #    Relay listens only on this Mac unless you allow your Wi-Fi.
  TS="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
  TS_ON=""
  if [ -x "$TS" ] && "$TS" status >/dev/null 2>&1; then TS_ON=1; fi
  if [ -z "$TS_ON" ] && ! grep -q '^RELAY_BIND=' "$ENV_FILE"; then
    echo "  Tailscale isn't set up, so right now only this Mac can open Relay."
    if (: < /dev/tty) 2>/dev/null; then
      printf 'Let phones on this Wi-Fi reach Relay? Only do this on your home network. [Y/n]: '
      read -r ANS < /dev/tty || ANS=n
      case "$ANS" in [nN]*) echo "  Kept private to this Mac." ;; *) put RELAY_BIND 0.0.0.0 ;; esac
    fi
  fi

  # Telegram: talk to Relay from anywhere. Long polling, so nothing on the Mac is opened to the internet.
  if [ -n "${RELAY_TELEGRAM_TOKEN:-}" ]; then put RELAY_TELEGRAM_TOKEN "$RELAY_TELEGRAM_TOKEN"; fi
  if ! grep -q '^RELAY_TELEGRAM_TOKEN=.' "$ENV_FILE" && (: < /dev/tty) 2>/dev/null; then
    echo "  Optional: control Relay from Telegram, from anywhere. In Telegram, message @BotFather,"
    echo "  send /newbot, pick a name, and copy the token it gives you (looks like 123456789:AAE…)."
    printf 'Telegram bot token (press Enter to skip; not shown): '
    read -rs TG < /dev/tty || TG=""; echo
    if [ -n "$TG" ]; then
      if printf '%s' "$TG" | grep -qE '^[0-9]+:[A-Za-z0-9_-]{30,}$'; then put RELAY_TELEGRAM_TOKEN "$TG"
      else echo "  That doesn't look like a bot token (123456789:AAE…); skipped. Run this again to retry."; fi
    fi
    unset TG
  fi
  BOT=""
  if grep -q '^RELAY_TELEGRAM_TOKEN=.' "$ENV_FILE"; then
    # the token goes to curl on stdin, not on the command line
    TGT="$(grep '^RELAY_TELEGRAM_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    BOT="$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$TGT" | curl -fsS -m 15 -K - 2>/dev/null \
           | sed -n 's/.*"username":"\([^"]*\)".*/\1/p' || true)"
    unset TGT
    if [ -n "$BOT" ]; then say "Telegram bot @$BOT connected"
    else echo "  Telegram didn't accept the bot token. Check it in @BotFather, then run this again."; fi
  fi

  # 6. Start at login, keep running (restarts it, so new settings apply).
  say "Starting Relay (and at every login)"
  RELAY_HOME="$HOME_DIR" "$VPY" -m macagent install-launchagent >/dev/null
  if [ -n "$TS_ON" ]; then
    # The first time, Tailscale asks you to turn on HTTPS for your tailnet and prints a link: show it.
    if OUT="$("$TS" serve --bg "$PORT" 2>&1 </dev/null)"; then
      say "Shared privately on your tailnet: works from anywhere (uni, bus, mobile data)"
    else
      echo "$OUT" | sed 's/^/  /'
      echo "  Tailscale needs one setting first: open the link above, turn on HTTPS, then run this script again."
    fi
  elif grep -q '^RELAY_BIND=0.0.0.0' "$ENV_FILE"; then
    say "Open to this Wi-Fi. If macOS asks whether python3 may accept incoming connections, click Allow."
    echo "  To use it away from home: install Tailscale on the Mac and phone, sign in, run this again."
  fi

  # 7. Wait for it, then pair the phone.
  for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 1; done
  curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 || die "Relay did not start. See $HOME_DIR/relay.log"
  say "Relay is running"
  RELAY_HOME="$HOME_DIR" "$VPY" -m macagent pair
  if [ -n "$BOT" ]; then
    printf '\n\033[1mTelegram:\033[0m open https://t.me/%s on your phone, tap Start, and send the /pair line above.\n' "$BOT"
  fi

  cat <<EOF

Done. If the permission check above showed ✗, open System Settings → Privacy & Security and add
  $REAL_PY
to Screen Recording and Accessibility (that is what the background service runs), then run:
  launchctl kickstart -k gui/$(id -u)/dev.relay.agent
Logs: $HOME_DIR/relay.log   ·   Update later: run the same one-line command again.
EOF
}

main "$@"
