"""Command-line entry point for the Yandex mail archiver."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .archiver import DEFAULT_HOST, DEFAULT_PORT, archive_mailboxes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Скачать все письма с корпоративных ящиков Яндекс Почты в локальный HTML-архив.",
    )
    parser.add_argument(
        "--mailboxes",
        default="mailboxes.txt",
        help="Файл со списком адресов (по одному на строку). По умолчанию: mailboxes.txt",
    )
    parser.add_argument(
        "--output",
        default="mail-archive",
        help="Папка, куда складывать архив. По умолчанию: mail-archive",
    )
    parser.add_argument(
        "--password-env",
        default="YANDEX_MAIL_PASSWORD",
        help="Имя переменной окружения с паролем. По умолчанию: YANDEX_MAIL_PASSWORD",
    )
    parser.add_argument(
        "--password-file",
        help="Файл с паролем (одна строка). Не кладите его в git.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"IMAP-сервер. По умолчанию: {DEFAULT_HOST}",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"IMAP-порт. По умолчанию: {DEFAULT_PORT}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Для проверки: скачать не больше N писем из каждой папки.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=15,
        help="Сколько писем забирать за один запрос IMAP. По умолчанию: 15",
    )
    parser.add_argument(
        "--no-mbox",
        action="store_true",
        help="Не писать folder.mbox (экономит место, .eml всё равно сохраняются).",
    )
    return parser


def load_mailboxes(path: Path) -> list[str]:
    if not path.exists():
        raise SystemExit(
            f"Не найден файл {path}. Скопируйте mailboxes.example.txt в mailboxes.txt и впишите адреса."
        )
    addresses: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        addresses.append(line)
    if not addresses:
        raise SystemExit(f"В {path} нет адресов.")
    return addresses


def resolve_password(args: argparse.Namespace) -> str:
    if args.password_file:
        text = Path(args.password_file).read_text(encoding="utf-8").strip()
        if not text:
            raise SystemExit("Файл с паролем пустой.")
        return text
    env_value = os.environ.get(args.password_env, "").strip()
    if env_value:
        return env_value
    if not sys.stdin.isatty():
        raise SystemExit(
            f"Пароль не задан. Укажите {args.password_env} или --password-file."
        )
    return getpass.getpass("Пароль (один на все ящики): ")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    addresses = load_mailboxes(Path(args.mailboxes))
    password = resolve_password(args)
    output_dir = Path(args.output)

    print(f"Ящиков: {len(addresses)}")
    print(f"Сервер: {args.host}:{args.port}")
    print(f"Архив:  {output_dir.resolve()}")

    results = archive_mailboxes(
        addresses=addresses,
        password=password,
        output_dir=output_dir,
        host=args.host,
        port=args.port,
        limit_per_folder=args.limit,
        write_mbox=not args.no_mbox,
        batch_size=args.batch_size,
        log=print,
    )
    print("\nГотово.")
    failed = 0
    for item in results:
        extra = f", ошибки: {'; '.join(item.errors)}" if item.errors else ""
        print(
            f"  {item.address}: скачано {item.downloaded}, пропущено {item.skipped}, "
            f"папок {item.folder_count}{extra}"
        )
        if item.errors and item.downloaded == 0:
            failed += 1
    print(f"\nОткройте в браузере: {(output_dir / 'index.html').resolve()}")
    return 1 if failed == len(results) else 0
