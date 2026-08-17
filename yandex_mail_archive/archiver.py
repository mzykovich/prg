"""Download every folder from one or more Yandex IMAP mailboxes."""

from __future__ import annotations

import imaplib
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .html_export import (
    append_mbox,
    load_state,
    save_state,
    write_mailbox_index,
    write_message_files,
    write_root_index,
)
from .imap_utf7 import decode_modified_utf7, parse_imap_list_line
from .mail_parser import mailbox_dirname, parse_raw_message, safe_filename

DEFAULT_HOST = "imap.yandex.ru"
DEFAULT_PORT = 993
SKIP_FLAGS = {"\\Noselect", "\\NonExistent"}


class MailArchiveError(RuntimeError):
    pass


@dataclass
class ArchiveResult:
    address: str
    downloaded: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    folder_count: int = 0


class ImapSession:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: int = 180,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.client: imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        context = ssl.create_default_context()
        self.client = imaplib.IMAP4_SSL(
            self.host,
            self.port,
            ssl_context=context,
            timeout=self.timeout,
        )
        status, payload = self.client.login(self.username, self.password)
        if status != "OK":
            raise MailArchiveError(f"Не удалось войти в {self.username}: {payload}")

    def close(self) -> None:
        if self.client is None:
            return
        try:
            self.client.logout()
        except Exception:
            try:
                self.client.shutdown()
            except Exception:
                pass
        self.client = None

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def list_folders(self) -> list[tuple[str, str]]:
        client = self._require()
        status, lines = client.list()
        if status != "OK":
            raise MailArchiveError(f"Не удалось получить папки: {lines}")
        folders: list[tuple[str, str]] = []
        for raw in lines or []:
            if not isinstance(raw, (bytes, bytearray)):
                continue
            parsed = parse_imap_list_line(raw.decode("utf-8", errors="replace"))
            if parsed is None:
                continue
            flags, _delim, name = parsed
            flag_set = {flag for flag in flags}
            if flag_set & SKIP_FLAGS:
                continue
            display = decode_modified_utf7(name)
            folders.append((name, display))
        return folders

    def select(self, folder: str) -> str:
        client = self._require()
        quoted = _quote_mailbox(folder)
        status, payload = client.select(quoted, readonly=True)
        if status != "OK":
            raise MailArchiveError(f"Не удалось открыть папку {folder}: {payload}")
        uidvalidity = self._status_uidvalidity(quoted)
        if uidvalidity:
            return uidvalidity
        try:
            status, data = client.response("UIDVALIDITY")
            if data and data[0]:
                raw = data[0]
                return raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        return ""

    def search_uids(self) -> list[str]:
        client = self._require()
        status, payload = client.uid("search", None, "ALL")
        if status != "OK":
            raise MailArchiveError(f"Не удалось получить список писем: {payload}")
        if not payload or payload[0] in (None, b""):
            return []
        blob = payload[0]
        if not isinstance(blob, (bytes, bytearray)):
            return []
        return [token.decode("ascii") for token in blob.split() if token]

    def fetch_raw(self, uid: str) -> bytes:
        found = self.fetch_raw_many([uid])
        raw = found.get(uid)
        if raw is None:
            raise MailArchiveError(f"Пустой ответ IMAP для UID {uid}")
        return raw

    def fetch_raw_many(self, uids: list[str]) -> dict[str, bytes]:
        if not uids:
            return {}
        client = self._require()
        status, payload = client.uid("fetch", ",".join(uids), "(UID BODY.PEEK[])")
        if status != "OK":
            raise MailArchiveError(f"Не удалось скачать UID {','.join(uids[:5])}: {payload}")
        found: dict[str, bytes] = {}
        unpaired: list[bytes] = []
        for uid, raw in _extract_fetch_items(payload):
            if uid:
                found[uid] = raw
            else:
                unpaired.append(raw)
        if unpaired:
            for uid, raw in zip((item for item in uids if item not in found), unpaired):
                found[uid] = raw
        return found

    def _status_uidvalidity(self, quoted_folder: str) -> str:
        client = self._require()
        status, payload = client.status(quoted_folder, "(UIDVALIDITY)")
        if status != "OK" or not payload:
            return ""
        text = payload[0].decode("utf-8", errors="replace") if isinstance(payload[0], bytes) else str(payload[0])
        match = re.search(r"UIDVALIDITY\s+(\d+)", text, re.IGNORECASE)
        return match.group(1) if match else ""

    def _require(self) -> imaplib.IMAP4_SSL:
        if self.client is None:
            raise MailArchiveError("Нет соединения с IMAP")
        return self.client


