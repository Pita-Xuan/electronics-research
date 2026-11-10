"""FRED CSV collector with strict CSV and numeric validation."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any

from .common import CollectorError, fetch_text, finite_number


def collect_fred(metric: dict[str, Any]) -> dict[str, Any]:
    text = fetch_text(metric["source_url"])
    if "<html" in text[:1000].lower() or "<!doctype" in text[:1000].lower():
        raise CollectorError("FRED 返回了 HTML 而非 CSV", "source_changed")
    rows = csv.reader(io.StringIO(text))
    try:
        header = [cell.strip().lstrip("\ufeff") for cell in next(rows)]
    except StopIteration as exc:
        raise CollectorError("FRED CSV 为空", "source_changed") from exc
    if len(header) < 2 or header[0].lower() not in {"date", "observation_date"}:
        raise CollectorError(f"FRED CSV 表头变化：{header}", "source_changed")

    points: list[tuple[str, float]] = []
    for row in rows:
        if len(row) < 2 or any("<" in cell or ">" in cell for cell in row[:2]):
            continue
        raw_date, raw_value = row[0].strip(), row[1].strip()
        if not raw_date or not raw_value or raw_value == ".":
            continue
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if len(raw_date) != 10:
            continue
        points.append((parsed_date.isoformat(), finite_number(raw_value, metric["id"])))
    if not points:
        raise CollectorError("FRED CSV 中没有有效观测", "source_changed")

    points.sort(key=lambda item: item[0])
    source_date, value = points[-1]
    age_days = (datetime.utcnow().date() - date.fromisoformat(source_date)).days
    status = "lagging" if age_days > int(metric.get("max_age_days", 75)) else "ok"
    return {
        "date": source_date,
        "value": value,
        "points": [{"date": point_date, "value": point_value} for point_date, point_value in points],
        "source_updated_at": None,
        "status": status,
    }

