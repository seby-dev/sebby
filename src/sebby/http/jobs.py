from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    id: str
    status: JobStatus = JobStatus.PENDING
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.monotonic)


class JobNotFoundError(Exception):
    pass


class JobStore:
    """A bounded, in-memory job store for async job-polling APIs.

    Not process-shared — one instance per running server process. When at
    capacity, the oldest DONE or ERROR job is evicted to make room for a
    new one; PENDING/RUNNING jobs are never evicted, so a store that's full
    of still-in-flight jobs raises `RuntimeError` on `create()` rather than
    silently dropping in-flight work.

    Status is always written last in `mark_done`/`mark_error` (after
    `result`/`error`), so a caller polling `get(job_id).status` and seeing
    DONE/ERROR is guaranteed to also see the corresponding result/error
    already set — no lock needed for that read.
    """

    def __init__(
        self, *, max_jobs: int = 1000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max_jobs = max_jobs
        self._clock = clock
        self._jobs: dict[str, Job] = {}

    def create(self) -> str:
        if len(self._jobs) >= self._max_jobs:
            self._evict_oldest_finished()
        job_id = str(uuid.uuid4())
        self._jobs[job_id] = Job(id=job_id, created_at=self._clock())
        return job_id

    def _evict_oldest_finished(self) -> None:
        finished = [j for j in self._jobs.values() if j.status in (JobStatus.DONE, JobStatus.ERROR)]
        if not finished:
            raise RuntimeError("job store is full and no finished jobs can be evicted")
        oldest = min(finished, key=lambda j: j.created_at)
        del self._jobs[oldest.id]

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise JobNotFoundError(job_id) from None

    def mark_running(self, job_id: str) -> None:
        self.get(job_id).status = JobStatus.RUNNING

    def mark_done(self, job_id: str, result: Any) -> None:
        job = self.get(job_id)
        job.result = result
        job.status = JobStatus.DONE

    def mark_error(self, job_id: str, error: str) -> None:
        job = self.get(job_id)
        job.error = error
        job.status = JobStatus.ERROR
