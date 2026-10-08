"""In-process async ingestion task queue.

Implements a lightweight background processing queue using asyncio.Queue and
a pool of asyncio worker coroutines that run file ingestion in FastAPI's
thread pool executor (to avoid blocking the event loop on CPU-bound work).

Architecture:
    ┌──────────────────────────┐
    │  POST /api/files/async/  │
    │  → enqueue_task()        │
    │  → return 202 + task_id  │
    └──────────┬───────────────┘
               │ asyncio.Queue
    ┌──────────▼────────────────────────────────────┐
    │  Worker coroutines (ASYNC_WORKER_CONCURRENCY)  │
    │  Each worker calls loop.run_in_executor()      │
    │  → IngestService.ingest_stream() (sync/CPU)   │
    │  → updates task registry on completion         │
    └────────────────────────────────────────────────┘

Usage:
    # In application startup (lifespan):
    await task_queue.start_workers(ingest_service, n=4)

    # Enqueue a task:
    task_id = await task_queue.enqueue(content, filename)

    # Poll task status:
    task = task_queue.get_task(task_id)
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.domain import AsyncTask, FileRecord, FileStatus
from app.monitoring.metrics import ASYNC_QUEUE_DEPTH

if TYPE_CHECKING:
    from app.services.ingest import IngestService

logger = logging.getLogger("geomeasure.workers.task_queue")

# Number of concurrent async worker coroutines (each offloads to thread pool)
ASYNC_WORKER_CONCURRENCY = 4


@dataclass
class _TaskState:
    """Internal mutable task tracking state."""
    task_id: str
    file_id: str
    filename: str
    status: FileStatus
    created_at: str
    error: str | None = None
    content: bytes = field(default=b"", repr=False)


class AsyncTaskQueue:
    """Asyncio-backed in-process task queue for background file ingestion.

    Thread safety:
        The task registry (_tasks) is only mutated from the asyncio event loop.
        The actual CPU work runs in a ThreadPoolExecutor and does NOT touch
        _tasks directly — results are communicated back via asyncio callbacks.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_TaskState] = asyncio.Queue()
        self._tasks: dict[str, _TaskState] = {}
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._executor: ThreadPoolExecutor | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_workers(
        self,
        ingest_service: IngestService,
        n: int = ASYNC_WORKER_CONCURRENCY,
    ) -> None:
        """Start n background worker coroutines.

        Must be called from within the asyncio event loop (e.g., application
        lifespan startup).
        """
        if self._running:
            logger.warning("AsyncTaskQueue workers already started; ignoring duplicate start.")
            return

        self._queue = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="geo-ingest")
        self._running = True
        self._worker_tasks = [
            asyncio.create_task(self._worker(ingest_service), name=f"ingest-worker-{i}")
            for i in range(n)
        ]
        logger.info("AsyncTaskQueue started with %d workers.", n)

    async def stop_workers(self) -> None:
        """Gracefully shut down all workers and the thread pool."""
        self._running = False
        # Send sentinel values to unblock workers
        for _ in self._worker_tasks:
            await self._queue.put(None)  # type: ignore[arg-type]

        for t in self._worker_tasks:
            try:
                await asyncio.wait_for(t, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()

        if self._executor:
            self._executor.shutdown(wait=False)

        self._worker_tasks.clear()
        logger.info("AsyncTaskQueue workers stopped.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        content: bytes,
        filename: str,
        file_id: str | None = None,
    ) -> AsyncTask:
        """Enqueue a file for background ingestion.

        Args:
            content: Raw file bytes to be ingested.
            filename: Sanitized original filename.
            file_id: Optional pre-created file ID (for idempotency).

        Returns:
            AsyncTask snapshot in QUEUED status.
        """
        task_id = uuid.uuid4().hex
        fid = file_id or uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()

        state = _TaskState(
            task_id=task_id,
            file_id=fid,
            filename=filename,
            status=FileStatus.QUEUED,
            created_at=created_at,
            content=content,
        )
        self._tasks[task_id] = state
        await self._queue.put(state)

        ASYNC_QUEUE_DEPTH.set(self._queue.qsize())
        logger.info(
            "Enqueued task_id=%s file_id=%s filename='%s' (queue depth=%d)",
            task_id,
            fid,
            filename,
            self._queue.qsize(),
        )
        return self._task_snapshot(state)

    def get_task(self, task_id: str) -> AsyncTask | None:
        """Return the current task snapshot or None if not found."""
        state = self._tasks.get(task_id)
        if state is None:
            return None
        return self._task_snapshot(state)

    def list_tasks(self, limit: int = 50) -> list[AsyncTask]:
        """Return the most recent tasks (newest first)."""
        all_states = sorted(
            self._tasks.values(),
            key=lambda s: s.created_at,
            reverse=True,
        )
        return [self._task_snapshot(s) for s in all_states[:limit]]

    def queue_depth(self) -> int:
        """Return current number of pending items in the queue."""
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _worker(self, ingest_service: IngestService) -> None:
        """Worker coroutine that processes tasks from the queue."""
        loop = asyncio.get_running_loop()

        while self._running:
            try:
                state: _TaskState | None = await self._queue.get()
            except Exception:
                continue

            # Sentinel for graceful shutdown
            if state is None:
                self._queue.task_done()
                break

            ASYNC_QUEUE_DEPTH.set(self._queue.qsize())
            state.status = FileStatus.PROCESSING
            logger.info(
                "Worker processing task_id=%s filename='%s'",
                state.task_id,
                state.filename,
            )

            try:
                def _run_ingest(s: _TaskState = state) -> FileRecord:
                    return ingest_service.ingest_stream(
                        io.BytesIO(s.content), s.filename
                    )

                record = await loop.run_in_executor(
                    self._executor,
                    _run_ingest,
                )
                state.file_id = record.id
                state.status = FileStatus.COMPLETED
                logger.info(
                    "Task completed: task_id=%s file_id=%s features=%d",
                    state.task_id,
                    record.id,
                    record.feature_count,
                )

            except Exception as exc:
                state.status = FileStatus.FAILED
                state.error = str(exc)
                logger.error(
                    "Task failed: task_id=%s error=%s",
                    state.task_id,
                    exc,
                )

            finally:
                # Release memory immediately — content no longer needed
                state.content = b""
                self._queue.task_done()

    @staticmethod
    def _task_snapshot(state: _TaskState) -> AsyncTask:
        """Create an immutable AsyncTask domain object from mutable state."""
        return AsyncTask(
            task_id=state.task_id,
            file_id=state.file_id,
            status=state.status,
            filename=state.filename,
            created_at=state.created_at,
            error=state.error,
        )


# ---------------------------------------------------------------------------
# Module-level singleton — initialized during app lifespan
# ---------------------------------------------------------------------------

task_queue = AsyncTaskQueue()
