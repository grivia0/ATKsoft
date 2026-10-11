"""Human-readable names for the key codes stored inside button/macro slots.

Two lookup tables, both keyed by the raw byte the firmware keeps:

* :data:`KEY_NAMES` -- HID Usage ID page 0x07 (Keyboard/Keypad).  A button
  slot's ``usage`` byte and a macro action's ``code`` byte are usage ids from
  this page.
* :data:`MOUSE_CODES` -- the five mouse actions a macro action may carry
  instead of a key (0xf1..0xf5, i.e. outside the keyboard page).

The names are the short display forms ("A", "Left Ctrl", "Page Up") so they
read well in a terminal and still round-trip through :func:`key_usage`.
"""

from __future__ import annotations

# ------------------------------------------------- HID Keyboard/Keypad page

KEY_NAMES: dict[int, str] = {
    0x00: "Reserved",
    0x01: "Error RollOver",
    0x02: "POST Fail",
    0x03: "Error Undefined",
}
KEY_NAMES.update({0x04 + i: chr(ord("a") + i) for i in range(26)})      # a .. z
KEY_NAMES.update({0x1E + i: str((i + 1) % 10) for i in range(10)})      # 1 .. 9, 0
KEY_NAMES.update({
    0x28: "Enter",
    0x29: "Escape",
    0x2A: "Backspace",
    0x2B: "Tab",
    0x2C: "Space",
    0x2D: "-",
    0x2E: "=",
    0x2F: "[",
    0x30: "]",
    0x31: "\\",
    0x32: "Non-US #",
    0x33: ";",
    0x34: "'",
    0x35: "`",
    0x36: ",",
    0x37: ".",
    0x38: "/",
    0x39: "Caps Lock",
})
KEY_NAMES.update({0x3A + i: f"F{i + 1}" for i in range(12)})            # F1 .. F12
KEY_NAMES.update({
    0x46: "Print Screen",
    0x47: "Scroll Lock",
    0x48: "Pause",
    0x49: "Insert",
    0x4A: "Home",
    0x4B: "Page Up",
    0x4C: "Delete",
    0x4D: "End",
    0x4E: "Page Down",
    0x4F: "Right Arrow",
    0x50: "Left Arrow",
    0x51: "Down Arrow",
    0x52: "Up Arrow",
})
KEY_NAMES.update({
    0x53: "Num Lock",
    0x54: "Keypad /",
    0x55: "Keypad *",
    0x56: "Keypad -",
    0x57: "Keypad +",
    0x58: "Keypad Enter",
    0x59: "Keypad 1",
    0x5A: "Keypad 2",
    0x5B: "Keypad 3",
    0x5C: "Keypad 4",
    0x5D: "Keypad 5",
    0x5E: "Keypad 6",
    0x5F: "Keypad 7",
    0x60: "Keypad 8",
    0x61: "Keypad 9",
    0x62: "Keypad 0",
    0x63: "Keypad .",
    0x64: "Non-US \\",
    0x65: "Application",
    0x66: "Power",
    0x67: "Keypad =",
})
KEY_NAMES.update({0x68 + i: f"F{i + 13}" for i in range(12)})           # F13 .. F24
KEY_NAMES.update({
    0x74: "Execute",
    0x75: "Help",
    0x76: "Menu",
    0x77: "Select",
    0x78: "Stop",
    0x79: "Again",
    0x7A: "Undo",
    0x7B: "Cut",
    0x7C: "Copy",
    0x7D: "Paste",
    0x7E: "Find",
    0x7F: "Mute",
    0x80: "Volume Up",
    0x81: "Volume Down",
    0x82: "Locking Caps Lock",
    0x83: "Locking Num Lock",
    0x84: "Locking Scroll Lock",
    0x85: "Keypad ,",
    0x86: "Keypad = (AS/400)",
})
KEY_NAMES.update({
    0x87: "International 1",
    0x88: "International 2",
    0x89: "International 3",
    0x8A: "International 4",
    0x8B: "International 5",
    0x8C: "International 6",
    0x8D: "International 7",
    0x8E: "International 8",
    0x8F: "International 9",
    0x90: "LANG 1",
    0x91: "LANG 2",
    0x92: "LANG 3",
    0x93: "LANG 4",
    0x94: "LANG 5",
    0x95: "LANG 6",
    0x96: "LANG 7",
    0x97: "LANG 8",
    0x98: "LANG 9",
    0x99: "Alternate Erase",
    0x9A: "SysReq / Attention",
    0x9B: "Cancel",
    0x9C: "Clear",
    0x9D: "Prior",
    0x9E: "Return",
    0x9F: "Separator",
    0xA0: "Out",
    0xA1: "Oper",
    0xA2: "Clear / Again",
    0xA3: "CrSel / Props",
    0xA4: "ExSel",
    0xB0: "Keypad 00",
    0xB1: "Keypad 000",
    0xB2: "Thousands Separator",
    0xB3: "Decimal Separator",
    0xB4: "Currency Unit",
    0xB5: "Currency Sub-unit",
    0xB6: "Keypad (",
    0xB7: "Keypad )",
    0xB8: "Keypad {",
    0xB9: "Keypad }",
})
# The 0xba..0xdf block is the Keypad/calculator set.  A mouse cannot send most
# of it, but a macro slot can store it, so the names are kept for display.
KEY_NAMES.update({
    0xBA: "Keypad Tab",
    0xBB: "Keypad Backspace",
    0xBC: "Keypad A",
    0xBD: "Keypad B",
    0xBE: "Keypad C",
    0xBF: "Keypad D",
    0xC0: "Keypad E",
    0xC1: "Keypad F",
    0xC2: "Keypad XOR",
    0xC3: "Keypad ^",
    0xC4: "Keypad %",
    0xC5: "Keypad <",
    0xC6: "Keypad >",
    0xC7: "Keypad &",
    0xC8: "Keypad &&",
    0xC9: "Keypad |",
    0xCA: "Keypad ||",
    0xCB: "Keypad :",
    0xCC: "Keypad #",
    0xCD: "Keypad Space",
    0xCE: "Keypad @",
    0xCF: "Keypad !",
    0xD0: "Keypad Memory Store",
    0xD1: "Keypad Memory Recall",
    0xD2: "Keypad Memory Clear",
    0xD3: "Keypad Memory Add",
    0xD4: "Keypad Memory Subtract",
    0xD5: "Keypad Memory Multiply",
    0xD6: "Keypad Memory Divide",
    0xD7: "Keypad +/-",
    0xD8: "Keypad Clear",
    0xD9: "Keypad Clear Entry",
    0xDA: "Keypad Binary",
    0xDB: "Keypad Octal",
    0xDC: "Keypad Decimal",
    0xDD: "Keypad Hexadecimal",
    0xE0: "Left Ctrl",
    0xE1: "Left Shift",
    0xE2: "Left Alt",
    0xE3: "Left GUI",
    0xE4: "Right Ctrl",
    0xE5: "Right Shift",
    0xE6: "Right Alt",
    0xE7: "Right GUI",
})

