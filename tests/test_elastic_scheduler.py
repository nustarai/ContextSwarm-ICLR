from __future__ import annotations

import unittest

from contextswarm_mini.elastic_scheduler import DynamicScheduler, ElasticScheduler


class ElasticSchedulerTests(unittest.TestCase):
    def test_initial_agents_obey_total_slot_budget(self) -> None:
        scheduler = ElasticScheduler(
            ["task-a", "task-b", "task-c"], max_parallel=4, initial_agents=2, horizon=20
        )
        leases = [scheduler.next_assignment() for _ in range(5)]
        self.assertEqual(sum(lease is not None for lease in leases), 4)
        admitted = [lease.task_id for lease in leases if lease]
        self.assertEqual(set(admitted), {"task-a", "task-b", "task-c"})
        self.assertTrue(all(admitted.count(task_id) <= 2 for task_id in {"task-a", "task-b", "task-c"}))
        self.assertEqual(scheduler.active_slots, 4)
        self.assertEqual(scheduler.next_assignment(), None)

    def test_unsolved_finish_refills_and_solved_task_is_retired(self) -> None:
        scheduler = DynamicScheduler(["a", "b"], max_parallel=2, initial_agents=1, horizon=20)
        first = scheduler.claim()
        second = scheduler.claim()
        assert first is not None and second is not None
        scheduler.finish(first, solved=True)
        self.assertEqual(scheduler.solved_tasks, frozenset({"a"}))
        replacement = scheduler.next_agent()
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.task_id, "b")
        scheduler.finish(second, solved=False)
        retry = scheduler.admit()
        self.assertIsNotNone(retry)
        self.assertEqual(retry.task_id, "b")
        self.assertEqual(scheduler.active_slots, 2)
        self.assertTrue(all(lease.task_id != "a" for lease in scheduler.active()))

    def test_task_solved_does_not_cancel_running_leases(self) -> None:
        scheduler = ElasticScheduler(["a", "b"], max_parallel=3, initial_agents=2, horizon=20)
        leases = [scheduler.next_assignment() for _ in range(3)]
        running_a = [lease for lease in leases if lease and lease.task_id == "a"]
        self.assertEqual(len(running_a), 2)
        scheduler.task_solved("a")
        self.assertEqual(len(scheduler.active("a")), 2)
        self.assertEqual(scheduler.remaining_slots, 0)
        self.assertEqual(scheduler.cancel_task("a"), tuple(running_a))
        self.assertEqual(scheduler.remaining_slots, 2)
        self.assertTrue(all(lease.task_id == "b" for lease in scheduler.active()))

    def test_horizon_blocks_new_leases_but_allows_closeout(self) -> None:
        current = [10.0]
        scheduler = ElasticScheduler(["a"], max_parallel=1, initial_agents=1, horizon=5, clock=lambda: current[0])
        lease = scheduler.next_assignment()
        self.assertIsNotNone(lease)
        current[0] = 15.0
        self.assertTrue(scheduler.horizon_reached)
        self.assertIsNone(scheduler.next_assignment())
        scheduler.finish(lease)
        self.assertTrue(scheduler.done)

    def test_per_task_initial_mapping_and_snapshot(self) -> None:
        scheduler = ElasticScheduler(
            ["a", "b"], max_parallel=3, initial_agents={"a": 2, "b": 1}, horizon=None
        )
        leases = [scheduler.next_assignment() for _ in range(3)]
        self.assertEqual([lease.task_id for lease in leases if lease], ["a", "b", "a"])
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["remaining_slots"], 0)
        self.assertEqual(snapshot["tasks"]["a"]["initial_admitted"], 2)

    def test_explicit_task_admission_preserves_scheduler_invariants(self) -> None:
        current = [0.0]
        scheduler = ElasticScheduler(
            ["a", "b"],
            max_parallel=2,
            initial_agents=1,
            horizon=10,
            clock=lambda: current[0],
        )
        first = scheduler.next_assignment()
        second = scheduler.next_assignment()
        self.assertFalse(scheduler.has_pending_initial)
        assert first is not None and second is not None
        scheduler.finish(first)
        targeted = scheduler.next_assignment_for("b")
        self.assertIsNotNone(targeted)
        assert targeted is not None
        self.assertEqual(targeted.task_id, "b")
        self.assertEqual(len(scheduler.active("b")), 2)
        scheduler.task_solved("b")
        scheduler.finish(targeted)
        self.assertIsNone(scheduler.next_assignment_for("b"))
        current[0] = 10.0
        scheduler.finish(second)
        self.assertIsNone(scheduler.next_assignment_for("a"))


if __name__ == "__main__":
    unittest.main()
