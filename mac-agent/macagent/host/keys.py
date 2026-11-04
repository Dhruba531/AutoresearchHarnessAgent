"""Key names → macOS virtual key codes (ANSI layout) and modifier flags.

The computer-use toolset sends xdotool-style names ("Return", "ctrl+s",
"super+Tab"); `super`/`cmd`/`meta` mean the Command key on a Mac.
"""

from __future__ import annotations

KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "`": 50,
    "return": 36, "enter": 36, "kp_enter": 76, "tab": 48, "space": 49, "backspace": 51, "delete": 51,
    "escape": 53, "esc": 53, "forwarddelete": 117, "home": 115, "end": 119, "page_up": 116, "pageup": 116,
    "prior": 116, "page_down": 121, "pagedown": 121, "next": 121, "left": 123, "right": 124, "down": 125,
    "up": 126, "capslock": 57, "caps_lock": 57,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111,
    "volumeup": 72, "volumedown": 73, "mute": 74,
    # modifiers as keys (for hold_key / bare presses)
    "cmd": 55, "command": 55, "super": 55, "meta": 55, "shift": 56, "alt": 58, "option": 58,
    "ctrl": 59, "control": 59, "fn": 63,
}

ALIASES = {
    "minus": "-", "equal": "=", "plus": "=", "comma": ",", "period": ".", "slash": "/", "backslash": "\\",
    "semicolon": ";", "apostrophe": "'", "quote": "'", "grave": "`", "bracketleft": "[", "bracketright": "]",
    "ret": "return", "del": "forwarddelete", "bksp": "backspace",
}

# CGEventFlags masks
FLAGS = {
    "shift": 1 << 17, "ctrl": 1 << 18, "control": 1 << 18, "alt": 1 << 19, "option": 1 << 19,
    "cmd": 1 << 20, "command": 1 << 20, "super": 1 << 20, "meta": 1 << 20, "win": 1 << 20, "fn": 1 << 23,
}


def parse_combo(combo: str) -> tuple[int, int]:
    """'super+shift+t' → (keycode, flags). Raises ValueError for unknown keys."""
    parts = [p.strip() for p in combo.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combination")
    *mods, key = parts
    flags = 0
    for m in mods:
        mask = FLAGS.get(m.lower())
        if mask is None:
            raise ValueError(f"unknown modifier '{m}'")
        flags |= mask
    name = key if len(key) == 1 else key.lower()
    name = ALIASES.get(name.lower(), name)
    if len(name) == 1 and name.isalpha() and name.isupper():
        flags |= FLAGS["shift"]
        name = name.lower()
    code = KEYCODES.get(name) if len(name) == 1 else KEYCODES.get(name.lower())
    if code is None:
        raise ValueError(f"unknown key '{key}'")
    return code, flags


def parse_modifiers(text: str | None) -> int:
    flags = 0
    for m in (text or "").replace(" ", "").split("+"):
        if m:
            mask = FLAGS.get(m.lower())
            if mask is None:
                raise ValueError(f"unknown modifier '{m}'")
            flags |= mask
    return flags