def archive_mailboxes(
    addresses: list[str],
    password: str,
    output_dir: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    limit_per_folder: int | None = None,
    session_factory: Callable[..., Any] | None = None,
    log: Callable[[str], None] | None = None,
    write_mbox: bool = True,
    batch_size: int = 20,
) -> list[ArchiveResult]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[ArchiveResult] = []
    index_rows: list[dict[str, Any]] = []
    emit = log or (lambda _message: None)
    factory = session_factory or ImapSession

    for address in addresses:
        emit(f"=== {address} ===")
        result = archive_one_mailbox(
            address=address,
            password=password,
            output_dir=output_dir,
            host=host,
            port=port,
            limit_per_folder=limit_per_folder,
            session_factory=factory,
            log=emit,
            write_mbox=write_mbox,
            batch_size=batch_size,
        )
        results.append(result)
        mailbox_dir = output_dir / mailbox_dirname(address)
        state = load_state(mailbox_dir)
        messages = state.get("messages") or []
        write_mailbox_index(mailbox_dir, address, messages)
        index_rows.append(
            {
                "address": address,
                "href": f"{mailbox_dirname(address)}/index.html",
                "count": len(messages),
                "error": "; ".join(result.errors),
            }
        )
    write_root_index(output_dir, index_rows)
    return results


def archive_one_mailbox(
    address: str,
    password: str,
    output_dir: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    limit_per_folder: int | None = None,
    session_factory: Callable[..., Any] | None = None,
    log: Callable[[str], None] | None = None,
    write_mbox: bool = True,
    batch_size: int = 20,
) -> ArchiveResult:
    emit = log or (lambda _message: None)
    result = ArchiveResult(address=address)
    mailbox_dir = output_dir / mailbox_dirname(address)
    mailbox_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(mailbox_dir)
    folders_state: dict[str, Any] = state.setdefault("folders", {})
    messages: list[dict[str, Any]] = state.setdefault("messages", [])
    known = {(item.get("folder"), str(item.get("uid"))) for item in messages}

    factory = session_factory or ImapSession
    session = factory(host=host, port=port, username=address, password=password)
    try:
        session.connect()
    except Exception as exc:
        result.errors.append(f"вход: {exc}")
        emit(f"Ошибка входа {address}: {exc}")
        save_state(mailbox_dir, state)
        return result

    try:
        folder_list = _with_retry(session, lambda: session.list_folders(), emit)
        result.folder_count = len(folder_list)
        for imap_name, display_name in folder_list:
            emit(f"  папка: {display_name}")
            try:
                downloaded, skipped = _archive_folder(
                    session=session,
                    mailbox_dir=mailbox_dir,
                    imap_name=imap_name,
                    display_name=display_name,
                    folders_state=folders_state,
                    messages=messages,
                    known=known,
                    limit_per_folder=limit_per_folder,
                    log=emit,
                    write_mbox=write_mbox,
                    batch_size=batch_size,
                )
                result.downloaded += downloaded
                result.skipped += skipped
            except Exception as exc:
                result.errors.append(f"{display_name}: {exc}")
                emit(f"  ошибка папки {display_name}: {exc}")
                try:
                    session.reconnect()
                except Exception as reconnect_exc:
                    result.errors.append(f"reconnect: {reconnect_exc}")
                    emit(f"  не удалось переподключиться: {reconnect_exc}")
                    break
    finally:
        session.close()
        state["email"] = address
        save_state(mailbox_dir, state)
    return result


def _archive_folder(
    session: Any,
    mailbox_dir: Path,
    imap_name: str,
    display_name: str,
    folders_state: dict[str, Any],
    messages: list[dict[str, Any]],
    known: set[tuple[Any, str]],
    limit_per_folder: int | None,
    log: Callable[[str], None],
    write_mbox: bool = True,
    batch_size: int = 20,
) -> tuple[int, int]:
    def restore_folder() -> str:
        return session.select(imap_name)

    uidvalidity = _with_retry(session, restore_folder, log)
    folder_info = folders_state.setdefault(display_name, {"uidvalidity": "", "uids": []})
    if folder_info.get("uidvalidity") and folder_info.get("uidvalidity") != uidvalidity:
        log(f"  UIDVALIDITY изменился для {display_name}, качаю заново")
        messages[:] = [item for item in messages if item.get("folder") != display_name]
        known.difference_update(
            {(folder, uid) for folder, uid in list(known) if folder == display_name}
        )
        folder_info["uids"] = []
        mbox_path = mailbox_dir / safe_filename(display_name, fallback="folder") / "folder.mbox"
        if mbox_path.exists():
            mbox_path.unlink()
    folder_info["uidvalidity"] = uidvalidity

    uids = _with_retry(session, session.search_uids, log, after_reconnect=restore_folder)
    if limit_per_folder is not None:
        uids = uids[:limit_per_folder]
    downloaded = 0
    skipped = 0
    pending = [uid for uid in uids if (display_name, uid) not in known]
    skipped = len(uids) - len(pending)
    chunk_size = max(1, batch_size)
    for offset in range(0, len(pending), chunk_size):
        chunk = pending[offset : offset + chunk_size]
        fetched = _fetch_chunk(session, chunk, log, restore_folder)
        for uid in chunk:
            raw = fetched.get(uid)
            if raw is None:
                log(f"    UID {uid}: пустой ответ IMAP, пропускаю")
                continue
            try:
                parsed = parse_raw_message(raw, uid)
                record = write_message_files(mailbox_dir, display_name, parsed)
                if write_mbox:
                    append_mbox(mailbox_dir, display_name, parsed)
            except Exception as exc:
                log(f"    UID {uid}: не разобралось ({exc}), сохраняю сырой .eml")
                record = _write_raw_fallback(mailbox_dir, display_name, uid, raw)
            messages.append(record)
            known.add((display_name, uid))
            folder_info.setdefault("uids", []).append(uid)
            downloaded += 1
        if downloaded % 100 < len(chunk):
            log(f"    скачано {downloaded} писем в «{display_name}»")
            save_state(mailbox_dir, {"folders": folders_state, "messages": messages})
    if downloaded:
        save_state(mailbox_dir, {"folders": folders_state, "messages": messages})
    return downloaded, skipped


