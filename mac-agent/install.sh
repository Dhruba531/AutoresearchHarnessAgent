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

# Local model: Ollama serves an OpenAI-compatible API on 127.0.0.1:11434.
setup_ollama() {
  local model tuned
  # the base model: RELAY_LOCAL_MODEL=qwen3:32b in front of the command picks a bigger one
  model="${RELAY_LOCAL_MODEL:-$(grep '^RELAY_LOCAL_MODEL=' "$ENV_FILE" | cut -d= -f2- || true)}"; model="${model:-qwen3:8b}"
  tuned="relay-$(printf '%s' "$model" | tr ':/' '--')"
  OLLAMA="$(command -v ollama || true)"
  [ -n "$OLLAMA" ] || { [ -x /Applications/Ollama.app/Contents/Resources/ollama ] && OLLAMA=/Applications/Ollama.app/Contents/Resources/ollama; } || true
  if [ -z "$OLLAMA" ]; then
    echo "  Ollama isn't installed. Download it from https://ollama.com/download (drag it to Applications),"
    echo "  open it once, then run this script again. Relay will start now but answer only once Ollama runs."
    return 0
  fi
  curl -fs http://127.0.0.1:11434/api/version >/dev/null 2>&1 || { open -a Ollama 2>/dev/null || (nohup "$OLLAMA" serve >/dev/null 2>&1 &); }
  for _ in $(seq 1 20); do curl -fs http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
  if "$OLLAMA" list 2>/dev/null | awk '{print $1}' | grep -qx "$model"; then
    echo "  Model $model is ready."
  else
    say "Downloading $model (about 5 GB, once)"
    "$OLLAMA" pull "$model" </dev/null || { echo "  Download failed; run: ollama pull $model"; return 0; }
  fi
  # Ollama's default 4k context is nearly full with Relay's tools before the first tool result, which makes
  # it slow and wrong. Build a copy with a 16k window (a few MB on disk; the weights are shared).
  MF="$(mktemp)"; printf 'FROM %s\nPARAMETER num_ctx 16384\n' "$model" > "$MF"
  if "$OLLAMA" create "$tuned" -f "$MF" </dev/null >/dev/null 2>&1; then
    put RELAY_LOCAL_MODEL "$model"; put RELAY_MODEL "$tuned"; put RELAY_ROUTER_MODEL "$tuned"
    echo "  Model $tuned ready (16k context, kept loaded so replies start fast)."
  else
    echo "  Could not build $tuned; using $model with Ollama's default context."
  fi
  rm -f "$MF"
  echo "  Tip: a 16 GB Mac mini runs $model well. With 32 GB or more, a bigger model is smarter: put"
  echo "  RELAY_LOCAL_MODEL=qwen3:32b in front of bash in the install line."
}

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
  if [ -n "${GEMINI_API_KEY:-}" ]; then put GEMINI_API_KEY "$GEMINI_API_KEY"; put RELAY_PROVIDER gemini; fi
  if [ -n "${DEEPSEEK_API_KEY:-}" ]; then put DEEPSEEK_API_KEY "$DEEPSEEK_API_KEY"; grep -q '^RELAY_PROVIDER=' "$ENV_FILE" || put RELAY_PROVIDER deepseek; fi
  # an OpenRouter key that an older version of this script filed as DeepSeek
  OLD="$(grep '^DEEPSEEK_API_KEY=sk-or-' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
  if [ -n "$OLD" ]; then
    grep -v '^DEEPSEEK_API_KEY=' "$ENV_FILE" > "$ENV_FILE.tmp" || true; mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
    put OPENROUTER_API_KEY "$OLD"; put RELAY_PROVIDER openrouter; echo "  Moved your OpenRouter key to OPENROUTER_API_KEY."
  fi
  unset OLD
  case "${RELAY_PROVIDER:-}" in anthropic|openrouter|gemini|openai|deepseek|local) put RELAY_PROVIDER "$RELAY_PROVIDER" ;; esac
  if ! grep -qE '^(ANTHROPIC|DEEPSEEK|OPENAI|OPENROUTER|GEMINI)_API_KEY=.+|^RELAY_PROVIDER=local$' "$ENV_FILE"; then
    (: < /dev/tty) 2>/dev/null || die "No API key and no terminal to ask in. Re-run with ANTHROPIC_API_KEY=sk-ant-… in front of the command."
    echo "  Paste an API key (Anthropic sk-ant-…, OpenRouter sk-or-…, Gemini AIza…, OpenAI or DeepSeek),"
    echo "  or type  local  to run a free model on this Mac with Ollama (nothing leaves the Mac)."
    printf 'Key or local (not shown): '
    read -rs KEY < /dev/tty || die "Could not read the key."; echo
    [ -n "$KEY" ] || die "No key entered."
    case "$KEY" in
      local|LOCAL|Local) put RELAY_PROVIDER local ;;
      sk-ant-*) put ANTHROPIC_API_KEY "$KEY"; put RELAY_PROVIDER anthropic ;;
      AIza*) put GEMINI_API_KEY "$KEY"; put RELAY_PROVIDER gemini
         echo "  Saved as a Gemini key: Gemini 3.8 Flash for tasks, 3.5 Flash-Lite for chat (fast and low cost)." ;;
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
  if ! grep -q '^RELAY_PROVIDER=local$' "$ENV_FILE"; then
    # leaving local: its model names mean nothing to a cloud provider
    grep -vE '^RELAY_(ROUTER_)?MODEL=relay-' "$ENV_FILE" > "$ENV_FILE.tmp" || true
    mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
  fi
  if grep -q '^RELAY_PROVIDER=local$' "$ENV_FILE"; then
    say "Using a local model (Ollama)"
    setup_ollama
  else
    say "Key saved in $ENV_FILE (owner-only)"
  fi

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

  # Come back by itself: never sleep, and power on again after a power cut (needs your Mac password once).
  if command -v pmset >/dev/null 2>&1; then
    PM="$(pmset -g 2>/dev/null || true)"
    SLEEP="$(printf '%s\n' "$PM" | awk '$1 == "sleep" {print $2; exit}')"
    AUTO="$(printf '%s\n' "$PM" | awk '$1 == "autorestart" {print $2; exit}')"
    if { [ "$SLEEP" != 0 ] || { [ -n "$AUTO" ] && [ "$AUTO" != 1 ]; }; } && (: < /dev/tty) 2>/dev/null; then
      printf 'Keep this Mac awake and have it start again after a power cut? Asks your Mac password once. [Y/n]: '
      read -r ANS < /dev/tty || ANS=n
      case "$ANS" in
        [nN]*) echo "  Left as is." ;;
        *) if sudo pmset -a sleep 0 autorestart 1 < /dev/tty; then say "The Mac stays awake and restarts after a power cut"
           else echo "  Couldn't change it; set it in System Settings → Energy."; fi ;;
      esac
    fi
  fi

  # 6. Start at login, keep running (restarts it, so new settings apply).
  say "Starting Relay (and at every login)"
  RELAY_HOME="$HOME_DIR" "$VPY" -m macagent install-launchagent >/dev/null
  if [ -n "$TS_ON" ]; then
    # The first time, Tailscale asks you to turn on HTTPS for your tailnet and prints a link: show it.
    if OUT="$("$TS" serve --bg "$PORT" 2>&1 </dev/null)"; then
      say "Shared privately on your tailnet: works from anywhere (uni, bus, mobile data)"
      # Optional: any browser, even a computer without Tailscale (Tailscale Funnel). Off unless you say yes.
      if [ -n "${RELAY_PUBLIC:-}" ]; then put RELAY_PUBLIC "$RELAY_PUBLIC"; fi
      if ! grep -q '^RELAY_PUBLIC=' "$ENV_FILE" && (: < /dev/tty) 2>/dev/null; then
        echo "  Optional: open Relay in ANY browser (e.g. a university computer) without Tailscale on it."
        echo "  It gets a public web address; a browser still needs a one-time code (from /app in Telegram) to get in."
        printf 'Allow any browser? [y/N]: '
        read -r ANS < /dev/tty || ANS=n
        case "$ANS" in [yY]*) put RELAY_PUBLIC 1 ;; *) put RELAY_PUBLIC 0 ;; esac
      fi
      if grep -q '^RELAY_PUBLIC=1' "$ENV_FILE"; then
        if OUT="$("$TS" funnel --bg "$PORT" 2>&1 </dev/null)"; then
          say "Open to any browser at https://$("$TS" status --json 2>/dev/null | sed -n 's/.*"DNSName": *"\([^"]*\)\.".*/\1/p' | head -1)"
          echo "  Get a sign-in link any time: send /app to your Telegram bot. Turn it off: tailscale funnel reset"
        else
          echo "$OUT" | sed 's/^/  /'
          echo "  Tailscale needs Funnel allowed first: open the link above, allow it, then run this script again."
        fi
      fi
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

To have it come back with no help after the Mac turns on: System Settings → Users & Groups →
"Automatically log in as" → your account (not possible while FileVault is on; then log in once after a
restart). Relay starts as soon as you are logged in. Check it any time with:
  cd "$APP" && .venv/bin/python -m macagent doctor
EOF
}

main "$@"
