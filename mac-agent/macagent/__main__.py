"""Command line: `python -m macagent <serve|pair|doctor|install-launchagent>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

from .config import Settings


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packet is sent; picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return None


def tailscale_name() -> str | None:
    exe = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not os.path.exists(exe):
        return None
    try:
        import json

        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5).stdout
        name = json.loads(out).get("Self", {}).get("DNSName", "").rstrip(".")
        return name or None
    except Exception:
        return None


def print_qr(url: str) -> None:
    try:
        import qrcode  # optional

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        print("(pip install qrcode to show a scannable QR code here)")


def urls(settings: Settings) -> list[str]:
    out = []
    ts = tailscale_name()
    if ts:
        out.append(f"https://{ts}  (after `tailscale serve --bg {settings.port}`)")
    if settings.host in {"0.0.0.0", "::"}:
        ip = lan_ip()
        if ip:
            out.append(f"http://{ip}:{settings.port}  (same Wi-Fi)")
    out.append(f"http://127.0.0.1:{settings.port}  (on this Mac)")
    return out


def cmd_pair(settings: Settings, quiet: bool = False) -> str:
    from .auth import new_pairing_code
    from .store import Store

    store = Store(str(settings.db_path))
    code = new_pairing_code(store)
    pretty = f"{code[:4]}-{code[4:]}"
    base = urls(settings)[0].split()[0]
    link = f"{base}/pair?code={code}"
    print(f"\nPairing code: {pretty}   (single use, expires in 10 minutes)\n")
    print("Open Relay on your phone and enter it, or scan:")
    print_qr(link)
    print(f"\n  {link}")
    print("\nReachable at:\n  " + "\n  ".join(urls(settings)))
    if settings.telegram_token:
        print(f"\nTelegram: send   /pair {code}   to your bot.")
    return code


def cmd_doctor(settings: Settings) -> int:
    ok = True

    def line(good: bool | None, label: str, hint: str = "") -> None:
        nonlocal ok
        mark = "✓" if good else ("?" if good is None else "✗")
        ok = ok and good is not False
        print(f" {mark} {label}" + (f"\n     → {hint}" if hint and not good else ""))

    print(f"Relay doctor — {sys.platform}, Python {sys.version.split()[0]}, home {settings.home}")
    line(sys.version_info >= (3, 10), "Python ≥ 3.10")
    mac = sys.platform == "darwin"
    line(mac, "running on macOS", "Relay controls a Mac; elsewhere it can only run with --fake-host.")
    if mac:
        try:
            import Quartz  # noqa: F401

            line(True, "pyobjc Quartz installed")
        except ImportError:
            line(False, "pyobjc Quartz installed", "pip install pyobjc-framework-Quartz")
        from .host.mac import accessibility_trusted, screen_capture_allowed

        line(accessibility_trusted(), "Accessibility permission (mouse & keyboard)",
             "System Settings → Privacy & Security → Accessibility → add the app running Relay "
             "(Terminal/iTerm, or the python binary for the LaunchAgent).")
        line(screen_capture_allowed(), "Screen Recording permission",
             "System Settings → Privacy & Security → Screen Recording → add the same app, then restart it.")
        r = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of first process'],
                           capture_output=True, text=True, timeout=20)
        line(r.returncode == 0, "Automation of System Events",
             "Approve the macOS prompt, or System Settings → Privacy & Security → Automation.")
        # Comes back by itself after the Mac turns on: awake, restarts after a power cut, logs in, starts Relay.
        pm = subprocess.run(["pmset", "-g"], capture_output=True, text=True).stdout

        def pmval(key: str) -> int | None:
            m = re.search(rf"^\s*{key}\s+(\d+)", pm, re.M)
            return int(m.group(1)) if m else None

        line(pmval("sleep") == 0, "the Mac never sleeps on its own",
             "System Settings → Energy → Prevent automatic sleeping (or run the installer again and say yes).")
        restart = pmval("autorestart")
        line(None if restart is None else restart == 1, "it starts again after a power cut",
             "System Settings → Energy → Start up automatically after a power failure.")
        agent_on = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/dev.relay.agent"],
                                  capture_output=True).returncode == 0
        line(agent_on, "Relay starts by itself at login",
             "python -m macagent install-launchagent, and allow python under System Settings → General → "
             "Login Items → Allow in the Background.")
        auto = subprocess.run(["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"],
                              capture_output=True, text=True).returncode == 0
        vault = "is On" in subprocess.run(["fdesetup", "status"], capture_output=True, text=True).stdout
        line(True if auto else None, "it logs in by itself after a restart",
             "FileVault is on, so macOS asks for your password once after every restart; Relay starts as soon as "
             "you log in. (Turning FileVault off allows automatic login, but anyone with the Mac can then use it.)"
             if vault else "System Settings → Users & Groups → Automatically log in as → your account.")
    if settings.provider == "local":
        import urllib.request

        try:
            with urllib.request.urlopen(settings.local_base_url.rstrip("/") + "/models", timeout=5) as r:
                names = [m.get("id") for m in json.load(r).get("data", [])]
            line(True, f"local model server at {settings.local_base_url}")
            line(settings.model in names, f"model {settings.model} is downloaded",
                 f"ollama pull {settings.model}   (or set RELAY_MODEL to one of: {', '.join(names[:6]) or 'none'})")
        except Exception:
            line(False, f"local model server at {settings.local_base_url}",
                 "Install Ollama from https://ollama.com/download and open it (or start LM Studio's server).")
    else:
        line(bool(settings.api_key()), f"API key for {settings.provider}", f"Add it to {settings.home / 'env'} (chmod 600).")
    line(settings.can_see_screen, f"model {settings.model} can see the screen",
         "Only Claude through an Anthropic key drives the screen; with Gemini, DeepSeek, OpenAI, OpenRouter or a "
         "local model Relay uses the native Mac tools only.")
    line(tailscale_name() is not None or None, "Tailscale (for secure access from anywhere)",
         "Install Tailscale on the Mac and phone, then `tailscale serve --bg 8765`.")
    return 0 if ok else 1


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.relay.agent</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string><string>-m</string><string>macagent</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>{cwd}</string>
  <key>EnvironmentVariables</key><dict>
    <key>RELAY_HOME</key><string>{home}</string>
    <key>PYTHONPATH</key><string>{cwd}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>{home}/relay.log</string>
  <key>StandardErrorPath</key><string>{home}/relay.log</string>
</dict></plist>
"""