def _write_raw_fallback(mailbox_dir: Path, folder_name: str, uid: str, raw: bytes) -> dict[str, Any]:
    folder_dir = mailbox_dir / safe_filename(folder_name, fallback="folder")
    folder_dir.mkdir(parents=True, exist_ok=True)
    eml_path = folder_dir / f"{uid}.eml"
    eml_path.write_bytes(raw)
    return {
        "uid": uid,
        "folder": folder_name,
        "subject": f"(не разобрано UID {uid})",
        "from": "",
        "to": "",
        "cc": "",
        "date": "",
        "date_raw": "",
        "eml": str(eml_path.relative_to(mailbox_dir)),
        "html": "",
        "attachments": [],
        "snippet": "",
        "message_id": "",
    }


def _fetch_chunk(
    session: Any,
    chunk: list[str],
    log: Callable[[str], None],
    restore_folder: Callable[[], Any],
) -> dict[str, bytes]:
    def fetch_batch() -> dict[str, bytes]:
        if hasattr(session, "fetch_raw_many"):
            return session.fetch_raw_many(chunk)
        return {uid: session.fetch_raw(uid) for uid in chunk}

    try:
        found = _with_retry(session, fetch_batch, log, after_reconnect=restore_folder)
        missing = [uid for uid in chunk if uid not in found]
        if not missing:
            return found
        for uid in missing:
            found[uid] = _with_retry(
                session,
                lambda current=uid: session.fetch_raw(current),
                log,
                after_reconnect=restore_folder,
            )
        return found
    except MailArchiveError:
        if len(chunk) == 1:
            raise
        log(f"    пакет из {len(chunk)} не прошёл, качаю по одному")
        found = {}
        for uid in chunk:
            found[uid] = _with_retry(
                session,
                lambda current=uid: session.fetch_raw(current),
                log,
                after_reconnect=restore_folder,
            )
        return found


def _with_retry(
    session: Any,
    action: Callable[[], Any],
    log: Callable[[str], None],
    attempts: int = 4,
    after_reconnect: Callable[[], Any] | None = None,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, MailArchiveError) as exc:
            last_error = exc
            log(f"    повтор {attempt}/{attempts}: {exc}")
            if attempt == attempts:
                break
            time.sleep(min(2 ** attempt, 8))
            try:
                session.reconnect()
                if after_reconnect is not None:
                    after_reconnect()
            except Exception as reconnect_exc:
                last_error = reconnect_exc
    raise MailArchiveError(str(last_error) if last_error else "неизвестная ошибка IMAP")


def _quote_mailbox(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _extract_fetch_items(payload: Any) -> list[tuple[str | None, bytes]]:
    items: list[tuple[str | None, bytes]] = []
    if not payload:
        return items
    for item in payload:
        if isinstance(item, tuple) and len(item) >= 2:
            meta, blob = item[0], item[1]
            if not isinstance(blob, (bytes, bytearray)) or not blob:
                continue
            uid = None
            if isinstance(meta, (bytes, bytearray)):
                match = re.search(rb"UID (\d+)", meta)
                if match:
                    uid = match.group(1).decode("ascii")
            items.append((uid, bytes(blob)))
        elif isinstance(item, (bytes, bytearray)) and b"\n" in item and len(item) > 40:
            items.append((None, bytes(item)))
    return items


def _extract_fetch_bytes(payload: Any) -> bytes | None:
    items = _extract_fetch_items(payload)
    return items[0][1] if items else None
