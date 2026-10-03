"""Platform-independent parsing of RegisterHotKey combinations."""
from dataclasses import dataclass


MODIFIERS = {"alt": (1, "Alt"), "ctrl": (2, "Ctrl"), "control": (2, "Ctrl"),
             "shift": (4, "Shift"), "win": (8, "Win"), "windows": (8, "Win")}
NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "escape": 0x1B,
              "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
              "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
              "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    key: int
    label: str


def parse_hotkey(value: str) -> Hotkey:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ショートカットは Ctrl+Alt+K のような文字列で指定してください")
    modifiers = 0
    key = None
    key_label = ""
    for token in value.split("+"):
        token = token.strip().lower()
        if token in MODIFIERS:
            flag, _ = MODIFIERS[token]
            if modifiers & flag:
                raise ValueError("ショートカットの修飾キーが重複しています")
            modifiers |= flag
            continue
        if key is not None:
            raise ValueError("ショートカットの主キーは1つだけ指定してください")
        if len(token) == 1 and token in "abcdefghijklmnopqrstuvwxyz0123456789":
            key = ord(token.upper())
        elif token in NAMED_KEYS:
            key = NAMED_KEYS[token]
        elif token.startswith("f") and token[1:].isdigit() and 1 <= int(token[1:]) <= 24:
            number = int(token[1:])
            if number == 12:
                raise ValueError("F12はWindowsのデバッガー用予約キーのため使用できません")
            key = 0x70 + number - 1
            token = f"f{number}"
        else:
            raise ValueError(f"未対応のショートカットキーです: {token!r}")
        key_label = token.upper() if len(token) == 1 or token.startswith("f") and token[1:].isdigit() else token.title()
    if key is None:
        raise ValueError("ショートカットに主キーを指定してください")
    if not modifiers and not 0x70 <= key <= 0x87:
        raise ValueError("文字キーなどにはCtrl・Alt・Shift・Winのいずれかを組み合わせてください")
    labels = [label for flag, label in ((2, "Ctrl"), (1, "Alt"), (4, "Shift"), (8, "Win"))
              if modifiers & flag]
    return Hotkey(modifiers, key, "+".join(labels + [key_label]))
