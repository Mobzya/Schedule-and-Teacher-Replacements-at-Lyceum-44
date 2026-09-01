from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

SHEET_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def google_sheet_parts(url: str) -> tuple[str, str | None]:
    url = url.strip()
    match = SHEET_RE.search(url)
    if not match:
        raise ValueError("Это не похоже на ссылку Google Sheets")
    spreadsheet_id = match.group(1)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    gid = (query.get("gid") or [None])[0]
    if gid is None and parsed.fragment:
        frag = parse_qs(parsed.fragment)
        gid = (frag.get("gid") or [None])[0]
        if gid is None:
            m = re.search(r"gid=(\d+)", parsed.fragment)
            gid = m.group(1) if m else None
    if gid is not None and not gid.isdigit():
        raise ValueError("Некорректный gid вкладки")
    return spreadsheet_id, gid


def google_sheet_export_url(url: str) -> str:
    spreadsheet_id, gid = google_sheet_parts(url)
    export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv"
    # Если gid не указан, Google сам выберет текущую/основную вкладку.
    # Принудительный gid=0 может открыть старый скрытый лист.
    return f"{export_url}&gid={gid}" if gid is not None else export_url


def google_workbook_export_url(url: str) -> str:
    spreadsheet_id, _ = google_sheet_parts(url)
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx"
