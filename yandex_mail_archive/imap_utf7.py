"""IMAP modified UTF-7 helpers for mailbox folder names."""

from __future__ import annotations

import base64
import binascii
import re


def decode_modified_utf7(value: str) -> str:
    """Decode an IMAP mailbox name from modified UTF-7 into Unicode."""
    if not value:
        return value

    parts: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        ampersand = value.find("&", index)
        if ampersand < 0:
            parts.append(value[index:])
            break
        parts.append(value[index:ampersand])
        if ampersand + 1 < length and value[ampersand + 1] == "-":
            parts.append("&")
            index = ampersand + 2
            continue
        dash = value.find("-", ampersand + 1)
        if dash < 0:
            parts.append(value[ampersand:])
            break
        encoded = value[ampersand + 1 : dash].replace(",", "/")
        if not encoded:
            parts.append("&")
            index = dash + 1
            continue
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        try:
            decoded = base64.b64decode(encoded + padding, validate=False)
            parts.append(decoded.decode("utf-16-be"))
        except (binascii.Error, UnicodeDecodeError):
            parts.append(value[ampersand : dash + 1])
        index = dash + 1
    return "".join(parts)


_LIST_LINE = re.compile(
    r"""^\((?P<flags>.*?)\)\s+
        (?P<delim>NIL|\"(?P<delim_quoted>[^\"]*)\")\s+
        (?P<name>.+)$""",
    re.VERBOSE | re.IGNORECASE,
)


def parse_imap_list_line(line: str) -> tuple[list[str], str, str] | None:
    """Parse one IMAP LIST response into (flags, delimiter, mailbox name)."""
    text = line.strip()
    if text.startswith("* "):
        text = text[2:]
    if text.upper().startswith("LIST "):
        text = text[5:]
    match = _LIST_LINE.match(text)
    if not match:
        return None
    flags = [flag.strip() for flag in match.group("flags").split() if flag.strip()]
    delim = match.group("delim_quoted")
    if delim is None:
        delim = ""
    name = match.group("name").strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1]
    return flags, delim, name
