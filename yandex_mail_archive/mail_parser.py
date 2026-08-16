"""Decode MIME messages into a local-archive friendly structure."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterable


_UNSAFE_FILENAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class Attachment:
    filename: str
    content_type: str
    content_id: str
    payload: bytes
    inline: bool = False


@dataclass
class ParsedMessage:
    uid: str
    subject: str
    from_addr: str
    to_addr: str
    cc_addr: str
    date: dt.datetime | None
    date_raw: str
    text_body: str
    html_body: str
    attachments: list[Attachment] = field(default_factory=list)
    message_id: str = ""
    raw: bytes = b""


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        decoded = str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError, ValueError):
        decoded = value
    return _WHITESPACE.sub(" ", decoded).strip()


def parse_email_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def safe_filename(name: str, fallback: str = "file") -> str:
    cleaned = _UNSAFE_FILENAME.sub("_", name or "").strip(" ._")
    cleaned = cleaned.replace("..", "_")
    if not cleaned:
        cleaned = fallback
    return cleaned[:120]


def mailbox_dirname(address: str) -> str:
    return safe_filename(address.replace("@", "_at_"), fallback="mailbox")


def parse_raw_message(raw: bytes, uid: str) -> ParsedMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    html_body, text_body, attachments = _extract_bodies(message)
    date_raw = decode_header_value(message.get("Date"))
    return ParsedMessage(
        uid=str(uid),
        subject=decode_header_value(message.get("Subject")) or "(без темы)",
        from_addr=decode_header_value(message.get("From")),
        to_addr=decode_header_value(message.get("To")),
        cc_addr=decode_header_value(message.get("Cc")),
        date=parse_email_date(message.get("Date")),
        date_raw=date_raw,
        text_body=text_body.strip(),
        html_body=html_body,
        attachments=attachments,
        message_id=decode_header_value(message.get("Message-ID")),
        raw=raw,
    )


def snippet_from(parsed: ParsedMessage, limit: int = 140) -> str:
    source = parsed.text_body or _strip_tags(parsed.html_body)
    source = _WHITESPACE.sub(" ", source).strip()
    if len(source) <= limit:
        return source
    return source[: limit - 1].rstrip() + "…"


def _extract_bodies(message: Message) -> tuple[str, str, list[Attachment]]:
    html_parts: list[str] = []
    text_parts: list[str] = []
    attachments: list[Attachment] = []
    used_names: set[str] = set()

    parts: Iterable[Message]
    if message.is_multipart():
        parts = (part for part in message.walk() if not part.is_multipart())
    else:
        parts = (message,)

    for index, part in enumerate(parts, start=1):
        content_type = (part.get_content_type() or "application/octet-stream").lower()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        decoded_name = decode_header_value(filename) if filename else ""
        content_id = decode_header_value(part.get("Content-ID")).strip("<>")
        payload = _decoded_payload(part)

        is_attachment = disposition == "attachment" or bool(decoded_name)
        is_inline_image = bool(content_id) and content_type.startswith("image/")

        if is_attachment or is_inline_image:
            fallback = f"part-{index}"
            if content_type.startswith("image/") and not decoded_name:
                fallback += ".png"
            unique_name = _unique_name(safe_filename(decoded_name, fallback), used_names)
            attachments.append(
                Attachment(
                    filename=unique_name,
                    content_type=content_type,
                    content_id=content_id,
                    payload=payload,
                    inline=is_inline_image and disposition != "attachment",
                )
            )
            if disposition == "attachment":
                continue

        if content_type == "text/plain" and disposition != "attachment":
            text_parts.append(_decode_text(payload, part))
        elif content_type == "text/html" and disposition != "attachment":
            html_parts.append(_decode_text(payload, part))

    html_body = html_parts[0] if html_parts else ""
    text_body = "\n\n".join(part for part in text_parts if part.strip())
    if html_body:
        html_body = sanitize_html(html_body)
        html_body = _rewrite_cid_links(html_body, attachments)
    return html_body, text_body, attachments


def _decoded_payload(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw.encode("utf-8", errors="replace")
        return b""
    return payload


def _decode_text(payload: bytes, part: Message) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _unique_name(name: str, used: set[str]) -> str:
    candidate = name
    stem, dot, ext = name.partition(".")
    counter = 2
    while candidate.lower() in used:
        if dot:
            candidate = f"{stem}-{counter}.{ext}"
        else:
            candidate = f"{name}-{counter}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def _rewrite_cid_links(html: str, attachments: list[Attachment]) -> str:
    for item in attachments:
        if not item.content_id:
            continue
        html = re.sub(
            rf"(?i)cid:{re.escape(item.content_id)}",
            item.filename,
            html,
        )
    return html


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)

    def get_text(self) -> str:
        return " ".join(self.chunks)


def _strip_tags(html: str) -> str:
    if not html:
        return ""
    stripper = _HTMLStripper()
    try:
        stripper.feed(html)
        stripper.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return stripper.get_text()


_ALLOWED_TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "table": {"border"},
}
_SAFE_URL = re.compile(r"^(https?:|mailto:|cid:|/|\./|#)", re.IGNORECASE)


class _HTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_start(tag, attrs, close=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_start(tag, attrs, close=True)

    def handle_endtag(self, tag: str) -> None:
        if tag in _ALLOWED_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.out.append(_escape_text(data))

    def _emit_start(self, tag: str, attrs: list[tuple[str, str | None]], close: bool) -> None:
        if tag not in _ALLOWED_TAGS:
            return
        allowed = _ALLOWED_ATTRS.get(tag, set())
        clean_attrs: list[str] = []
        for name, value in attrs:
            key = name.lower()
            if key.startswith("on") or key not in allowed:
                continue
            text = value or ""
            if key in {"href", "src"} and not _SAFE_URL.match(text.strip()):
                continue
            clean_attrs.append(f'{key}="{_escape_attr(text)}"')
        attr_str = (" " + " ".join(clean_attrs)) if clean_attrs else ""
        slash = " /" if close or tag in {"br", "hr", "img"} else ""
        self.out.append(f"<{tag}{attr_str}{slash}>")


def sanitize_html(html: str) -> str:
    if not html:
        return ""
    sanitizer = _HTMLSanitizer()
    try:
        sanitizer.feed(html)
        sanitizer.close()
    except Exception:
        return _escape_text(_strip_tags(html))
    return "".join(sanitizer.out)


def _escape_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
