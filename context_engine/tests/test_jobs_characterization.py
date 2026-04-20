from __future__ import annotations

import asyncio
import unittest

from context_engine.core import jobs


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


class _ConcurrencyOrchestrator:
    def __init__(self) -> None:
        self.running: set[str] = set()
        self.max_parallel = 0
        self.calls: list[tuple[str, str]] = []

    async def execute_sync(self, *, job: jobs.Job, log):
        self.calls.append(("sync", job.workspace_id))
        self.running.add(job.workspace_id)
        self.max_parallel = max(self.max_parallel, len(self.running))
        await asyncio.sleep(0.05)
        self.running.remove(job.workspace_id)

    async def execute_index(self, *, job: jobs.Job, log):
        self.calls.append(("index", job.workspace_id))
        self.running.add(job.workspace_id)
        self.max_parallel = max(self.max_parallel, len(self.running))
        await asyncio.sleep(0.05)
        self.running.remove(job.workspace_id)


class JobsCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        jobs._jobs.clear()
        jobs._worker_task = None
        jobs._workspace_queues.clear()
        for task in jobs._workspace_worker_tasks.values():
            task.cancel()
        jobs._workspace_worker_tasks.clear()
        jobs._sync_orchestrator = _FakeOrchestrator()

    async def _await_workers(self) -> None:
        if jobs._workspace_worker_tasks:
            await asyncio.gather(*list(jobs._workspace_worker_tasks.values()))

    async def test_index_job_transitions_pending_running_done(self) -> None:
        orchestrator = _FakeOrchestrator()
        jobs._sync_orchestrator = orchestrator

        job = jobs.enqueue_index("workspace-1", "alpha", "manual")
        self.assertEqual(job.status, "pending")
        self.assertIn("workspace-1", jobs._workspace_worker_tasks)
        self.assertIsNotNone(jobs._worker_task)
        await self._await_workers()

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
        await self._await_workers()

    async def test_jobs_run_concurrently_across_workspaces(self) -> None:
        orchestrator = _ConcurrencyOrchestrator()
        jobs._sync_orchestrator = orchestrator

        jobs.enqueue_index("workspace-1", "alpha", "manual")
        jobs.enqueue_index("workspace-2", "bravo", "manual")
        await self._await_workers()

        self.assertGreaterEqual(orchestrator.max_parallel, 2)

    async def test_jobs_preserve_order_within_workspace(self) -> None:
        orchestrator = _ConcurrencyOrchestrator()
        jobs._sync_orchestrator = orchestrator

        jobs.enqueue_sync("workspace-1", "alpha", "manual")
        jobs.enqueue_index("workspace-1", "alpha", "manual")
        await self._await_workers()

        self.assertEqual(orchestrator.calls, [("sync", "workspace-1"), ("index", "workspace-1")])


if __name__ == "__main__":
    unittest.main()
