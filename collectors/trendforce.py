"""TrendForce public DRAM and NAND spot-price collector."""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

from .common import CollectorError, fetch_text, finite_number


TAIPEI = timezone(timedelta(hours=8))
UPDATE_RE = re.compile(r"Last Update\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+\(GMT\+8\)")


@dataclass
class ParsedTable:
    start: int
    headers: list[str]
    rows: list[list[str]]


class PriceTableParser(HTMLParser):
    def __init__(self, line_offsets: list[int]) -> None:
        super().__init__(convert_charrefs=True)
        self._line_offsets = line_offsets
        self.tables: list[ParsedTable] = []
        self._table: ParsedTable | None = None
        self._in_thead = False
        self._in_tbody = False
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._row: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table" and "price-table" in (attributes.get("class") or "").split():
            line, column = self.getpos()
            absolute_offset = self._line_offsets[line - 1] + column
            self._table = ParsedTable(absolute_offset, [], [])
        elif self._table is None:
            return
        elif tag == "thead":
            self._in_thead = True
        elif tag == "tbody":
            self._in_tbody = True
        elif tag == "tr":
            self._row = []
        elif tag in {"th", "td"}:
            self._in_cell = True
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._table is not None and self._in_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._table is None:
            return
        if tag in {"th", "td"}:
            cell = " ".join("".join(self._cell_parts).split())
            self._row.append(cell)
            self._in_cell = False
            self._cell_parts = []
        elif tag == "tr":
            if self._in_thead and self._row:
                self._table.headers = self._row
            elif self._in_tbody and self._row:
                self._table.rows.append(self._row)
            self._row = []
        elif tag == "thead":
            self._in_thead = False
        elif tag == "tbody":
            self._in_tbody = False
        elif tag == "table":
            self.tables.append(self._table)
            self._table = None
            self._in_thead = False
            self._in_tbody = False


def _latest_update_before(page: str, position: int) -> tuple[str, str]:
    matches = list(UPDATE_RE.finditer(page[:position]))
    if not matches:
        raise CollectorError("TrendForce 页面的更新时间结构发生变化", "source_changed")
    match = matches[-1]
    source_date, source_time = match.groups()
    updated_at = datetime.fromisoformat(f"{source_date}T{source_time}:00").replace(tzinfo=TAIPEI)
    return source_date, updated_at.isoformat()


def _pick_table(page: str, target_label: str) -> tuple[ParsedTable, str, str]:
    line_offsets: list[int] = []
    offset = 0
    for line in page.splitlines(keepends=True):
        line_offsets.append(offset)
        offset += len(line)
    if not line_offsets:
        line_offsets.append(0)
    parser = PriceTableParser(line_offsets)
    parser.feed(page)
    normalized_target = " ".join(target_label.split())
    for table in parser.tables:
        normalized_headers = [" ".join(value.split()) for value in table.headers]
        if "Session Average" not in normalized_headers:
            continue
        for row in table.rows:
            if row and " ".join(row[0].split()) == normalized_target:
                source_date, source_updated_at = _latest_update_before(page, table.start)
                return table, source_date, source_updated_at
    raise CollectorError(
        f"TrendForce 未找到目标规格或 Session Average 列：{target_label}",
        "source_changed",
    )


def collect_trendforce(metric: dict[str, Any]) -> dict[str, Any]:
    page = fetch_text(metric["source_url"])
    table, source_date, source_updated_at = _pick_table(page, metric["selector_label"])
    headers = [" ".join(value.split()) for value in table.headers]
    try:
        average_index = headers.index("Session Average")
    except ValueError as exc:
        raise CollectorError("TrendForce Session Average 表头缺失", "source_changed") from exc
    row = next(
        row
        for row in table.rows
        if row and " ".join(row[0].split()) == " ".join(metric["selector_label"].split())
    )
    if average_index >= len(row):
        raise CollectorError("TrendForce 目标价格列缺失", "source_changed")
    value = finite_number(row[average_index], "Session Average")
    source_day = date.fromisoformat(source_date)
    age_days = (datetime.now(TAIPEI).date() - source_day).days
    max_age = int(metric.get("max_age_days", 7))
    status = "lagging" if age_days > max_age else "ok"
    return {
        "date": source_date,
        "value": value,
        "source_updated_at": source_updated_at,
        "status": status,
    }

