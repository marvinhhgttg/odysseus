"""Detached agent-run manager.

Keeps an agent/chat stream running server-side after the SSE client disconnects
(tab close, navigate away, refresh). The streaming generator is drained by a
background asyncio task into a per-session replay buffer; SSE clients SUBSCRIBE
to that buffer (replay everything so far, then live). Closing the SSE only drops
the subscriber — the drain task keeps going.

The wrapped generator already persists the assistant message to the session on
completion, so reopening the session shows the finished result even if nobody
was connected when it finished. Reconnecting mid-run replays the buffer + streams
live (pick up where it is).

Durability scope: in-memory, survives as long as the server process runs (tab
close / navigation / refresh). It does NOT survive a server restart.
"""
import asyncio
import json
import logging
import time
import uuid
from collections import Counter, deque
from typing import Any, AsyncGenerator, Dict, Optional

from src.request_context import correlation_context, current_request_id

logger = logging.getLogger(__name__)


class _Run:
    __slots__ = (
        "buffer",
        "subscribers",
        "status",
        "task",
        "evict_task",
        "run_id",
        "request_id",
    )

    def __init__(self) -> None:
        self.buffer: list = []          # ordered SSE event strings (replay log)
        self.subscribers: set = set()   # one asyncio.Queue per connected client
        self.status: str = "running"    # running | done | error | stopped
        self.task: Optional[asyncio.Task] = None
        self.evict_task: Optional[asyncio.Task] = None
        self.run_id: str = str(uuid.uuid4())
        self.request_id: str = current_request_id()


_RUNS: Dict[str, _Run] = {}

# Completed run metrics are deliberately in-memory only. They contain timing,
# routing and aggregate tool metadata, never prompt or response content.
_METRICS_HISTORY: deque[dict[str, Any]] = deque(maxlen=100)


def _record_metrics(run: _Run, metrics: dict[str, Any]) -> None:
    """Store one sanitized terminal metrics snapshot for a detached run."""
    snapshot = dict(metrics)
    snapshot.update(
        {
            "run_id": run.run_id,
            "request_id": run.request_id,
            "status": run.status,
            "event_count": len(run.buffer),
        }
    )
    _METRICS_HISTORY.append(snapshot)


def recent_metrics(limit: int = 50) -> list[dict[str, Any]]:
    """Return newest completed metrics first, bounded to retained history."""
    try:
        bounded = max(0, min(int(limit), len(_METRICS_HISTORY)))
    except (TypeError, ValueError):
        bounded = 50
    if bounded == 0:
        return []
    return list(reversed(_METRICS_HISTORY))[:bounded]


