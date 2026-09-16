from __future__ import annotations

import threading

import pytest

from sebby.http.jobs import JobNotFoundError, JobStatus, JobStore


def test_create_returns_unique_job_ids():
    store = JobStore()

    first = store.create()
    second = store.create()

    assert first != second


def test_get_returns_pending_job_after_create():
    store = JobStore()

    job_id = store.create()
    job = store.get(job_id)

    assert job.status == JobStatus.PENDING
    assert job.result is None
    assert job.error is None


def test_mark_running_updates_status():
    store = JobStore()
    job_id = store.create()

    store.mark_running(job_id)

    assert store.get(job_id).status == JobStatus.RUNNING


def test_mark_done_sets_result_and_status():
    store = JobStore()
    job_id = store.create()

    store.mark_done(job_id, {"answer": 42})

    job = store.get(job_id)
    assert job.status == JobStatus.DONE
    assert job.result == {"answer": 42}


def test_mark_error_sets_error_and_status():
    store = JobStore()
    job_id = store.create()

    store.mark_error(job_id, "something broke")

    job = store.get(job_id)
    assert job.status == JobStatus.ERROR
    assert job.error == "something broke"


def test_get_raises_for_unknown_job_id():
    store = JobStore()

    with pytest.raises(JobNotFoundError):
        store.get("does-not-exist")


def test_create_evicts_oldest_finished_job_when_at_capacity():
    clock = {"t": 0.0}
    store = JobStore(max_jobs=2, clock=lambda: clock["t"])

    clock["t"] = 1.0
    old_job_id = store.create()
    store.mark_done(old_job_id, "first")

    clock["t"] = 2.0
    other_job_id = store.create()
    store.mark_done(other_job_id, "second")

    clock["t"] = 3.0
    new_job_id = store.create()  # at capacity — should evict old_job_id (oldest finished)

    with pytest.raises(JobNotFoundError):
        store.get(old_job_id)
    assert store.get(other_job_id).result == "second"
    assert store.get(new_job_id).status == JobStatus.PENDING


def test_create_raises_when_full_and_nothing_finished_to_evict():
    store = JobStore(max_jobs=1)
    store.create()  # still PENDING — nothing finished to evict

    with pytest.raises(RuntimeError):
        store.create()


def test_create_evicts_finished_job_even_when_a_newer_in_flight_job_exists():
    clock = {"t": 0.0}
    store = JobStore(max_jobs=2, clock=lambda: clock["t"])

    clock["t"] = 1.0
    finished_job_id = store.create()
    store.mark_done(finished_job_id, "done")

    clock["t"] = 2.0
    in_flight_job_id = store.create()  # newer, but still PENDING

    clock["t"] = 3.0
    new_job_id = store.create()  # at capacity — must evict finished_job_id, NOT in_flight_job_id

    with pytest.raises(JobNotFoundError):
        store.get(finished_job_id)
    assert store.get(in_flight_job_id).status == JobStatus.PENDING
    assert store.get(new_job_id).status == JobStatus.PENDING


def test_create_is_thread_safe_under_concurrent_load():
    store = JobStore(max_jobs=1000)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(40):
                store.create()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(store._jobs) == 320
    assert len(store._jobs) <= 1000
