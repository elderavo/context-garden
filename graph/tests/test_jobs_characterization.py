from __future__ import annotations

import asyncio
import unittest

from graph import jobs


class _FakeOrchestrator:
    def __init__(self, index_delay: float = 0.0) -> None:
        self.index_delay = index_delay
        self.observed: list[str] = []

    async def execute_sync(self, *, job: jobs.Job, log):
        log(f"sync {job.workspace_id}")
        await asyncio.sleep(0)

    async def execute_index(self, *, job: jobs.Job, log):
        self.observed.append(job.status)
        log("Triggering daemon reindex...")
        await asyncio.sleep(self.index_delay)
        log("Reindex queued.")


class JobsCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        jobs._jobs.clear()
        jobs._worker_task = None
        jobs._sync_orchestrator = _FakeOrchestrator()

    async def test_index_job_transitions_pending_running_done(self) -> None:
        orchestrator = _FakeOrchestrator()
        jobs._sync_orchestrator = orchestrator

        job = jobs.enqueue_index("workspace-1", "alpha", "manual")
        self.assertEqual(job.status, "pending")
        self.assertIsNotNone(jobs._worker_task)
        await jobs._worker_task

        self.assertEqual(orchestrator.observed, ["running"])
        self.assertEqual(job.status, "done")
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.completed_at)

    async def test_active_index_job_is_deduped_for_same_workspace(self) -> None:
        jobs._sync_orchestrator = _FakeOrchestrator(index_delay=0.01)

        first = jobs.enqueue_index("workspace-1", "alpha", "manual")
        second = jobs.enqueue_index("workspace-1", "alpha", "manual")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(jobs._jobs), 1)
        await jobs._worker_task


if __name__ == "__main__":
    unittest.main()
