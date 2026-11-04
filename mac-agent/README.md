# Relay — your Mac mini, from your phone

Relay is a personal agent in the spirit of Meta's Muse for Mac. It runs on your
Mac mini, you hand it tasks from your phone, and it keeps working while
you're away. It works across apps, files, Calendar, Reminders, Notes, Mail and
Messages, can see and drive the whole screen, and **asks you before anything
that sends, deletes or changes things**.

## Architecture: DeepSeek's Super-agent design

Relay implements the design in DeepSeek Harness's note *Exploring Super-agent
Design* (1 Nov 2026), as explained by Vizuara. It is one continuous,
productive, proactive and persistent agent: you never open sessions; events
reach it and it opens its own.

```
 where things happen        ⑤ TRIGGERS + FILTER          ④ ROUTER                     WORKERS
 phone app · Telegram ─┐    code rules (fast path)       one voice, light model       own context + tools,
 Telegram groups ──────┼──▶ └▶ policies on a light ────▶ no tools of its own ───────▶ strongest model,
 folder · mail · cal ──┤       model (slow path)         batches events               parallel, screen lease
 sensors · webhooks ───┘    ⑥ followed Thread? ─────── pass straight through ───────▶ (§8 silent · §9 jobs)
                                                          ⑨ replies per channel ◀── report()
 ⑩ session DB + loop here   ── commands ──▶ this Mac / other Macs via a stateless daemon
 ⑪ Watchdog (own thread)    ── alive? making progress? ──▶ nudges, pauses, proposals you approve
```

