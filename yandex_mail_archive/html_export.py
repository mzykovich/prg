"""Write downloaded messages as EML files and a browsable HTML archive."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from pathlib import Path
from typing import Any

from .mail_parser import ParsedMessage, safe_filename, snippet_from


CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: Georgia, "Times New Roman", serif; margin: 0; background: #f4f1ea; color: #222; }
a { color: #0b57d0; }
header, nav { background: #1f3b4d; color: #fff; padding: 16px 24px; }
header a, nav a { color: #fff; }
main { max-width: 1100px; margin: 0 auto; padding: 24px; }
.card { background: #fff; border-radius: 12px; padding: 16px 20px; margin: 12px 0; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
input[type=search] { width: 100%; padding: 10px 12px; font-size: 16px; border: 1px solid #ccc; border-radius: 8px; }
table { width: 100%; border-collapse: collapse; background: #fff; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eadfce; vertical-align: top; }
th { background: #1f3b4d; color: #fff; position: sticky; top: 0; }
.muted { color: #666; font-size: 0.92em; }
.meta dt { font-weight: bold; }
.meta dd { margin: 0 0 8px 0; }
.attachments a { display: inline-block; margin: 0 8px 8px 0; }
.body { background: #fff; padding: 16px; border-radius: 8px; overflow-x: auto; }
pre.body-text { white-space: pre-wrap; font-family: inherit; }
"""


def write_message_files(
    mailbox_dir: Path,
    folder_name: str,
    parsed: ParsedMessage,
) -> dict[str, Any]:
    folder_dir = mailbox_dir / safe_filename(folder_name, fallback="folder")
    folder_dir.mkdir(parents=True, exist_ok=True)

    date_stamp = ""
    if parsed.date:
        date_stamp = parsed.date.strftime("%Y%m%d")
    stem = safe_filename(f"{date_stamp}_{parsed.uid}_{parsed.subject}", fallback=parsed.uid)
    eml_path = folder_dir / f"{stem}.eml"
    html_path = folder_dir / f"{stem}.html"
    eml_path.write_bytes(parsed.raw)

    saved_attachments: list[str] = []
    if parsed.attachments:
        attach_dir = folder_dir / f"{stem}_files"
        attach_dir.mkdir(parents=True, exist_ok=True)
        for item in parsed.attachments:
            # Large ordinary attachments stay inside the .eml to save disk.
            if not item.inline and not item.content_id and len(item.payload) > 200_000:
                continue
            target = attach_dir / item.filename
            target.write_bytes(item.payload)
            saved_attachments.append(str(target.relative_to(mailbox_dir)))

    html_path.write_text(
        render_message_page(folder_name, parsed, stem, saved_attachments),
        encoding="utf-8",
    )

    iso_date = parsed.date.isoformat() if parsed.date else ""
    return {
        "uid": parsed.uid,
        "folder": folder_name,
        "subject": parsed.subject,
        "from": parsed.from_addr,
        "to": parsed.to_addr,
        "cc": parsed.cc_addr,
        "date": iso_date,
        "date_raw": parsed.date_raw,
        "eml": str(eml_path.relative_to(mailbox_dir)),
        "html": str(html_path.relative_to(mailbox_dir)),
        "attachments": saved_attachments,
        "snippet": snippet_from(parsed),
        "message_id": parsed.message_id,
    }


def append_mbox(mailbox_dir: Path, folder_name: str, parsed: ParsedMessage) -> None:
    folder_dir = mailbox_dir / safe_filename(folder_name, fallback="folder")
    folder_dir.mkdir(parents=True, exist_ok=True)
    mbox_path = folder_dir / "folder.mbox"
    sender = parsed.from_addr.replace(" ", "_") or "unknown"
    stamp = (parsed.date or dt.datetime.now(dt.timezone.utc)).strftime("%a %b %d %H:%M:%S %Y")
    lines = [f"From {sender} {stamp}\n".encode("utf-8")]
    raw = parsed.raw.replace(b"\r\n", b"\n")
    for line in raw.splitlines(keepends=True):
        if line.startswith(b"From "):
            lines.append(b">" + line)
        else:
            lines.append(line)
    if not raw.endswith(b"\n"):
        lines.append(b"\n")
    lines.append(b"\n")
    with mbox_path.open("ab") as handle:
        handle.writelines(lines)


