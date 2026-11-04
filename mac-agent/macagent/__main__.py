"""Command line: `python -m macagent <serve|pair|doctor|install-launchagent>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
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
        line(subprocess.run(["pmset", "-g"], capture_output=True).returncode == 0, "pmset available")
    key = {"anthropic": settings.anthropic_api_key, "deepseek": settings.deepseek_api_key,
           "openai": settings.openai_api_key}.get(settings.provider)
    line(bool(key), f"API key for {settings.provider}", f"Add it to {settings.home / 'env'} (chmod 600).")
    line(settings.can_see_screen, f"model {settings.model} can see the screen",
         "Only Claude models drive the screen; with DeepSeek/OpenAI Relay uses the native tools only.")
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
    from .host import make_host
    from .llm import make_adapter
    from .notify import Ntfy
    from .server import create_app
    from .store import Store

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    store = Store(str(settings.db_path))
    host = make_host(settings.fake_host, settings.screen_max_edge)
    agent = Agent(settings, store, host, lambda: make_adapter(settings))
    bot = None
    if settings.ntfy_topic:
        agent.notifiers.append(Ntfy(settings.ntfy_server, settings.ntfy_topic))
    if settings.telegram_token:
        from .telegram import TelegramBot

        bot = TelegramBot(settings.telegram_token, agent, store)
        agent.notifiers.append(bot)

    caffeinate = None
    if settings.keep_awake and sys.platform == "darwin" and shutil.which("caffeinate"):
        # Keep the Mac awake (not the display) while Relay runs, so tasks and phone access keep working.
        caffeinate = subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())])

    def background():
        return [bot.run()] if bot else []

    app = create_app(settings, store, agent, background)
    from .auth import list_devices

    print(f"Relay {host.name} host · {settings.provider}/{settings.model} · policy {settings.policy}")
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
    s.add_argument("--provider", choices=["anthropic", "deepseek", "openai"])
    s.add_argument("--model")
    sub.add_parser("pair", help="print a one-time code to pair a phone or Telegram chat")
    sub.add_parser("doctor", help="check permissions and configuration")
    sub.add_parser("install-launchagent", help="start Relay at login and keep it running")
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


if __name__ == "__main__":
    main()

__all__ = ["main", "asyncio"]
