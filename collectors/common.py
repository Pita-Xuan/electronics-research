"""Shared persistence and validation helpers for public market data."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
LATEST_PATH = DATA_DIR / "latest.json"
MANIFEST_PATH = REPO_ROOT / "collector_manifest.json"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_STATUSES = {"ok", "lagging", "source_changed", "failed"}
USER_AGENT = (
    "Mozilla/5.0 (compatible; ElectronicsResearchData/1.0; "
    "+https://github.com/Pita-Xuan/electronics-research)"
)


class CollectorError(RuntimeError):
    """A source could not be parsed without guessing a value."""

    def __init__(self, message: str, status: str = "failed") -> None:
        super().__init__(message)
        self.status = status if status in VALID_STATUSES else "failed"


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_now() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/csv;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            content = response.read()
            if not content:
                raise CollectorError("数据源返回空内容")
            return content
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise CollectorError(f"请求数据源失败：{exc}") from exc


def fetch_text(url: str, timeout: int = 30) -> str:
    raw = fetch_bytes(url, timeout=timeout)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def assert_metric(metric: dict[str, Any]) -> None:
    required = {"id", "name", "short", "unit", "source", "source_url", "category"}
    missing = required.difference(metric)
    if missing:
        raise ValueError(f"指标配置缺少字段：{sorted(missing)}")


def parse_source_date(value: str) -> date:
    if not DATE_RE.fullmatch(value):
        raise CollectorError(f"日期格式不正确：{value}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CollectorError(f"日期无效：{value}") from exc


def finite_number(value: Any, field: str = "value") -> float:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise CollectorError(f"{field} 不是有效数字：{value}", "source_changed") from exc
    if not math.isfinite(number) or number <= 0:
        raise CollectorError(f"{field} 超出有效范围：{value}", "source_changed")
    return number


def update_history(
    metric: dict[str, Any],
    observations: dict[str, Any] | list[dict[str, Any]],
    value: float | None = None,
    *,
    source_updated_at: str | None,
    status: str,
    crawled_at: str,
    error: str | None = None,
) -> tuple[dict[str, Any], float | None]:
    """Merge one or more observations and return the latest public metric."""
    metric_id = metric["id"]
    if isinstance(observations, str) and value is not None:
        candidates = [{"date": observations, "value": value}]
    elif isinstance(observations, dict):
        candidates = [observations]
    elif isinstance(observations, list):
        candidates = observations
    else:
        raise CollectorError("历史观测结构无效", "source_changed")

    normalized: dict[str, float] = {}
    for point in candidates:
        if not isinstance(point, dict):
            raise CollectorError("历史观测包含无效记录", "source_changed")
        point_date = str(point.get("date", ""))
        parse_source_date(point_date)
        point_value = finite_number(point.get("value"))
        if point_date in normalized and normalized[point_date] != point_value:
            raise CollectorError(f"同一日期出现冲突价格：{point_date}", "source_changed")
        normalized[point_date] = point_value

    if not normalized:
        raise CollectorError("没有可写入的历史观测", "source_changed")

    path = HISTORY_DIR / f"{metric_id}.json"
    previous = load_json(
        path,
        {
            "metric_id": metric_id,
            "unit": metric["unit"],
            "source": metric["source"],
            "source_url": metric["source_url"],
            "points": [],
            "updated_at": crawled_at,
        },
    )
    points = [
        point
        for point in previous.get("points", [])
        if isinstance(point, dict)
        and DATE_RE.fullmatch(str(point.get("date", "")))
        and isinstance(point.get("value"), (int, float))
        and math.isfinite(float(point["value"]))
        and float(point["value"]) > 0
    ]
    deduped = {point["date"]: point for point in points}
    for point_date, point_value in normalized.items():
        deduped[point_date] = {"date": point_date, "value": point_value}
    ordered = [deduped[key] for key in sorted(deduped)]
    history = {
        "metric_id": metric_id,
        "unit": metric["unit"],
        "source": metric["source"],
        "source_url": metric["source_url"],
        "points": ordered,
        "updated_at": crawled_at,
    }
    atomic_write_json(path, history)
    source_date = ordered[-1]["date"]
    latest_value = float(ordered[-1]["value"])
    latest = {
        **metric,
        "date": source_date,
        "value": latest_value,
        "status": status,
        "source_updated_at": source_updated_at,
        "last_success_at": crawled_at,
    }
    if error:
        latest["error"] = error
    return latest, latest_value


def failed_metric(
    metric: dict[str, Any],
    *,
    status: str,
    crawled_at: str,
    error: str,
) -> dict[str, Any]:
    """Keep the last valid observation visible when a refresh fails."""
    previous = load_json(HISTORY_DIR / f"{metric['id']}.json", {})
    points = previous.get("points", []) if isinstance(previous, dict) else []
    valid_points = [
        point
        for point in points
        if isinstance(point, dict)
        and DATE_RE.fullmatch(str(point.get("date", "")))
        and isinstance(point.get("value"), (int, float))
        and math.isfinite(float(point["value"]))
    ]
    last = valid_points[-1] if valid_points else None
    latest = {
        **metric,
        "date": last.get("date") if last else None,
        "value": last.get("value") if last else None,
        "status": status if status in VALID_STATUSES else "failed",
        "source_updated_at": None,
        "last_success_at": previous.get("updated_at") if isinstance(previous, dict) else None,
        "error": error,
    }
    return latest


def load_manifest() -> dict[str, Any]:
    manifest = load_json(MANIFEST_PATH, None)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("metrics"), list):
        raise ValueError("collector_manifest.json 结构无效")
    for metric in manifest["metrics"]:
        if not isinstance(metric, dict):
            raise ValueError("collector_manifest.json 中存在无效指标")
        assert_metric(metric)
    return manifest

