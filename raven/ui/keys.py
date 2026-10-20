"""Portable key names shared by decoding, binding validation, and reports."""

_ESCAPE_KEYS = {
    "\x1b[1;5D": "ctrl-left",
    "\x1b[1;5C": "ctrl-right",
    "\x1b[1;3D": "alt-left",
    "\x1b[1;3C": "alt-right",
    "\x1bb": "alt-b",
    "\x1bf": "alt-f",
    "\x1bd": "alt-d",
    "\x1bu": "alt-u",
    "\x1br": "alt-r",
    "\x1b\x7f": "alt-backspace",
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1b[C": "right",
    "\x1b[D": "left",
}
_ESCAPE_KEYS.update(
    {
        "\x1b[H": "home",
        "\x1b[F": "end",
        "\x1b[Z": "btab",
        "\x1b[1~": "home",
        "\x1b[4~": "end",
        "\x1b[7~": "home",
        "\x1b[8~": "end",
        "\x1b[3~": "delete",
        "\x1b[5~": "pgup",
        "\x1b[6~": "pgdn",
        "\x1bOA": "up",
        "\x1bOB": "down",
        "\x1bOC": "right",
        "\x1bOD": "left",
        "\x1bOH": "home",
        "\x1bOF": "end",
        "\x1bOP": "f1",
        "\x1bOQ": "f2",
        "\x1bOR": "f3",
        "\x1bOS": "f4",
    }
)
_ESCAPE_KEYS.update(
    {
        f"\x1b[{number}~": f"f{index}"
        for index, number in enumerate((15, 17, 18, 19, 20, 21, 23, 24), 5)
    }
)
_ESCAPE_KEYS.update(
    {"\x1b" + chr(code): "alt-" + chr(code) for code in range(ord("a"), ord("z") + 1)}
)
for _modifier, _number in (("alt", 3), ("ctrl", 5)):
    for _suffix, _key in (
        ("A", "up"),
        ("B", "down"),
        ("C", "right"),
        ("D", "left"),
        ("H", "home"),
        ("F", "end"),
    ):
        _ESCAPE_KEYS[f"\x1b[1;{_number}{_suffix}"] = f"{_modifier}-{_key}"
    for _code, _key in ((3, "delete"), (5, "pgup"), (6, "pgdn")):
        _ESCAPE_KEYS[f"\x1b[{_code};{_number}~"] = f"{_modifier}-{_key}"
_ESCAPE_KEYS["\x1b[127;5u"] = "ctrl-backspace"
del _modifier, _number, _suffix, _key, _code

KEY_LABELS = {
    "up": "↑",
    "down": "↓",
    "pgup": "PgUp",
    "pgdn": "PgDn",
    "home": "Home",
    "end": "End",
    "tab": "Tab",
    "btab": "S-Tab",
    "enter": "Enter",
    "esc": "Esc",
    "space": "Space",
}
KEY_LABELS_ASCII = dict(KEY_LABELS, up="Up", down="Down")
