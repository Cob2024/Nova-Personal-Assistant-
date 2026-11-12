"""
Background task lifecycle management, concurrent execution, execution audit logging,
and stage-level performance telemetry for Nova.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Optional
import uuid

DATA = Path.home() / ".laptop_assistant"
PERF_FILE = DATA / "perf.jsonl"
EXEC_LOG = DATA / "execution.log"


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskRecord:
    """Tracked asynchronous operation with progress and cooperative cancellation."""

    id: str
    description: str
    status: str = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    progress: str = ""
    result: Any = None
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _future: Any = field(default=None, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set() or self.status == TaskStatus.CANCELLED

    def cancel(self) -> bool:
        self.cancel_event.set()
        if self.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            if self._future is not None:
                self._future.cancel()
            self.status = TaskStatus.CANCELLED
            self.completed_at = time.time()
            return True
        return False

    def elapsed_ms(self) -> float:
        start = self.started_at or self.created_at
        end = self.completed_at or time.time()
        return max(0.0, (end - start) * 1000.0)


def _accepts_cancel_event(fn: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return any(
        p.name == "cancel_event" or p.kind == inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )


class TaskManager:
    """Thread-pool task orchestrator supporting concurrent commands and cooperative cancellation."""

    def __init__(self, max_workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="nova-task")
        self._lock = threading.Lock()
        self._records: deque[TaskRecord] = deque(maxlen=60)
        self._by_id: dict[str, TaskRecord] = {}

    def submit(
        self,
        description: str,
        fn: Callable[..., Any],
        on_done: Optional[Callable[[TaskRecord], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> TaskRecord:
        tid = uuid.uuid4().hex[:6]
        rec = TaskRecord(id=tid, description=description, cancel_event=cancel_event or threading.Event())
        with self._lock:
            self._records.append(rec)
            self._by_id[tid] = rec
            if len(self._by_id) > 60:
                valid = {r.id for r in self._records}
                for stale in [k for k in self._by_id if k not in valid]:
                    self._by_id.pop(stale, None)

        accepts_cancel = _accepts_cancel_event(fn)

        def runner() -> Any:
            if rec.cancel_event.is_set():
                rec.status = TaskStatus.CANCELLED
                rec.completed_at = time.time()
                return None
            rec.status = TaskStatus.RUNNING
            rec.started_at = time.time()
            try:
                out = fn(cancel_event=rec.cancel_event) if accepts_cancel else fn()
                if rec.cancel_event.is_set():
                    rec.status = TaskStatus.CANCELLED
                else:
                    rec.result = out
                    rec.status = TaskStatus.COMPLETED
                return out
            except Exception as err:
                if rec.cancel_event.is_set():
                    rec.status = TaskStatus.CANCELLED
                else:
                    rec.error = str(err)
                    rec.status = TaskStatus.FAILED
                return None
            finally:
                rec.completed_at = time.time()
                if on_done is not None:
                    try:
                        on_done(rec)
                    except Exception:
                        pass

        rec._future = self._pool.submit(runner)
        return rec

    def run_parallel(
        self,
        items: list[tuple[str, Callable[..., Any]]],
        timeout: float = 20.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> list[TaskRecord]:
        """Run independent `(description, fn)` steps concurrently and return records in order."""
        if not items:
            return []
        shared_cancel = cancel_event or threading.Event()
        recs = [self.submit(desc, fn, cancel_event=shared_cancel) for desc, fn in items]
        futures = [r._future for r in recs if r._future is not None]
        if futures:
            done, not_done = wait_futures(futures, timeout=timeout)
            if not_done or shared_cancel.is_set():
                for rec in recs:
                    if rec.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                        rec.cancel()
        return recs

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._by_id.get(task_id)

    def active(self) -> list[TaskRecord]:
        with self._lock:
            return [r for r in self._records if r.status in (TaskStatus.PENDING, TaskStatus.RUNNING)]

    def recent(self, limit: int = 8) -> list[TaskRecord]:
        with self._lock:
            return list(self._records)[-max(1, int(limit)):]

    def cancel(self, task_id: Optional[str] = None) -> int:
        with self._lock:
            targets = (
                [self._by_id[task_id]] if task_id and task_id in self._by_id
                else [r for r in self._records if r.status in (TaskStatus.PENDING, TaskStatus.RUNNING)]
            )
        n = 0
        for rec in targets:
            if rec.cancel():
                n += 1
        return n

    def summary_text(self) -> str:
        act = self.active()
        if act:
            items = ", ".join(f"{r.description} ({r.status})" for r in act[:4])
            return f"{len(act)} active task{'s' * (len(act) != 1)}: {items}."
        rec = self.recent(3)
        if not rec:
            return "No recent background tasks."
        latest = rec[-1]
        return f"No active tasks. Last task {latest.description} finished with status {latest.status}."

    def shutdown(self) -> None:
        self.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)


class PerfMonitor:
    """Structured per-stage latency recorder for Nova pipelines."""

    def __init__(self, log_path: Path = PERF_FILE):
        self.log_path = Path(log_path)
        self._lock = threading.Lock()
        self._turns: deque[dict[str, float]] = deque(maxlen=150)

    def record_turn(self, timings: dict[str, float]) -> None:
        clean = {k: round(float(v), 2) for k, v in timings.items() if v is not None}
        if not clean:
            return
        entry = {"ts": round(time.time(), 3), **clean}
        with self._lock:
            self._turns.append(clean)
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                if self.log_path.exists() and self.log_path.stat().st_size > 1_000_000:
                    self.log_path.unlink(missing_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                pass

    def stats(self, last_n: int = 20) -> dict[str, dict[str, float]]:
        with self._lock:
            sample = list(self._turns)[-max(1, int(last_n)):]
        buckets: dict[str, list[float]] = {}
        for item in sample:
            for k, v in item.items():
                buckets.setdefault(k, []).append(v)
        out: dict[str, dict[str, float]] = {}
        for k, vals in buckets.items():
            s = sorted(vals)
            out[k] = {
                "last_ms": round(vals[-1], 1),
                "avg_ms": round(sum(vals) / len(vals), 1),
                "p50_ms": round(s[len(s) // 2], 1),
                "count": float(len(vals)),
            }
        return out

    def report_text(self) -> str:
        st = self.stats()
        if not st:
            return "No performance telemetry recorded yet in this session."
        parts = []
        for key, label in (
            ("stt_ms", "speech recognition"),
            ("route_ms", "intent routing"),
            ("llm_ttft_ms", "first AI token"),
            ("tts_first_ms", "TTS start"),
            ("total_ms", "end-to-end"),
        ):
            if key in st:
                parts.append(f"{label} averages {st[key]['avg_ms']:.0f} milliseconds")
        if not parts:
            parts = [f"{k}: {v['avg_ms']:.0f} ms" for k, v in list(st.items())[:4]]
        return "Latency report: " + ", ".join(parts) + "."


class ExecutionLogger:
    """Thread-safe structured audit trail for computer actions and shell commands."""

    def __init__(self, log_path: Path = EXEC_LOG):
        self.log_path = Path(log_path)
        self._lock = threading.Lock()

    def log(self, action: str, target: str = "", status: str = "ok", detail: str = "", duration_ms: float = 0.0) -> None:
        rec = {
            "ts": round(time.time(), 3),
            "action": action,
            "target": str(target)[:200],
            "status": status,
            "detail": str(detail)[:240],
            "ms": round(float(duration_ms), 2),
        }
        with self._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                if self.log_path.exists() and self.log_path.stat().st_size > 1_000_000:
                    self.log_path.unlink(missing_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError:
                pass