def write_mailbox_index(mailbox_dir: Path, address: str, messages: list[dict[str, Any]]) -> None:
    rows = []
    folders = sorted({item.get("folder", "") for item in messages})
    for item in sorted(messages, key=lambda row: row.get("date") or "", reverse=True):
        date = _format_date(item.get("date") or "")
        subject = html.escape(item.get("subject") or "(без темы)")
        sender = html.escape(item.get("from") or "")
        folder = html.escape(item.get("folder") or "")
        href = html.escape(item.get("html") or "#")
        snippet = html.escape(item.get("snippet") or "")
        rows.append(
            "<tr>"
            f"<td>{date}</td>"
            f"<td>{folder}</td>"
            f"<td><a href=\"{href}\">{subject}</a><div class=\"muted\">{snippet}</div></td>"
            f"<td>{sender}</td>"
            "</tr>"
        )
    folder_links = "".join(
        f"<li>{html.escape(name)} — {sum(1 for item in messages if item.get('folder') == name)}</li>"
        for name in folders
    )
    body = f"""
<nav><a href="../index.html">← Все ящики</a></nav>
<main>
  <h1>{html.escape(address)}</h1>
  <p class="muted">Писем в архиве: {len(messages)}</p>
  <div class="card">
    <h2>Папки</h2>
    <ul>{folder_links or "<li>пусто</li>"}</ul>
  </div>
  <p><input type="search" id="q" placeholder="Поиск по теме, отправителю, тексту..."></p>
  <table>
    <thead><tr><th>Дата</th><th>Папка</th><th>Тема</th><th>От кого</th></tr></thead>
    <tbody id="rows">{''.join(rows)}</tbody>
  </table>
</main>
<script>
const q = document.getElementById('q');
const rows = [...document.querySelectorAll('#rows tr')];
q.addEventListener('input', () => {{
  const value = q.value.toLowerCase();
  rows.forEach(row => {{
    row.style.display = row.textContent.toLowerCase().includes(value) ? '' : 'none';
  }});
}});
</script>
"""
    (mailbox_dir / "index.html").write_text(_page(f"Архив {address}", body), encoding="utf-8")


def write_root_index(output_dir: Path, mailboxes: list[dict[str, Any]]) -> None:
    items = []
    for box in mailboxes:
        address = html.escape(box["address"])
        href = html.escape(box["href"])
        count = box["count"]
        error = html.escape(box.get("error") or "")
        status = error if error else f"{count} писем"
        items.append(f'<div class="card"><h2><a href="{href}">{address}</a></h2><p>{status}</p></div>')
    body = f"""
<header><h1>Архив Яндекс Почты</h1></header>
<main>
  <p>Откройте ящик, чтобы читать письма в браузере. Рядом лежат файлы <code>.eml</code> — их можно открыть в Outlook или Thunderbird.</p>
  {''.join(items) or '<p>Пока нет скачанных ящиков.</p>'}
</main>
"""
    (output_dir / "index.html").write_text(_page("Архив Яндекс Почты", body), encoding="utf-8")


def render_message_page(
    folder_name: str,
    parsed: ParsedMessage,
    stem: str,
    saved_attachments: list[str],
) -> str:
    files_dir = f"{stem}_files"
    attach_html = []
    for rel in saved_attachments:
        name = Path(rel).name
        attach_html.append(f'<a href="{html.escape(files_dir + "/" + name)}">{html.escape(name)}</a>')

    if parsed.html_body:
        body_html = _prefix_local_images(parsed.html_body, files_dir)
        body_block = f'<div class="body">{body_html}</div>'
    else:
        body_block = f'<pre class="body body-text">{html.escape(parsed.text_body or "(пустое письмо)")}</pre>'

    date_show = parsed.date.strftime("%Y-%m-%d %H:%M UTC") if parsed.date else parsed.date_raw
    body = f"""
<nav><a href="../index.html">← {html.escape(folder_name)}</a> · <a href="{html.escape(stem)}.eml">скачать .eml</a></nav>
<main>
  <h1>{html.escape(parsed.subject)}</h1>
  <dl class="meta card">
    <dt>От</dt><dd>{html.escape(parsed.from_addr)}</dd>
    <dt>Кому</dt><dd>{html.escape(parsed.to_addr)}</dd>
    <dt>Копия</dt><dd>{html.escape(parsed.cc_addr) or "—"}</dd>
    <dt>Дата</dt><dd>{html.escape(date_show)}</dd>
    <dt>Папка</dt><dd>{html.escape(folder_name)}</dd>
  </dl>
  <div class="card attachments"><strong>Вложения:</strong> {''.join(attach_html) or "нет"}</div>
  {body_block}
</main>
"""
    return _page(parsed.subject, body)


def load_state(mailbox_dir: Path) -> dict[str, Any]:
    path = mailbox_dir / "_state.json"
    if not path.exists():
        return {"folders": {}, "messages": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(mailbox_dir: Path, state: dict[str, Any]) -> None:
    mailbox_dir.mkdir(parents=True, exist_ok=True)
    path = mailbox_dir / "_state.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>\n"
    )


def _prefix_local_images(html_body: str, files_dir: str) -> str:
    def replace(match: re.Match[str]) -> str:
        prefix, url, suffix = match.group(1), match.group(2), match.group(3)
        if re.match(r"^(?:https?:|mailto:|data:|/|\./|#)", url, re.IGNORECASE):
            return match.group(0)
        return f"{prefix}{html.escape(files_dir)}/{url}{suffix}"

    return re.sub(r"""(src=["'])([^"']+)(["'])""", replace, html_body)


def _format_date(iso_value: str) -> str:
    if not iso_value:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(iso_value)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_value
