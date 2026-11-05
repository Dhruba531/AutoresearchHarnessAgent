# Relay on your Mac: install guide and Telegram commands

This guide takes you from nothing to controlling your Mac from your phone,
anywhere: at home, at university, or on the bus. It covers:

1. [What you need](#1-what-you-need)
2. [Create your Telegram bot](#2-create-your-telegram-bot)
3. [Install Relay on the Mac](#3-install-relay-on-the-mac)
4. [Pair your phone](#4-pair-your-phone)
5. [Grant macOS permissions](#5-grant-macos-permissions)
6. [Use it from anywhere (Tailscale)](#6-use-it-from-anywhere-tailscale)
7. [All bot commands](#7-all-bot-commands)
8. [Models, keys and cost](#8-models-keys-and-cost)
9. [After a restart or power cut](#9-after-a-restart-or-a-power-cut)
10. [Updating](#10-updating)
11. [Troubleshooting](#11-troubleshooting)
12. [Safety](#12-safety)

---

## 1. What you need

| Item | Why | Cost |
| --- | --- | --- |
| A Mac (a Mac mini is ideal) | Relay runs on it and controls it | — |
| An Android phone or iPhone with **Telegram** | To talk to Relay | Free |
| **One AI key**: Gemini, OpenAI, OpenRouter, DeepSeek or Anthropic, or Ollama for a free local model | Relay's brain | Pay per use, from about $0.005 per message; Ollama is free |
| **Tailscale** on the Mac and phone (optional, recommended) | The full app from anywhere | Free (Personal plan) |

Which AI key to get:

| Provider | Get a key | Good for |
| --- | --- | --- |
| **Gemini** (recommended for students) | <https://aistudio.google.com/apikey> | Fast and cheap |
| DeepSeek | <https://platform.deepseek.com/api_keys> | Cheapest per message |
| OpenAI | <https://platform.openai.com/api-keys> (add $5 credit under Billing) | Fast, cheap |
| OpenRouter | <https://openrouter.ai/settings/keys> | One key, hundreds of models (Claude too) |
| Anthropic | <https://console.anthropic.com/settings/keys> | Smartest; the only one that can click the screen |
| Ollama (no key) | <https://ollama.com/download> | Free and private, but slower |

You can add more keys later and switch between them from Telegram.

---

## 2. Create your Telegram bot

Do this on your phone. It takes about 2 minutes.

1. In Telegram, search for **@BotFather** (the one with the blue tick) and tap **Start**.
2. Send `/newbot`.
3. Send a display name, e.g. `My Mac`.
4. Send a username that ends in `bot`, e.g. `dhruba_mac_bot`.
5. BotFather replies with a **token** that looks like `123456789:AAE…`. **Copy it.**
   You paste it into the installer in the next step.

> Keep the token private. Anyone with it can impersonate your bot (though
> they still can't control your Mac without pairing).

---

## 3. Install Relay on the Mac

Open **Terminal** on the Mac (press ⌘ Space, type *Terminal*, press Return),
paste this one line and press Return:

```bash
curl -fsSL https://raw.githubusercontent.com/Dhruba531/AutoresearchHarnessAgent/claude/festive-fermi-gb3xr7/mac-agent/install.sh | bash
```

The installer does everything itself and asks you a few questions along the way:

| It asks | What to do |
| --- | --- |
| **Key or local (not shown)** | Paste your AI key and press Return. Nothing appears as you paste; that's normal. Or type `local` to use Ollama. If the key is ambiguous it asks `OpenAI or DeepSeek? [o/d]` |
| **Let phones on this Wi-Fi reach Relay? [Y/n]** | Only asked without Tailscale. Press Return (yes) on your home network |
| **Telegram bot token** | Paste the BotFather token from step 2 and press Return. Nothing appears as you paste |
| **Keep this Mac awake and have it start again after a power cut? [Y/n]** | Press Return (yes), then type your **Mac login password** (not shown). This lets it come back by itself |
| **Allow any browser? [y/N]** | Only with Tailscale. `y` lets you open the app on a university computer; Return keeps it to your own devices |

At the end it prints a **pairing code**, a **QR code**, and a line like:

```
Telegram: open https://t.me/your_bot on your phone, tap Start, and send the /pair line above.
```

What got installed:
- the code, in `~/Relay`;
- your keys, in `~/.relay/env`, readable only by you;
- a background service (`dev.relay.agent`) that starts Relay at every login
  and restarts it if it stops.

If your Mac has no Python 3.10+, the installer installs a private copy with
`uv`. You don't need Homebrew.

---

## 4. Pair your phone

1. On your phone, open the link the installer printed (`https://t.me/your_bot`) and tap **Start**.
2. Send the pairing line it printed, for example:
   ```
   /pair 7JMSQSHC
   ```
3. The bot answers **"Paired. Send me anything you want your Mac to do."**

Pairing codes work **once** and expire after **10 minutes**. For a new one,
run this on the Mac:

```bash
cd ~/Relay/mac-agent && .venv/bin/python -m macagent pair
```

The bot answers only paired chats. Anyone else who finds your bot is ignored.

**Check:** send `what apps are open on my Mac?` If it answers, everything works.

---

## 5. Grant macOS permissions

The installer prints the exact Python path the background service uses, e.g.

```
/Users/you/.local/share/uv/python/cpython-3.12.14-macos-aarch64-none/bin/python3.12
```

In **System Settings → Privacy & Security**, add that file to:

| Permission | Needed for |
| --- | --- |
| **Accessibility** | Clicking, typing, `/lock`, controlling apps |
| **Screen Recording** | `/screen`, the app's live Screen view, screen control |
| **Automation** (macOS asks the first time) | Mail, Calendar, Notes, Reminders, Finder; click **OK** |

Also check **System Settings → General → Login Items → Allow in the
Background**: **python** must be **on**, or Relay won't start at login.

Then restart Relay:

```bash
launchctl kickstart -k gui/$(id -u)/dev.relay.agent
```

Check everything at once. Each line should show ✓:

```bash
cd ~/Relay/mac-agent && .venv/bin/python -m macagent doctor
```

> In `doctor`, a ✗ next to *Screen Recording* can be ignored if the app's
> Screen view works. `doctor` checks Terminal, while Relay runs as python.

---

## 6. Use it from anywhere (Tailscale)

Telegram works from anywhere with no extra setup. For the **full app**
(the live screen, tasks, settings and model picker) away from home:

1. Install **Tailscale** on the Mac (<https://tailscale.com/download/mac>) and
   on your phone (Play Store or App Store). **Sign in with the same account** on both.
2. On the Mac, turn on sharing for Relay:
   ```bash
   /Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 8765
   ```
   The first time, it prints a link: open it, click **Enable**, and the
   command finishes by itself.
3. Run the updater, which saves your Tailscale address for Relay:
   ```bash
   cd ~/Relay && git pull -q && bash mac-agent/install.sh
   ```
   Answer `y` to **Allow any browser?** if you want university computers to work too.
4. In Telegram send **`/app`** and tap **📱 Open Relay**. The app opens inside
   Telegram. Tap **Pair**, then in Chrome use **⋮ → Add to Home screen** for
   a one-tap icon.

**Test it like you're away:** turn Wi-Fi off on the phone and open the app on
mobile data.

---

## 7. All bot commands

Type **/** in the chat to see this list in Telegram.

### Talk normally

You don't need commands for most things. Just write:

- `what's on my calendar tomorrow?`
- `find the invoice PDF I downloaded last week and move it to Documents/Receipts`
- `draft a reply to Sam's last email saying Thursday works`
- `every weekday at 8, tell me my meetings`
- `use gemini`, `switch to claude`, or `change model to gpt nano` switches the AI model

When Relay wants to **send, delete or change** something, it asks first with
**✅ Approve / ⛔ Deny** buttons. **Reply** to one of its "On it — task #N"
messages to add details to that task. When it asks a question, reply to that
message with your answer.

### Commands

| Command | What it does | Example |
| --- | --- | --- |
| `/app` | A one-time link (10 min) that opens the full Relay app, inside Telegram or in a browser | `/app` |
| `/model` | Which model runs now, with a button per provider you have a key for | `/model` |
| `/model <name>` | Switch by name | `/model gemini` · `/model haiku` · `/model gpt nano` · `/model local` |
| `/mac` | 🔋 battery, 💾 free disk, ⏱ uptime, 🪟 the app in front, model, today's spend | `/mac` |
| `/status` | What's running now, and what's waiting for your approval | `/status` |
| `/tasks` | The last 10 tasks with their status | `/tasks` |
| `/stop` | Stop every running task | `/stop` |
| `/screen` | A screenshot of the Mac | `/screen` |
| `/get <name or path>` | Send yourself a file from your home folder. Several matches give you buttons | `/get resume.pdf` · `/get ~/Documents/notes.txt` |
| *(send a file or photo)* | Saved on the Mac in **Downloads → From Telegram** (up to 20 MB) | — |
| `/clip` | Show the text on the Mac's clipboard | `/clip` |
| `/copy <text>` | Put text on the Mac's clipboard (phone → Mac copy-paste) | `/copy meeting at 3pm room 204` |
| `/open <app>` | Open an app | `/open Safari` · `/open "Visual Studio Code"` |
| `/lock` | Lock the Mac's screen | `/lock` |
| `/spend` | Today's cost, per provider | `/spend` |
| `/cap <dollars>` | Set the daily spending limit (remembered) | `/cap 1` |
| `/speed` | Where the time went on recent tasks: model, Mac actions, waiting | `/speed` |
| `/help` | The command list | `/help` |
| `/pair <code>` | Pair a new chat (from the installer or `macagent pair`) | `/pair 7JMSQSHC` |

### In a Telegram group

You can pair a group too, by sending `/pair CODE` in it. Relay then answers
when someone **@mentions** the bot or **replies** to it. Other messages are
filtered by your rules (see Settings → *What deserves attention* in the app).
For safety, `/get`, `/clip`, `/copy`, `/open`, `/lock` and `/app` work **only
in your private chat**, never in a group. Switching the model also works only
from your private chat.

---

## 8. Models, keys and cost

- **Add keys** in the app: **Settings → Models → (provider) → paste → Save**.
  Relay tests the key at once and then shows only its last 4 characters.
  Don't paste keys into the Telegram chat.
- **Switch** with `/model`, `/model gemini`, or by writing `use gemini`.
- **Switch automatically** is on by default. When a key runs out of credit or
  hits a rate limit, Relay moves to the next provider in your order and tells
  you.
- **Limit spending** with `/cap 1` ($1 a day). Relay stops at the cap until midnight.

Rough cost per message:

| Model | Per message | Speed |
| --- | --- | --- |
| Gemini 3.8 Flash | ~$0.015 | fast |
| DeepSeek | ~$0.005 | medium |
| GPT-5.4 mini | ~$0.02 | fast |
| Claude Haiku 5.5 (OpenRouter) | ~$0.003 | fast |
| Claude Opus 5.5 (OpenRouter) | ~$0.08 | slow, but smartest |
| Ollama on the Mac | free | slow |

If replies feel slow, send `/speed`. If most of the time is "model", switch
with `/model haiku` or `/model gemini`.

---

## 9. After a restart or a power cut

You don't need Terminal for this. Relay starts by itself.

1. Power comes back → the Mac turns on by itself (if you said yes in step 3).
2. **If FileVault is on**, the Mac waits at a password screen. Someone at home
   types your Mac password once. Without FileVault and with automatic login
   (System Settings → Users & Groups), it skips this.
3. After login, macOS starts Relay automatically, and Tailscale too, if
   "Launch at login" is on in its menu-bar icon.
4. About a minute later the bot messages you: **"Your Mac restarted at 14:32
   … and Relay is back online."**

If that message never arrives, the Mac is still at the password screen, or the
home internet is down. For planned restarts, `sudo fdesetup authrestart`
unlocks FileVault for that one restart.

---

## 10. Updating

On the Mac:

```bash
cd ~/Relay && git pull -q && bash mac-agent/install.sh
```

Use this rather than the `curl` line for updates: GitHub caches the `curl`
copy for a few minutes, so it can be one version behind. Your keys, pairing,
models and settings are kept.

---

## 11. Troubleshooting

| Problem | Fix |
| --- | --- |
| The bot doesn't answer at all | On the Mac run `launchctl print gui/$(id -u)/dev.relay.agent \| grep "state ="`. It should say `running`. If not, start it: `launchctl kickstart -k gui/$(id -u)/dev.relay.agent`. Then read `tail -30 ~/.relay/relay.log` |
| The bot answers "can't switch models" or misses a command | The Mac runs old code: update (section 10) |
| Pairing code "wrong, used, or expired" | Make a new one: `cd ~/Relay/mac-agent && .venv/bin/python -m macagent pair` |
| The installer seems stuck after **"Starting Relay"** | Tailscale is waiting for you to enable HTTPS. Press Ctrl+C, run `/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 8765`, open the link it prints, click **Enable**, then update again |
| `/app` says it "needs Tailscale" | Run `/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status`, copy your `https://…ts.net` address, and save it, putting **your** address in: `echo 'RELAY_PUBLIC_URL=https://your-mac.tailXXXX.ts.net' >> ~/.relay/env && launchctl kickstart -k gui/$(id -u)/dev.relay.agent` |
| The app's QR or link points to `127.0.0.1` | That address means "this device". At home, use the Wi-Fi address (`http://192.168.x.x:8765`); away, use `/app` with Tailscale |
| Taps on the app's Screen view only move the mouse | You're in **Point** mode: tap **Tap** under the screenshot |
| The app shows **offline** | Its live connection dropped (common on mobile data). Close it and send `/app` again, or reload |
| `/screen` or the Screen view is black or fails | Add the python path to **Screen Recording** (section 5) and restart Relay |
| Replies are slow | Send `/speed`. Usually a faster model fixes it: `/model haiku` or `/model gemini` |
| "Daily spend cap reached" | Raise it with `/cap 2`, or wait until midnight |
| A key stopped working (credit or limit) | Relay switches automatically if you have another key; add credit or another key in Settings → Models |

Logs live in `~/.relay/relay.log`. For a full check, run
`cd ~/Relay/mac-agent && .venv/bin/python -m macagent doctor`.

---

## 12. Safety

- **Only paired chats** can control the Mac. Strangers get no reply at all.
- **Approvals:** anything that sends, deletes, buys or changes things waits for your **Approve** tap.
- **Keys** stay on the Mac in `~/.relay/env` (owner-only) and never reach the AI model.
- **`/get`** only reads inside your home folder, never from hidden folders such
  as `~/.ssh` or `~/.relay`.
- **On shared computers** (university labs), use a private window, and when
  you're done tap **Settings → Unpair this phone** in the app.
- **"Any browser" (Tailscale Funnel)** gives Relay a public address. It's
  still protected by one-time codes, but turn it off when you don't need it:
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale funnel reset`.
- **Prompt injection:** emails and web pages can contain text that tries to
  instruct an AI. Relay treats them as data, and approvals stop the
  dangerous actions, but **read approval cards before tapping Approve**.
