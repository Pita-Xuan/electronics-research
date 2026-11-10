"""Run all collectors and write the public data bundle."""

from __future__ import annotations

from typing import Any

from .common import (
    CollectorError,
    LATEST_PATH,
    atomic_write_json,
    failed_metric,
    iso_now,
    load_manifest,
    update_history,
)
from .fred import collect_fred
from .trendforce import collect_trendforce


COLLECTORS = {
    "trendforce": collect_trendforce,
    "fred": collect_fred,
}


def run() -> int:
    manifest = load_manifest()
    crawled_at = iso_now()
    latest_metrics: dict[str, dict[str, Any]] = {}
    failures = 0

    for metric in manifest["metrics"]:
        collector_name = metric.get("collector")
        try:
            collector = COLLECTORS[collector_name]
            result = collector(metric)
            observations = result.get("points") or [
                {"date": result["date"], "value": result["value"]}
            ]
            latest, _ = update_history(
                metric,
                observations,
                source_updated_at=result.get("source_updated_at"),
                status=result["status"],
                crawled_at=crawled_at,
            )
            print(f"OK   {metric['id']} {result['date']} {result['value']} ({result['status']})")
        except CollectorError as exc:
            failures += 1
            latest = failed_metric(
                metric,
                status=exc.status,
                crawled_at=crawled_at,
                error=str(exc),
            )
            print(f"FAIL {metric['id']} [{exc.status}] {exc}")
        except Exception as exc:  # Keep one malformed metric from stopping the rest.
            failures += 1
            latest = failed_metric(
                metric,
                status="failed",
                crawled_at=crawled_at,
                error=f"未预期错误：{type(exc).__name__}: {exc}",
            )
            print(f"FAIL {metric['id']} [failed] {type(exc).__name__}: {exc}")
        latest_metrics[metric["id"]] = latest

    atomic_write_json(
        LATEST_PATH,
        {
            "crawled_at": crawled_at,
            "timezone": "Asia/Shanghai",
            "schedule": "工作日北京时间 09:30",
            "metrics": latest_metrics,
        },
    )
    print(f"Wrote {LATEST_PATH} with {len(latest_metrics)} metrics; failures={failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