| § in the note | In Relay |
| --- | --- |
| **4 Router and Workers** | `router.py` is the only thing that talks to people. It runs on a light model (Claude Haiku 5.5 or `deepseek-chat`), has no tools that touch a machine, and batches events that arrive together into one call. Workers (`agent.py`) each have an isolated context and the strongest model (Claude Opus 5.5); several run in parallel. Workers never post: they `report()`, and the router writes every reply. |
| **5 Which events matter** | `filters.py`: code rules first (per-source *admit / candidate / explicit-only / ignore*, sender lists; an @mention or a direct message is admitted at once), then a light model judges candidates against **policies written as prompts** that you edit in Settings. Every event keeps its verdicts (Tasks → Events). |
| **6 Threads and pass-through** | Events carry a Thread key (email subject chain, Telegram reply chain, one automation's stream). A dispatched worker subscribes to its events' Threads; follow-ups are delivered straight into its inbox by code, with no router decision. Many-to-many: workers can `subscribe_thread` / `unsubscribe_thread`. |
| **7 Inline & Spill, Chain of Recall** | `context.py`: every object has an ID (`E12`, `W7`, `O31`, `thread:…`). An event is injected minimal-first, with what's near in space and time inlined as one line each. Long content is cut with a pointer, and tool outputs over 6 KB are spilled to the database. `read(ID)` pages through anything. The router keeps one line per past task, then follows IDs (`read(W7)`) to answer "did that get fixed?". |
| **8 Acting ahead** | `dispatch_task(mode="silent")`: a speculative background worker. It cannot use the screen or ask you anything. It may prepare (draft a reply), but not take actions that need approval. It either `discard()`s quietly, or reports what it confirmed. The default policies include "silently reproduce and fix" and "draft replies to invitations". |
| **9 Full duplex, one voice per channel** | `job_start()` returns `Started [J3]` at once. The result arrives later as a message (full output → `read(O…)`), and the worker keeps answering in the meantime; `job_stop()` ends a job. Chat replies are capped at **100 words**. |
| **10 Compute, state, environments** | Sessions live in SQLite. After a restart, interrupted workers **resume** from their history (up to 3 times). Other machines run `python -m macagent daemon`, which connects out to the server, keeps no session state, and executes host operations. A task targets a machine, and if that machine sleeps, only that task waits. `RELAY_LOCAL_HOST=none` runs the loop on a server with no screen of its own. |
| **11 Watchdog** | `watchdog.py` runs on its own thread and event loop, on a schedule. It checks liveness (event-loop heartbeat, database, model errors, offline machines, failing triggers) and progress (code heuristics for repeated actions or failures and stalls, then a light model judges moving / stuck / drifting). It nudges a stuck worker, and pauses it if it's still stuck after the nudge. Ask it "why did #12 keep retrying?"; it can propose policy, guidance or trigger changes that apply only when you tap Apply. |
| **12 The whole design** | Tasks → **System** draws the note's "five modules on one page" with live counts: where work happens (each source and its filter mode) → two-stage filter (code admitted, sent to the model, kept, noise dropped) → router (dispatched, ignored; runs no tools) → workers (live, silent, their jobs and threads), with the followed-thread bypass, replies per channel, your Macs, the Watchdog and the session database. Tap a module to open it. One column on a phone, the whiteboard layout on a wide screen. Served by `GET /api/system?hours=`. |
| **13 Open problems** | Security is still the big one; see *Limits* below. |

## What it does

- **Chat with your Mac.** "Summarize my unread email", "Move last week's
  screenshots into a folder", "Book the 7pm slot on the gym site", "Text Sam
  I'm running late". The router answers status questions itself, hands real
  work to workers, and tells you how each task went. You can also open any
  task to watch it step by step and send it follow-ups.
- **Works while you're away.** Tasks run in the background on the Mac whether
  or not your phone is connected. You can schedule them for later, or repeat
  them hourly, daily, on weekdays or weekly ("every weekday at 8:30, brief me
  on my calendar and inbox").
- **Native tools first, screen when needed.** Calendar, Reminders, Notes, Mail,
  Messages, Finder, Spotlight, the clipboard and the shell are driven through
  AppleScript/JXA and the CLI. For everything else, Claude's computer-use
  toolset sees the screen and clicks, types, scrolls and drags. On Retina
  displays, coordinates are mapped from the screenshot back to the display.
- **Asks before it acts.** Approvals are enforced in code, not left to the
  model. They arrive as cards on your phone, as Telegram buttons, or as ntfy
  push notifications. Choose how much Relay may do on its own:

  | Policy | Asks before |
  | --- | --- |
  | careful | every action except looking |
  | **balanced** (default) | changing data, sending, anything risky |
  | autonomous | sending messages and high-risk actions only |

  Shell commands are classified automatically. `ls`, `git status` and `df` run
  freely. Redirection and installs count as changes. `sudo`, `rm -r`,
  disk-erase commands and keychain dumps are high risk. Reading `~/.ssh`,
  keychains or browser data is high risk too. Relay's own data folder is off
  limits to every tool. For consequential clicks on screen (Send, Buy, Submit,
  Accept), the model is instructed to call `request_approval` first.
- **Live screen from your phone.** Watch the Mac at about 1 fps. Tap to click
  (or double-click, right-click, point), type, and use ⌘ ⌥ ⌃ ⇧ modifiers and
  shortcuts. While Relay is using the screen, **Take over** pauses it and
  **Hand back** resumes it. Every manual action is logged.
- **Remembers.** Facts you mention ("my partner is Sam", "I prefer aisle seats")
  are saved and offered to later tasks. You can review and delete them in
  Settings. It refuses to store anything that looks like a secret.
- **Spend limits.** Every model call is metered. A daily cap (default $10) and
  a step limit stop runaway loops.

## Set up on the Mac mini

Requirements: macOS 13 or later, Python 3.10 or later (`brew install python`),
and an Anthropic API key.

**One line.** In Terminal on the Mac mini:

```bash
curl -fsSL https://raw.githubusercontent.com/Dhruba531/AutoresearchHarnessAgent/claude/festive-fermi-gb3xr7/mac-agent/install.sh | bash
```

It downloads Relay into `~/Relay`, installs it, asks for your API key once
(hidden as you paste it, saved in `~/.relay/env` with owner-only access),
checks permissions, starts Relay at login, shares it over Tailscale if you're
signed in, and prints a pairing code and QR code for your phone. Run the same
line again to update. To skip the key prompt, put `ANTHROPIC_API_KEY=sk-ant-…`
before `bash`. The steps below do the same thing by hand.

```bash
git clone https://github.com/Dhruba531/AutoresearchHarnessAgent.git
```

```bash
cd AutoresearchHarnessAgent/mac-agent
```

```bash
python3 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

Put your key in Relay's private env file:

```bash
mkdir -p ~/.relay && echo 'ANTHROPIC_API_KEY=sk-ant-…' >> ~/.relay/env && chmod 600 ~/.relay/env
```

Check permissions, then start it:

```bash
python -m macagent doctor
```

```bash
python -m macagent serve
```

macOS asks for three permissions. `doctor` tells you which are missing. Grant
them to the app that runs Relay: Terminal or iTerm, or the venv's `python`
binary when Relay runs as a LaunchAgent.

1. **Screen Recording**, to see the screen.
2. **Accessibility**, to move the mouse and type.
3. **Automation**, approved per app the first time Relay scripts Mail,
   Calendar, Messages and the rest.

Optionally grant **Full Disk Access** for files outside your home folder.

### Reach it from your phone (Tailscale recommended)

Relay listens on `127.0.0.1` only. The safest way to reach it from anywhere is
[Tailscale](https://tailscale.com): install it on the Mac and on your phone,
sign in to both, then:

```bash
tailscale serve --bg 8765
```

Your phone can now open `https://<mac-name>.<tailnet>.ts.net`, over HTTPS and
only inside your private tailnet. Pair the phone:

```bash
python -m macagent pair
```

Scan the QR code, or open the site and type the code (single use, valid 10
minutes). In Safari use **Share → Add to Home Screen** to get an app icon.
Paired devices can be removed in Settings.

*Same Wi-Fi only, no Tailscale:* `python -m macagent serve --host 0.0.0.0`
and open `http://<mac-ip>:8765`. This is plain HTTP, so use it only on a
network you trust, and never forward the port to the internet.

### Add your other Macs (optional)

In Settings → Machines, tap **Add a machine** and run the command it shows on
the other Mac, for example a MacBook with its own apps and files:

```bash
python -m macagent daemon --server https://<mac-mini>.<tailnet>.ts.net --name macbook --code ABCD-EFGH
```

The daemon connects out, keeps no state, and needs the same macOS
permissions as Relay. The router can then send work to "macbook", and the
Screen tab can show and control it. When the MacBook sleeps, only its tasks
wait.

### Keep it always on

```bash
python -m macagent install-launchagent
```

This starts Relay at login and restarts it if it stops. While Relay runs, it
keeps the Mac awake with `caffeinate`; the display may still sleep.
Screen control needs a **logged-in, unlocked GUI session**:

- Relay cannot work at the login window, or before a FileVault unlock after a
  reboot.
- A Mac mini with no monitor may need an HDMI dummy plug to get a usable
  display resolution.
- If you turn on automatic login or disable the screen lock so Relay can
  always see the desktop, anyone with physical access gets the same. Decide
  that trade-off deliberately.

### Telegram and push notifications (optional)

**Telegram** works like nudging Muse over WhatsApp. Create a bot with
@BotFather, add `RELAY_TELEGRAM_TOKEN=…` to `~/.relay/env`, restart Relay, and
send `/pair CODE` to the bot with a code from `python -m macagent pair`. Then
message it like the Chat tab: your messages go to the router. Approvals
arrive as ✅/⛔ buttons, and workers' questions as messages you reply to.
`/screen` sends a screenshot, `/status` shows what's running, and `/stop`
cancels it. It uses long polling, so nothing needs to be exposed to the
internet. Only paired chats are obeyed.

**ntfy** sends push notifications for approvals and finished tasks. Set
`RELAY_NTFY_TOPIC=<long-random-name>` and subscribe to that topic in the ntfy
app. Pick an unguessable topic, because anyone who knows it can read the
notifications.

## Configuration (`~/.relay/env` or environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Claude: required for screen control |
| `RELAY_MODEL` | `claude-opus-5-5` | or `claude-sonnet-5-5` (faster and cheaper) |
| `RELAY_EFFORT` | `medium` | `low`…`xhigh`; also settable from the phone |
| `RELAY_PROVIDER` | `anthropic` | `deepseek` / `openai`: native tools only, no screen (these models can't see screenshots) |
| `DEEPSEEK_API_KEY`, `OPENAI_API_KEY` | — | for those providers |
| `RELAY_ROUTER_MODEL` | `claude-haiku-5-5` | the light routing model (`deepseek-chat` with DeepSeek) |
| `RELAY_WORKERS` | `3` | tasks that run at once (screen work still takes turns) |
| `RELAY_ROUTER_BATCH_MS`, `RELAY_ROUTER_MAX_WAIT_MS` | `1200`, `4000` | how long the router gathers a batch |
| `RELAY_LOCAL_HOST` | `auto` | `none`: no screen of its own; drive machines only through daemons |
| `RELAY_WATCHDOG_INTERVAL_S`, `RELAY_WATCHDOG_STALL_S` | `300`, `900` | how often the Watchdog checks; how long without a step counts as stalled |
| `RELAY_WATCHDOG_RESTART` | `false` | if the event loop hangs, exit so launchd restarts Relay (workers resume) |
| `RELAY_POLICY` | `balanced` | `careful` / `balanced` / `autonomous` |
| `RELAY_DAILY_CAP_USD` | `10` | stop when today's spend reaches this |
| `RELAY_MAX_STEPS` | `80` | per task, before it pauses for a follow-up |
| `RELAY_APPROVAL_TIMEOUT_S` | `1800` | an unanswered approval counts as a no |
| `RELAY_SCREEN_MAX_EDGE` | `1440` | screenshot size sent to the model |
| `RELAY_BIND`, `RELAY_PORT` | `127.0.0.1`, `8765` | listen address |
| `RELAY_TELEGRAM_TOKEN`, `RELAY_NTFY_TOPIC` | — | phone channels |
| `RELAY_KEEP_AWAKE` | `true` | run `caffeinate` while serving |

## How it works

| File | Role |
| --- | --- |
| `macagent/router.py` | routing layer: event log, pass-through, batching, light model, dispatch-only tools, `read`, 100-word cap, fallback |
| `macagent/filters.py` | two-stage filter: code rules, then policies on a light model |
| `macagent/context.py` | IDs, Inline & Spill, `read()` / Chain of Recall |
| `macagent/machines.py` | machine registry, `RemoteHost`, the stateless daemon |
| `macagent/watchdog.py` | the Watchdog: liveness, progress, nudges, maintainer chat, proposals |
| `macagent/triggers.py` | event triggers: folder, mail, calendar, system sensors, webhooks |
| `macagent/agent.py` | worker layer: parallel pool, screen lease, model↔tool loop, approvals, pause/take-over, schedule, budget |
| `macagent/llm.py` | Claude via the `anthropic` SDK (computer toolset, progress notes, context clearing, refusal fallback, caching); DeepSeek/OpenAI adapter |
| `macagent/tools.py` | native tools, their schemas, and approval summaries |
| `macagent/policy.py` | risk tiers, shell/AppleScript/path classification |
| `macagent/scripts.py` | AppleScript/JXA (inputs passed as argv, never spliced into code) |
| `macagent/host/mac.py` | `screencapture` + Quartz CGEvents, Retina coordinate mapping |
| `macagent/host/fake.py` | a drawn virtual desktop for development and tests |
| `macagent/server.py` | HTTP + WebSocket API, the phone app, manual control |
| `macagent/web/` | the phone app (no build step) |
| `macagent/telegram.py`, `notify.py` | Telegram bot, ntfy |

The loop follows the computer-use toolset's contract:

- One `tool_result` answers each call, and each echoes `toolset_name`.
- When an action in a batch fails, the rest are answered "Not executed".
- History is append-only, so thinking blocks stay valid.
- Old screenshots are cleared server-side, never by editing history.

## Develop and test anywhere

```bash
python -m macagent serve --fake-host     # virtual desktop, real API calls
```

```bash
python -m pytest
```

The tests run the real Anthropic SDK against a mock transport, on the
virtual desktop. They cover:

- every section of the design note (`tests/test_design_note.py`):
  - the two-stage filter;
  - Thread pass-through while a background job runs;
  - spill, then `read()`, then Chain of Recall;
  - silent workers that discard or report;
  - the 100-word cap;
  - a task waiting for its machine, then running on it;
  - resume after a restart, and the daemon over a real WebSocket;
  - Watchdog nudge → pause → proposal → apply;
  - Telegram group mentions and threads;
  - the System map's counts for every module;
- the router batching a burst into one light-model call with no Mac tools,
  dispatching, reporting back, answering questions but never approvals, and
  its no-model fallback;
- parallel workers taking turns on the screen;
- folder triggers (partial downloads, stable files, single fire) and
  webhook secrets;

- screen batches, `toolset_name` echoes and the halt-after-failure rule;
- approvals over the app and Telegram, denials and `ask_user`;
- follow-ups, cancel and history repair, pause and take-over;
- scheduled and repeating tasks;
- pairing, single-use codes, rate limiting and revocation;
- the shell classifier and the DeepSeek history translation.

## Limits and honest notes

- **Not yet run on a real Mac.** The macOS-specific layer (`host/mac.py`
  CGEvents and `screencapture`, and the AppleScript in `scripts.py`) was
  written against Apple's APIs but has not been run on a Mac. Everything above
  it was exercised on the virtual desktop. The JXA scripts parse, but
  AppleScript can only be checked on macOS. Expect a first-run fix or two;
  `doctor` and the error messages point at permissions first.
- Only the **main display** of each machine is captured and controlled.
- Background jobs live in memory. After a restart, workers resume, but a job
  they had started must be started again, and the worker is told so.
- **The design note's open security problem applies.** The loop and every
  paired machine trust each other, so a compromised server or a successful
  prompt injection reaches every connected machine. Pair only machines you
  own, keep the server inside your tailnet, and remove machines you no longer
  use. API keys stay in the server's environment and never enter a model's
  context.
- **Prompt injection.** Content Relay reads (emails, web pages) can try to
  instruct it. Relay treats such content as data, and code-enforced approvals
  gate what matters. Still, read approval cards before you tap Approve. The
  `balanced` policy lets on-screen clicks through without asking; use
  `careful` for sites where one click spends money.
- Anthropic recommends running computer use in a dedicated, low-privilege
  environment. Relay deliberately runs on your real Mac. Consider a separate
  macOS user account for it, signed in only to what it needs.
