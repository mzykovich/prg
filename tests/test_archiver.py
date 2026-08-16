from __future__ import annotations

import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from yandex_mail_archive.archiver import MailArchiveError, _extract_fetch_bytes, archive_mailboxes
from yandex_mail_archive.cli import load_mailboxes
from yandex_mail_archive.html_export import load_state
from yandex_mail_archive.imap_utf7 import decode_modified_utf7


def _eml(subject: str, body: str, sender: str = "boss@company.ru") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "user@company.ru"
    message["Subject"] = subject
    message["Date"] = "Thu, 16 Jan 2025 09:00:00 +0300"
    message.set_content(body)
    return message.as_bytes()


class FakeSession:
    mailboxes = {
        "user@company.ru": {
            "INBOX": {
                "1": _eml("Первое письмо", "Нужно оплатить счет"),
                "2": _eml("Второе письмо", "Повторная отправка"),
            },
            "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-": {
                "10": _eml("Исходящее", "Ответ клиенту", sender="user@company.ru"),
            },
        }
    }

    def __init__(self, host: str, port: int, username: str, password: str, timeout: int = 60) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.selected: str | None = None
        self.closed = False

    def connect(self) -> None:
        if self.password != "secret":
            raise MailArchiveError("неверный пароль")
        if self.username not in self.mailboxes:
            raise MailArchiveError("ящик не найден")

    def close(self) -> None:
        self.closed = True

    def reconnect(self) -> None:
        self.connect()

    def list_folders(self) -> list[tuple[str, str]]:
        data = self.mailboxes[self.username]
        return [(name, decode_modified_utf7(name)) for name in data]

    def select(self, folder: str) -> str:
        self.selected = folder
        if folder not in self.mailboxes[self.username]:
            raise MailArchiveError(f"нет папки {folder}")
        return "99"

    def search_uids(self) -> list[str]:
        assert self.selected is not None
        return list(self.mailboxes[self.username][self.selected])

    def fetch_raw(self, uid: str) -> bytes:
        assert self.selected is not None
        return self.mailboxes[self.username][self.selected][uid]


class FetchParseTests(unittest.TestCase):
    def test_extracts_literal_payload(self) -> None:
        payload = [(b"1 (UID 1 BODY[] {12}", b"From: a\r\n\r\nHi"), b")"]
        self.assertEqual(_extract_fetch_bytes(payload), b"From: a\r\n\r\nHi")

    def test_empty_payload(self) -> None:
        self.assertIsNone(_extract_fetch_bytes(None))
        self.assertIsNone(_extract_fetch_bytes([]))


class ArchiverTests(unittest.TestCase):
    def test_downloads_all_folders_and_builds_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            results = archive_mailboxes(
                addresses=["user@company.ru"],
                password="secret",
                output_dir=output,
                session_factory=FakeSession,
            )
            self.assertEqual(results[0].downloaded, 3)
            self.assertEqual(results[0].skipped, 0)
            self.assertFalse(results[0].errors)
            root = output / "index.html"
            self.assertTrue(root.exists())
            mailbox_dir = output / "user_at_company.ru"
            index = (mailbox_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("Первое письмо", index)
            self.assertIn("Отправленные", index)
            inbox_files = list((mailbox_dir / "INBOX").glob("*.eml"))
            self.assertEqual(len(inbox_files), 2)
            html_page = next((mailbox_dir / "INBOX").glob("*.html")).read_text(encoding="utf-8")
            self.assertIn("Нужно оплатить счет", html_page)
            self.assertTrue((mailbox_dir / "INBOX" / "folder.mbox").exists())

            again = archive_mailboxes(
                addresses=["user@company.ru"],
                password="secret",
                output_dir=output,
                session_factory=FakeSession,
            )
            self.assertEqual(again[0].downloaded, 0)
            self.assertEqual(again[0].skipped, 3)
            state = load_state(mailbox_dir)
            self.assertEqual(len(state["messages"]), 3)

    def test_bad_password_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = archive_mailboxes(
                addresses=["user@company.ru"],
                password="wrong",
                output_dir=Path(tmp),
                session_factory=FakeSession,
            )
            self.assertTrue(results[0].errors)
            self.assertEqual(results[0].downloaded, 0)

    def test_limit_per_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = archive_mailboxes(
                addresses=["user@company.ru"],
                password="secret",
                output_dir=Path(tmp),
                limit_per_folder=1,
                session_factory=FakeSession,
            )
            self.assertEqual(results[0].downloaded, 2)


class CliHelperTests(unittest.TestCase):
    def test_load_mailboxes_skips_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text("# comment\n\na@x.ru\nb@x.ru\n", encoding="utf-8")
            self.assertEqual(load_mailboxes(path), ["a@x.ru", "b@x.ru"])


if __name__ == "__main__":
    unittest.main()