def _store_terminal_metrics(run: _Run) -> None:
    """Persist the final metrics event only after the run has a terminal status."""
    for event in reversed(run.buffer):
        try:
            payload = json.loads(event.removeprefix("data: ").strip())
        except (AttributeError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("type") != "metrics":
            continue
        data = payload.get("data")
        if isinstance(data, dict):
            _record_metrics(run, data)
        return


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile for a small in-memory sample."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if q <= 0:
        return ordered[0]
    if q >= 1:
        return ordered[-1]
    rank = max(1, int((q * len(ordered)) + 0.999999999))
    return ordered[min(len(ordered), rank) - 1]


def summarize_metrics(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """Return compact aggregate stats for the provided metrics slice."""
    status_counts = dict(Counter(str(item.get("status") or "unknown") for item in metrics))
    response_times = [
        float(item["response_time"])
        for item in metrics
        if isinstance(item, dict) and isinstance(item.get("response_time"), (int, float))
    ]
    avg_response_time = (
        round(sum(response_times) / len(response_times), 3)
        if response_times else None
    )
    p50_response_time = _percentile(response_times, 0.50)
    p95_response_time = _percentile(response_times, 0.95)

    if isinstance(p50_response_time, float):
        p50_response_time = round(p50_response_time, 3)
    if isinstance(p95_response_time, float):
        p95_response_time = round(p95_response_time, 3)

    return {
        "count": len(metrics),
        "status_counts": status_counts,
        "avg_response_time": avg_response_time,
        "p50_response_time": p50_response_time,
        "p95_response_time": p95_response_time,
    }


# How long a FINISHED run (and its full replay buffer) is retained after the
# last subscriber disconnects, so a reconnect within the window can still
# replay the result. After this, the run is evicted to bound memory — without
# it, every session that ever streamed kept its entire event log forever.
_EVICT_GRACE_S = 180


def _publish(run: _Run, ev: str) -> None:
    """Append one SSE event and fan it out to every live subscriber."""
    run.buffer.append(ev)
    seq = len(run.buffer) - 1
    for q in list(run.subscribers):
        try:
            q.put_nowait((seq, ev))
        except Exception:
            pass


def _schedule_evict(session_id: str) -> None:
    """(Re)arm a grace-period eviction for a terminal run with no subscribers.
    Identity-checked so a run that gets replaced/reused is never evicted by a
    stale timer."""
    run = _RUNS.get(session_id)
    if run is None:
        return
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()

    async def _evict(run_ref: _Run) -> None:
        try:
            await asyncio.sleep(_EVICT_GRACE_S)
        except asyncio.CancelledError:
            return
        cur = _RUNS.get(session_id)
        if cur is run_ref and cur.status != "running" and not cur.subscribers:
            _RUNS.pop(session_id, None)

    run.evict_task = asyncio.create_task(_evict(run))


def is_active(session_id: str) -> bool:
    r = _RUNS.get(session_id)
    return bool(r and r.status == "running")


def get_status(session_id: str) -> Optional[str]:
    r = _RUNS.get(session_id)
    return r.status if r else None


async def _drain(
    session_id: str,
    run: _Run,
    agen: AsyncGenerator[str, None],
    prev_task: Optional[asyncio.Task] = None,
    replaced_previous: bool = False,
) -> None:
    """Drain one concrete run without resolving replacements by session ID."""
    started = time.monotonic()
    error_type = "unknown"

    logger.info(
        "Agent run started session_id=%s replaced_previous=%s",
        session_id,
        str(replaced_previous).lower(),
    )

    try:
        # A rapid double-send cancels the previous run. Wait until its
        # cancellation handler has persisted the partial response before this
        # run starts writing to the same session.
        if prev_task is not None and not prev_task.done():
            await asyncio.wait({prev_task})

        async for ev in agen:
            _publish(run, ev)

        if run.status == "running":
            run.status = "done"

    except asyncio.CancelledError:
        run.status = "stopped"

        # Let the wrapped generator finalize and persist its partial response.
        try:
            await agen.aclose()
        except Exception:
            pass

    except Exception as exc:
        run.status = "error"
        error_type = type(exc).__name__

        _publish(
            run,
            "event: error\n"
            f"data: {json.dumps({'error': 'Agent run failed before completion.', 'status': 500})}\n\n",
        )
        _publish(run, "data: [DONE]\n\n")

    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        event_count = len(run.buffer)

        _store_terminal_metrics(run)

        if run.status == "done":
            logger.info(
                "Agent run finished session_id=%s status=%s "
                "duration_ms=%d event_count=%d",
                session_id,
                run.status,
                duration_ms,
                event_count,
            )
        elif run.status == "stopped":
            logger.info(
                "Agent run stopped session_id=%s status=%s "
                "duration_ms=%d event_count=%d",
                session_id,
                run.status,
                duration_ms,
                event_count,
            )
        else:
            logger.error(
                "Agent run failed session_id=%s status=%s "
                "duration_ms=%d event_count=%d error_type=%s",
                session_id,
                run.status,
                duration_ms,
                event_count,
                error_type,
            )

        # Wake every subscriber so its SSE connection can close.
        for q in list(run.subscribers):
            try:
                q.put_nowait((None, None))
            except Exception:
                pass

        # A replaced run no longer owns this session ID. It must not schedule
        # an eviction task for the newer run now stored under the same key.
        if _RUNS.get(session_id) is run:
            _schedule_evict(session_id)


def start(session_id: str, agen: AsyncGenerator[str, None]) -> _Run:
    """Start a detached run, replacing any active run for the session."""
    prev = _RUNS.get(session_id)
    prev_task: Optional[asyncio.Task] = None
    replaced_previous = bool(
        prev and prev.task and not prev.task.done()
    )

    if prev:
        if prev.task and not prev.task.done():
            prev.task.cancel()
            prev_task = prev.task
        if prev.evict_task and not prev.evict_task.done():
            prev.evict_task.cancel()

    run = _Run()
    _RUNS[session_id] = run

    # create_task copies the current Context. Bind both identifiers while the
    # detached task is created so its logs remain correlated after the HTTP
    # request and its SSE subscriber have ended.
    with correlation_context(
        request_id=run.request_id,
        agent_run_id=run.run_id,
    ):
        run.task = asyncio.create_task(
            _drain(
                session_id,
                run,
                agen,
                prev_task,
                replaced_previous,
            )
        )

    return run


async def subscribe(session_id: str) -> AsyncGenerator[str, None]:
    """Replay the run's buffer from the start, then stream live until it ends.
    Safe to call repeatedly (reconnect) and from multiple clients at once."""
    run = _RUNS.get(session_id)
    if run is None:
        return
    q: asyncio.Queue = asyncio.Queue()
    run.subscribers.add(q)            # register BEFORE replaying so nothing is missed
    # A live subscriber is connected — don't let a pending grace timer evict
    # the run out from under it mid-replay.
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()
    try:
        next_seq = 0
        while next_seq < len(run.buffer):
            yield run.buffer[next_seq]
            next_seq += 1
        if run.status != "running":
            return
        heartbeat_idx = 0
        while True:
            try:
                seq, ev = await asyncio.wait_for(q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                # Keep slow local models/proxies alive while they prefill before
                # the first token. SSE comments are ignored by the UI but reset
                # browser/proxy idle timers, which prevents "empty response"
                # disconnects on llama.cpp first-token latencies of 30s+.
                if run.status == "running":
                    heartbeat_idx += 1
                    yield f": heartbeat {heartbeat_idx}\n\n"
                    continue
                seq, ev = (None, None)
            if seq is None:            # end sentinel
                while next_seq < len(run.buffer):   # flush any tail the sentinel raced
                    yield run.buffer[next_seq]
                    next_seq += 1
                break
            if seq >= next_seq:        # skip events already replayed from the buffer
                yield ev
                next_seq = seq + 1
    finally:
        run.subscribers.discard(q)
        # Last subscriber gone on a finished run — (re)arm eviction so the
        # buffer doesn't linger indefinitely.
        if not run.subscribers and run.status != "running":
            _schedule_evict(session_id)


def stop(session_id: str) -> bool:
    """Cancel an in-flight run (the wrapped generator saves its partial)."""
    run = _RUNS.get(session_id)
    if run and run.task and not run.task.done():
        run.task.cancel()
        return True
    return False