def cmd_install_launchagent(settings: Settings) -> None:
    if sys.platform != "darwin":
        sys.exit("LaunchAgents are a macOS feature.")
    path = Path("~/Library/LaunchAgents/dev.relay.agent.plist").expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    cwd = str(Path(__file__).resolve().parents[1])
    path.write_text(PLIST.format(python=sys.executable, cwd=cwd, home=settings.home))
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, text=True)
    print(f"Wrote {path}")
    print("Started." if r.returncode == 0 else f"launchctl: {r.stderr.strip()}")
    print(textwrap.dedent(f"""
        Relay now starts at login and restarts if it stops. Logs: {settings.home / 'relay.log'}
        Grant Screen Recording and Accessibility to {sys.executable} (the LaunchAgent runs it directly),
        and keep API keys in {settings.home / 'env'}."""))


def cmd_serve(settings: Settings) -> None:
    import uvicorn

    from .agent import Agent
    from .filters import Filter
    from .host import make_host
    from .machines import MachineRegistry
    from .models import FailoverAdapter, ModelManager
    from .notify import Ntfy
    from .router import Router, WebChannel
    from .server import create_app
    from .store import Store
    from .triggers import Triggers
    from .watchdog import Watchdog

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    store = Store(str(settings.db_path))
    host = None if settings.local_host == "none" else make_host(settings.fake_host, settings.screen_max_edge)
    machines = MachineRegistry(store, host)  # this Mac (if any) + daemons on other machines
    # Which provider and models run is chosen in the app (Settings → Models); on a spent key, fail over.
    models = ModelManager(settings, store)
    models.apply_saved()
    light = lambda: FailoverAdapter(models, "fast")  # noqa: E731
    # Worker layer: strongest model, isolated context per task, parallel, a screen lease per machine.
    agent = Agent(settings, store, machines, lambda: FailoverAdapter(models, "main"), light_factory=light)
    # Event triggers → two-stage filter (code, then policies on a light model) → router.
    triggers = Triggers(store, host, settings) if host else None
    router = Router(settings, store, agent, light, triggers, Filter(store, light), models=models)
    if triggers:
        triggers.router = router
    # The Watchdog runs on its own thread and loop, outside the main system.
    watchdog = Watchdog(settings, store, agent, light, interval_s=settings.watchdog_interval_s,
                        stall_s=settings.watchdog_stall_s, restart_on_hang=settings.watchdog_restart)
    ntfy = Ntfy(settings.ntfy_server, settings.ntfy_topic) if settings.ntfy_topic else None
    if ntfy:
        agent.notifiers.append(ntfy)
    router.add_channel(WebChannel(store, agent, ntfy))
    bot = None
    if settings.telegram_token:
        from .telegram import TelegramBot

        ts = tailscale_name()
        app_url = os.environ.get("RELAY_PUBLIC_URL") or (f"https://{ts}" if ts else None)
        bot = TelegramBot(settings.telegram_token, agent, store, router=router, models=models, app_url=app_url,
                          public=os.environ.get("RELAY_PUBLIC") == "1" or bool(os.environ.get("RELAY_PUBLIC_URL")))
        agent.notifiers.append(bot)
        router.add_channel(bot)

    caffeinate = None
    if settings.keep_awake and sys.platform == "darwin" and shutil.which("caffeinate"):
        # Keep the Mac awake (not the display) while Relay runs, so tasks and phone access keep working.
        caffeinate = subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())])

    from .llm import keep_warm

    def background():
        return ([bot.run()] if bot else []) + [keep_warm(settings)]

    app = create_app(settings, store, agent, background, router=router, triggers=triggers, watchdog=watchdog,
                     models=models)
    from .auth import list_devices

    print(f"Relay · machines {', '.join(machines.names()) or 'none yet'} · router {settings.router_model} · "
          f"workers {settings.model} ×{settings.max_workers} · policy {settings.policy}")
    if not list_devices(store):
        cmd_pair(settings)
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    finally:
        if caffeinate:
            caffeinate.terminate()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m macagent", description="Relay — control your Mac from your phone")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the agent and the phone app server")
    s.add_argument("--host", help="bind address (default 127.0.0.1; 0.0.0.0 for LAN access)")
    s.add_argument("--port", type=int)
    s.add_argument("--fake-host", action="store_true", help="virtual desktop instead of this machine")
    s.add_argument("--policy", choices=["careful", "balanced", "autonomous"])
    s.add_argument("--provider", choices=["anthropic", "deepseek", "openai", "openrouter", "gemini", "local"])
    s.add_argument("--model")
    sub.add_parser("pair", help="print a one-time code to pair a phone or Telegram chat")
    sub.add_parser("doctor", help="check permissions and configuration")
    sub.add_parser("install-launchagent", help="start Relay at login and keep it running")
    d = sub.add_parser("daemon", help="serve this machine to a Relay server (keeps no session state)")
    d.add_argument("--server", required=True, help="the Relay server URL, e.g. https://mini.tailnet.ts.net")
    d.add_argument("--name", default=socket.gethostname().split(".")[0], help="this machine's name")
    d.add_argument("--code", help="pairing code (first run only)")
    d.add_argument("--fake-host", action="store_true")
    args = p.parse_args(argv)

    if getattr(args, "provider", None):
        os.environ["RELAY_PROVIDER"] = args.provider
    settings = Settings()
    for attr in ("host", "port", "policy", "model"):
        if getattr(args, attr, None):
            setattr(settings, attr, getattr(args, attr))
    if getattr(args, "fake_host", False):
        settings.fake_host = True

    if args.cmd == "serve":
        cmd_serve(settings)
    elif args.cmd == "pair":
        cmd_pair(settings)
    elif args.cmd == "doctor":
        sys.exit(cmd_doctor(settings))
    elif args.cmd == "install-launchagent":
        cmd_install_launchagent(settings)
    elif args.cmd == "daemon":
        from .machines import run_daemon

        asyncio.run(run_daemon(args.server, args.name, args.code, fake=args.fake_host, max_edge=settings.screen_max_edge))


if __name__ == "__main__":
    main()

__all__ = ["main", "asyncio"]
