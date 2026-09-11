"""In-process HTTP/runtime metrics collection.

Snapshots aggregate counters and latency percentiles for every HTTP request as
it flows through :class:`src.request_context.RequestIdMiddleware`. Exposed
under ``GET /api/diagnostics/metrics`` (JSON) and
``GET /api/diagnostics/metrics/export`` (Prometheus text format).

Bounded and privacy-safe by construction:

- a fixed-size latency window keeps only the newest samples;
- aggregate counters only — no URLs, headers, bodies, or PII;
- process-local; nothing is persisted.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Optional

_STARTED = time.monotonic()
_LOCK = threading.Lock()

_requests_total = 0
_active = 0
_status_counts: Dict[int, int] = {}
_method_counts: Dict[str, int] = {}
_response_times: Deque[float] = deque(maxlen=2000)
_internal_tool_outcomes: Dict[str, int] = {}


def reset_counts() -> None:
    """Clear every counter and latency sample. Testing / administrative use."""
    global _requests_total, _active
    with _LOCK:
        _requests_total = 0
        _active = 0
        _status_counts.clear()
        _method_counts.clear()
        _response_times.clear()
        _internal_tool_outcomes.clear()


def request_started() -> None:
    """Count one in-flight request (called before the downstream app runs)."""
    global _active
    with _LOCK:
        _active += 1


def request_finished(method: str, status_code: int, duration_s: float) -> None:
    """Record a terminal request: totals, status/method buckets, latency."""
    global _requests_total, _active
    with _LOCK:
        _requests_total += 1
        if _active > 0:
            _active -= 1
        status = int(status_code or 0)
        _status_counts[status] = _status_counts.get(status, 0) + 1
        method_key = str(method or "").upper()
        if method_key:
            _method_counts[method_key] = _method_counts.get(method_key, 0) + 1
        _response_times.append(max(0.0, duration_s))


def record_internal_tool_outcome(outcome: str) -> None:
    """Count an internal-tool loopback auth outcome.

    Outcomes are ``granted`` (token + trusted loopback), ``impersonated``
    (loopback call that resolved an ``X-Odysseus-Owner`` user), and ``rejected``
    (internal-tool header present but the gate failed — wrong token or a
    non-loopback/proxied peer).
    """
    with _LOCK:
        _internal_tool_outcomes[outcome] = _internal_tool_outcomes.get(outcome, 0) + 1


def _percentile(values: list[float], q: float) -> Optional[float]:
    """Nearest-rank percentile for a small in-memory sample."""
    if not values:
        return None
    ordered = sorted(values)
    if q <= 0:
        return ordered[0]
    if q >= 1:
        return ordered[-1]
    rank = max(1, int((q * len(ordered)) + 0.999999999))
    return ordered[min(len(ordered), rank) - 1]


def _app_version() -> str:
    try:
        from core.constants import APP_VERSION
        return str(APP_VERSION)
    except Exception:
        return "unknown"


def snapshot() -> Dict[str, object]:
    """Return a JSON-serialisable aggregate metrics snapshot."""
    with _LOCK:
        times = list(_response_times)
        total = _requests_total
        active = _active
        status = dict(_status_counts)
        method = dict(_method_counts)
        tool_outcomes = dict(_internal_tool_outcomes)

    summary = sum(times)
    avg = round(summary / len(times), 4) if times else None
    p50 = _percentile(times, 0.50)
    p95 = _percentile(times, 0.95)

    return {
        "uptime_seconds": round(time.monotonic() - _STARTED, 1),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "version": _app_version(),
        "requests_total": total,
        "active_requests": active,
        "errors_5xx_total": sum(v for k, v in status.items() if k >= 500),
        "requests_by_status": status,
        "requests_by_method": method,
        "internal_tool_outcomes": tool_outcomes,
        "latency_seconds": {
            "avg": avg,
            "p50": round(p50, 4) if p50 is not None else None,
            "p95": round(p95, 4) if p95 is not None else None,
            "samples": len(times),
            "sum": round(summary, 4),
        },
    }


def prometheus_text() -> str:
    """Return the snapshot as Prometheus text exposition format (0.0.4)."""
    data = snapshot()
    latency = data["latency_seconds"]

    lines = [
        "# HELP odysseus_http_requests_total Total HTTP requests completed.",
        "# TYPE odysseus_http_requests_total counter",
        f"odysseus_http_requests_total {data['requests_total']}",
    ]
    for code, count in sorted(data["requests_by_status"].items()):
        lines.append(f'odysseus_http_requests_total{{status="{code}"}} {count}')
    for method, count in sorted(data["requests_by_method"].items()):
        lines.append(f'odysseus_http_requests_total{{method="{method}"}} {count}')

    tool_total = sum(data["internal_tool_outcomes"].values())
    lines += [
        "# HELP odysseus_internal_tool_requests_total Internal-tool loopback auth outcomes.",
        "# TYPE odysseus_internal_tool_requests_total counter",
        f"odysseus_internal_tool_requests_total {tool_total}",
    ]
    for result, count in sorted(data["internal_tool_outcomes"].items()):
        lines.append(f'odysseus_internal_tool_requests_total{{result="{result}"}} {count}')

    lines += [
        "# HELP odysseus_http_errors_total HTTP responses with status >= 500.",
        "# TYPE odysseus_http_errors_total counter",
        f"odysseus_http_errors_total {data['errors_5xx_total']}",
        "# HELP odysseus_http_active_requests Requests currently in flight.",
        "# TYPE odysseus_http_active_requests gauge",
        f"odysseus_http_active_requests {data['active_requests']}",
        "# HELP odysseus_http_latency_seconds HTTP response latency distribution.",
        "# TYPE odysseus_http_latency_seconds summary",
        f'odysseus_http_latency_seconds{{quantile="0.5"}} {latency["p50"] or 0}',
        f'odysseus_http_latency_seconds{{quantile="0.95"}} {latency["p95"] or 0}',
        f"odysseus_http_latency_seconds_sum {latency['sum']}",
        f"odysseus_http_latency_seconds_count {latency['samples']}",
        "# HELP odysseus_uptime_seconds Process uptime in seconds.",
        "# TYPE odysseus_uptime_seconds counter",
        f"odysseus_uptime_seconds {data['uptime_seconds']}",
        "# HELP odysseus_build_info Build metadata (version).",
        "# TYPE odysseus_build_info gauge",
        f'odysseus_build_info{{version="{data["version"]}"}} 1',
    ]
    return "\n".join(lines) + "\n"