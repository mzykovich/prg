from __future__ import annotations

import base64
import unittest
from email.message import EmailMessage

from yandex_mail_archive.imap_utf7 import decode_modified_utf7, parse_imap_list_line
from yandex_mail_archive.mail_parser import (
    parse_raw_message,
    safe_filename,
    sanitize_html,
    snippet_from,
)


def encode_modified_utf7(value: str) -> str:
    result: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        raw = "".join(buffer).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").replace("/", ",").rstrip("=")
        result.append("&" + encoded + "-")
        buffer.clear()

    for char in value:
        code = ord(char)
        if 0x20 <= code <= 0x7E:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            buffer.append(char)
    flush()
    return "".join(result)


class ImapUtf7Tests(unittest.TestCase):
    def test_roundtrip_russian_folder(self) -> None:
        original = "Отправленные"
        encoded = encode_modified_utf7(original)
        self.assertTrue(encoded.startswith("&"))
        self.assertEqual(decode_modified_utf7(encoded), original)

    def test_ampersand(self) -> None:
        self.assertEqual(decode_modified_utf7("A&-B"), "A&B")

    def test_ascii_passthrough(self) -> None:
        self.assertEqual(decode_modified_utf7("INBOX"), "INBOX")

    def test_parse_list_line_inbox(self) -> None:
        parsed = parse_imap_list_line('(\\HasNoChildren) "/" INBOX')
        self.assertIsNotNone(parsed)
        flags, delim, name = parsed  # type: ignore[misc]
        self.assertIn("\\HasNoChildren", flags)
        self.assertEqual(delim, "/")
        self.assertEqual(name, "INBOX")

    def test_parse_quoted_and_star_prefix(self) -> None:
        parsed = parse_imap_list_line('* LIST (\\HasNoChildren \\Sent) "/" "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"')
        self.assertIsNotNone(parsed)
        _flags, _delim, name = parsed  # type: ignore[misc]
        self.assertEqual(decode_modified_utf7(name), "Отправленные")

    def test_skip_nil_delimiter(self) -> None:
        parsed = parse_imap_list_line('(\\Noselect) NIL "Archive"')
        self.assertIsNotNone(parsed)
        flags, delim, name = parsed  # type: ignore[misc]
        self.assertIn("\\Noselect", flags)
        self.assertEqual(delim, "")
        self.assertEqual(name, "Archive")


class MailParserTests(unittest.TestCase):
    def test_decodes_russian_headers_and_html(self) -> None:
        message = EmailMessage()
        message["From"] = "Иван <ivan@company.ru>"
        message["To"] = "Бухгалтерия <buh@company.ru>"
        message["Subject"] = "=?UTF-8?B?0KHRh9C10YIg0L3QsCDQvtC/0LvQsNGC0YM=?="
        message["Date"] = "Wed, 15 Jan 2025 12:34:56 +0300"
        message.set_content("Текст письма")
        message.add_alternative("<p>Текст письма</p><script>alert(1)</script>", subtype="html")
        parsed = parse_raw_message(message.as_bytes(), "42")
        self.assertEqual(parsed.subject, "Счет на оплату")
        self.assertIn("Иван", parsed.from_addr)
        self.assertIsNotNone(parsed.date)
        self.assertIn("Текст письма", parsed.text_body)
        self.assertIn("Текст письма", parsed.html_body)
        self.assertNotIn("<script>", parsed.html_body)
        self.assertIn("Текст", snippet_from(parsed))

    def test_saves_attachment_and_inline_image(self) -> None:
        message = EmailMessage()
        message["From"] = "a@company.ru"
        message["To"] = "b@company.ru"
        message["Subject"] = "Файлы"
        message.set_content("см. вложение")
        message.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="акт.pdf")
        message.add_attachment(
            b"\x89PNG\r\n",
            maintype="image",
            subtype="png",
            filename="logo.png",
            cid="<logo@cid>",
            disposition="inline",
        )
        parsed = parse_raw_message(message.as_bytes(), "7")
        names = {item.filename for item in parsed.attachments}
        self.assertIn("акт.pdf", names)
        self.assertTrue(any(item.content_id == "logo@cid" for item in parsed.attachments))

    def test_sanitize_strips_javascript(self) -> None:
        dirty = '<a href="javascript:alert(1)">x</a><img src="http://ok/a.png" onclick="steal()">'
        clean = sanitize_html(dirty)
        self.assertNotIn("javascript:", clean)
        self.assertNotIn("onclick", clean)
        self.assertIn("http://ok/a.png", clean)

    def test_parses_crlf_in_from_header(self) -> None:
        raw = (
            b'From: "Bad\r\n Name" <ivan@company.ru>\r\n'
            b"Subject: =?UTF-8?B?0KLQtdGB0YI=?=\r\n"
            b"Date: Thu, 16 Jan 2025 09:00:00 +0300\r\n"
            b"\r\n"
            b"Hello\r\n"
        )
        parsed = parse_raw_message(raw, "99")
        self.assertEqual(parsed.uid, "99")
        self.assertTrue(parsed.subject)
        self.assertIn(b"Hello", parsed.raw)

    def test_safe_filename(self) -> None:
        self.assertEqual(safe_filename('a/b:c*.eml'), "a_b_c_.eml")
        self.assertEqual(safe_filename("   "), "file")


if __name__ == "__main__":
    unittest.main()