# ------------------------------------------------------- macro mouse codes

# Macro actions may carry a mouse action instead of a key.  The firmware uses
# the same five values the vendor GUI's macro editor offers.
MOUSE_CODES: dict[int, str] = {
    0xF1: "Left Click",
    0xF2: "Right Click",
    0xF3: "Middle Click",
    0xF4: "Side Back",
    0xF5: "Side Forward",
}
# ------------------------------------------------------------- name lookup

# Spoken names for things people actually type.  Applied last so they win over
# the auto-generated table (the arrow keys, for instance, are aliased to the
# bare "left"/"right" a user is likely to try first).
ALIASES: dict[str, int] = {
    "ctrl": 0xE0,
    "control": 0xE0,
    "lctrl": 0xE0,
    "left ctrl": 0xE0,
    "shift": 0xE1,
    "lshift": 0xE1,
    "left shift": 0xE1,
    "alt": 0xE2,
    "lalt": 0xE2,
    "left alt": 0xE2,
    "win": 0xE3,
    "super": 0xE3,
    "meta": 0xE3,
    "lgui": 0xE3,
    "left gui": 0xE3,
    "rctrl": 0xE4,
    "right ctrl": 0xE4,
    "rshift": 0xE5,
    "right shift": 0xE5,
    "ralt": 0xE6,
    "right alt": 0xE6,
    "rgui": 0xE7,
    "right gui": 0xE7,
    "esc": 0x29,
    "escape": 0x29,
    "enter": 0x28,
    "return": 0x28,
    "space": 0x2C,
    "spacebar": 0x2C,
    "backspace": 0x2A,
    "tab": 0x2B,
    "capslock": 0x39,
    "caps": 0x39,
    "del": 0x4C,
    "delete": 0x4C,
    "ins": 0x49,
    "insert": 0x49,
    "home": 0x4A,
    "end": 0x4D,
    "pgup": 0x4B,
    "pageup": 0x4B,
    "pgdn": 0x4E,
    "pagedown": 0x4E,
    "up": 0x52,
    "up arrow": 0x52,
    "down": 0x51,
    "down arrow": 0x51,
    "left": 0x50,
    "left arrow": 0x50,
    "right": 0x4F,
    "right arrow": 0x4F,
    "prtsc": 0x46,
    "print screen": 0x46,
    "scrolllock": 0x47,
    "pause": 0x48,
    "app": 0x65,
    "application": 0x65,
    "menu": 0x76,
    "mute": 0x7F,
    "volup": 0x80,
    "volume up": 0x80,
    "voldown": 0x81,
    "volume down": 0x81,
    "lmb": 0xF1,
    "left click": 0xF1,
    "rmb": 0xF2,
    "right click": 0xF2,
    "mmb": 0xF3,
    "middle click": 0xF3,
    "back": 0xF4,
    "forward": 0xF5,
}

# Reverse table for :func:`key_usage`.  Keys are lower-case display names plus
# the aliases above.
KEY_USAGE_BY_NAME: dict[str, int] = {
    name.lower(): code for code, name in KEY_NAMES.items()
}
KEY_USAGE_BY_NAME.update({name.lower(): code for code, name in MOUSE_CODES.items()})
KEY_USAGE_BY_NAME.update(ALIASES)


def key_name(code: int) -> str:
    """Display name for a usage id or a macro mouse code."""
    if code in MOUSE_CODES:
        return MOUSE_CODES[code]
    return KEY_NAMES.get(code, f"code {code:#04x}")


def key_usage(name: str | int) -> int:
    """Reverse of :func:`key_name`; accepts a number as well ("0x04", "4").

    Raises :class:`ValueError` for an unknown name or an out-of-range value.
    """
    if isinstance(name, int):
        if not 0 <= name <= 0xFF:
            raise ValueError(f"key code {name} does not fit in one byte")
        return name
    text = str(name).strip()
    if text.lower() in KEY_USAGE_BY_NAME:
        return KEY_USAGE_BY_NAME[text.lower()]
    try:
        value = int(text, 0)
    except ValueError:
        raise ValueError(
            f"unknown key {name!r}: use a name such as 'a', 'Left Ctrl' or a "
            f"code such as 0x04"
        ) from None
    return key_usage(value)
