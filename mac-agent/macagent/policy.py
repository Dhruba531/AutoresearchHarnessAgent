"""Risk tiers and the approval policy — enforced in code, not left to the model.

Every tool call is classified into a tier:

  read     looks without changing anything (screenshot, list files, read mail)
  act      moves the mouse, types, opens an app — reversible, everyday
  change   creates, edits, moves or trashes data (files, events, notes, shell)
  send     reaches another person (email, iMessage)
  danger   could do serious damage or leak secrets (sudo, rm -r, keys, disks)

and the policy decides which tiers wait for a human:

  careful     everything except read
  balanced    change, send, danger            (default)
  autonomous  send, danger

Some calls are refused outright whatever the policy (touching Relay's own
data directory, which holds the device-token database).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

TIERS = ("read", "act", "change", "send", "danger")
POLICIES = {
    "careful": {"act", "change", "send", "danger"},
    "balanced": {"change", "send", "danger"},
    "autonomous": {"send", "danger"},
}


def needs_approval(policy: str, tier: str) -> bool:
    return tier in POLICIES.get(policy, POLICIES["balanced"])


# -- shell --------------------------------------------------------------------

DANGER_SHELL = [
    r"\bsudo\b", r"\bsu\s", r"\brm\s+(-\w*[rRf]\w*\s+)+", r"\bmkfs", r"\bdiskutil\s+(erase|partition|zero|secure|apfs\s+delete)",
    r"\bdd\s+if=", r"\b(shutdown|reboot|halt)\b", r"\blaunchctl\s+(unload|bootout|remove|disable)", r"\bcsrutil\b",
    r"\bsecurity\s+(delete|dump|find-generic-password|find-internet-password)", r"\bch(mod|own)\s+-R\b",
    r"\bkillall\b", r":\(\)\s*\{", r">\s*/dev/(disk|rdisk)", r"(curl|wget)[^|]*\|\s*(ba|z)?sh\b",
    r"\bdefaults\s+delete\b", r"\bspctl\b", r"\btmutil\s+(delete|disable)", r"\bgit\s+push\s+.*--force",
    r"\bnvram\b", r"\bfdesetup\b", r"\bsqlite3\b.*\.relay", r"\bscutil\b", r"(?:^|[;&|]\s*)passwd\b",
]
READ_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "pwd", "whoami", "date", "cal", "uptime", "df", "du", "ps", "mdfind", "mdls",
    "grep", "rg", "egrep", "which", "type", "echo", "printf", "file", "stat", "sw_vers", "uname", "system_profiler",
    "ioreg", "sysctl", "ifconfig", "hostname", "id", "env", "printenv", "sort", "uniq", "cut", "tr", "awk", "sed",
    "less", "more", "tree", "basename", "dirname", "realpath", "pmset", "networksetup", "defaults", "git", "brew",
    "jq", "ping", "dig", "nslookup", "lsof", "top", "vm_stat", "diskutil", "log", "plutil", "shasum", "md5", "find",
    "xattr", "mdutil", "pbpaste", "say", "true", "test", "[",
}
READ_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files", "blame", "tag"},
    "brew": {"list", "info", "outdated", "search", "config", "doctor", "deps", "leaves"},
    "defaults": {"read", "domains", "read-type"},
    "pmset": {"-g"},
    "diskutil": {"list", "info"},
    "networksetup": {"-listallnetworkservices", "-getairportnetwork", "-getinfo", "-listallhardwareports"},
    "log": {"show"},
    "mdutil": {"-s"},
    "xattr": {"-l", "-p"},
}


def classify_shell(command: str) -> str:
    for pat in DANGER_SHELL:
        if re.search(pat, command):
            return "danger"
    if re.search(r"(^|[^<>&|0-9])>{1,2}(?!&)", command) or "$(" in command or "`" in command or "system(" in command:
        return "change"  # redirection into files or command substitution
    for segment in re.split(r"\|\||&&|;|\||\n", command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            return "change"
        while words and re.match(r"^\w+=", words[0]):  # FOO=bar cmd
            words = words[1:]
        if not words:
            continue
        cmd = Path(words[0]).name
        if cmd not in READ_COMMANDS:
            return "change"
        subs = READ_SUBCOMMANDS.get(cmd)
        if subs is not None and (len(words) < 2 or words[1] not in subs):
            return "change"
        if cmd == "find" and any(w in {"-delete", "-exec", "-execdir", "-ok"} for w in words):
            return "change"
        if cmd == "sed" and any(w.startswith("-i") for w in words):
            return "change"
        if cmd == "say":
            return "act"
    return "read"


# -- AppleScript ---------------------------------------------------------------

def classify_script(script: str) -> str:
    s = script.lower()
    if "with administrator privileges" in s or re.search(r"do shell script[^\n]*\bsudo\b", s):
        return "danger"
    if "do shell script" in s:
        inner = re.findall(r'do shell script\s+"([^"]*)"', script)
        tiers = [classify_shell(c) for c in inner] or ["change"]
        worst = max(tiers, key=TIERS.index)
        if worst != "read":
            return worst
    if re.search(r"\bsend\b", s) and re.search(r'"(mail|messages|outlook|slack|whatsapp|telegram)"|application\("(mail|messages)"\)', s):
        return "send"
    if re.search(r"\b(make new|delete|move|duplicate|empty trash|save|set\s+\w|close|quit|restart|shut down|log out|"
                 r"keystroke|key code|click|push\(|\.delete\(|\.make\(|erase)\b", s):
        return "change"
    return "read"


# -- paths -----------------------------------------------------------------------

SENSITIVE = [".ssh", ".gnupg", ".aws", ".config/gcloud", ".kube", "library/keychains", ".netrc", ".npmrc",
             ".pypirc", ".docker/config.json", "library/cookies", "library/application support/google/chrome",
             "library/safari", "library/messages", "library/mail", ".env", "id_rsa", "id_ed25519", "1password"]


def resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def is_relay_home(path: Path, home: Path) -> bool:
    home = home.resolve()
    return path == home or home in path.parents


def path_tier(path: Path, base: str) -> str:
    p = str(path).lower()
    if any(s in p for s in SENSITIVE):
        return "danger"
    return base
