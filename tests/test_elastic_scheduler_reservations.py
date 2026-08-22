from __future__ import annotations

import unittest

from contextswarm_mini.elastic_scheduler import ElasticScheduler


class ElasticSchedulerReservationTests(unittest.TestCase):
    def test_reservation_uses_occupied_capacity_without_changing_active_slots(self) -> None:
        scheduler = ElasticScheduler(["a"], max_parallel=2, horizon=20)

        held = scheduler.acquire_reservation(purpose="llm_scheduler")

        self.assertIsNotNone(held)
        self.assertEqual(scheduler.active_slots, 0)
        self.assertEqual(scheduler.reservation_slots, 1)
        self.assertEqual(scheduler.occupied_slots, 1)
        self.assertEqual(scheduler.remaining_slots, 1)
        self.assertIsNotNone(scheduler.next_assignment())
        self.assertIsNone(scheduler.next_assignment())
        self.assertIsNone(scheduler.next_assignment_for("a"))

    def test_stable_ids_release_idempotently_and_record_outcome(self) -> None:
        scheduler = ElasticScheduler(["a"], max_parallel=3, horizon=20)
        first = scheduler.acquire_reservation(purpose="scheduler")
        second = scheduler.acquire_reservation(purpose="evaluator")
        assert first is not None and second is not None

        self.assertEqual(first.reservation_id, "reservation-1")
        self.assertEqual(second.reservation_id, "reservation-2")
        self.assertTrue(
            scheduler.release_reservation(first, reason="provider_error", now=3.0)
        )
        self.assertFalse(scheduler.release_reservation(first))

        event = scheduler.history()[-1]
        self.assertEqual(event["event"], "reservation_completed")
        self.assertEqual(event["reservation_id"], "reservation-1")
        self.assertEqual(event["outcome"], "released")
        self.assertEqual(event["reason"], "provider_error")
        self.assertEqual(scheduler.reservation_slots, 1)

    def test_admit_reserved_atomically_converts_capacity(self) -> None:
        scheduler = ElasticScheduler(["a"], max_parallel=1, horizon=20)
        held = scheduler.acquire_reservation(purpose="llm_scheduler")
        assert held is not None

        assignment = scheduler.admit_reserved(held, "a", now=2.0)

        self.assertIsNotNone(assignment)
        self.assertEqual(scheduler.active_slots, 1)
        self.assertEqual(scheduler.reservation_slots, 0)
        self.assertEqual(scheduler.occupied_slots, 1)
        self.assertFalse(scheduler.release_reservation(held))
        conversion = scheduler.history()[-1]
        self.assertEqual(conversion["outcome"], "converted_to_solver")
        self.assertEqual(conversion["reservation_id"], held.reservation_id)
        self.assertEqual(conversion["agent_id"], assignment.agent_id)

    def test_multi_slot_reservation_can_convert_one_and_release_remainder(self) -> None:
        scheduler = ElasticScheduler(["a"], max_parallel=3, horizon=20)
        held = scheduler.acquire_reservation(
            slots=2, purpose="batched_scheduler", now=1.0
        )
        assert held is not None

        assignment = scheduler.admit_reserved(held.reservation_id, "a", now=2.0)

        self.assertIsNotNone(assignment)
        self.assertEqual(scheduler.active_slots, 1)
        self.assertEqual(scheduler.reservation_slots, 1)
        self.assertEqual(scheduler.occupied_slots, 2)
        self.assertTrue(scheduler.release_reservation(held, reason="batch_complete"))
        self.assertEqual(scheduler.occupied_slots, 1)
        outcomes = [
            event["outcome"]
            for event in scheduler.history()
            if event["event"] == "reservation_completed"
        ]
        self.assertEqual(outcomes, ["converted_to_solver", "released"])

    def test_rejected_conversion_keeps_reservation_for_explicit_cleanup(self) -> None:
        current = [0.0]
        scheduler = ElasticScheduler(
            ["a"], max_parallel=1, horizon=5, clock=lambda: current[0]
        )
        held = scheduler.acquire_reservation(purpose="llm_scheduler")
        assert held is not None
        current[0] = 5.0

        self.assertIsNone(scheduler.admit_reserved(held, "a"))
        self.assertEqual(scheduler.reservation_slots, 1)
        self.assertTrue(scheduler.release_reservation(held, reason="horizon_reached"))
        self.assertEqual(scheduler.occupied_slots, 0)

    def test_context_manager_releases_on_error(self) -> None:
        scheduler = ElasticScheduler(["a"], max_parallel=1, horizon=20)

        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            with scheduler.reservation(purpose="llm_scheduler") as held:
                self.assertIsNotNone(held)
                self.assertEqual(scheduler.reservation_slots, 1)
                raise RuntimeError("provider failed")

        self.assertEqual(scheduler.reservation_slots, 0)
        self.assertEqual(scheduler.remaining_slots, 1)
        self.assertEqual(scheduler.history()[-1]["outcome"], "released")

    def test_snapshot_exposes_reservations_and_capacity_totals(self) -> None:
        scheduler = ElasticScheduler(["a"], max_parallel=4, horizon=20)
        held = scheduler.acquire_reservation(slots=2, purpose="llm_scheduler")
        assert held is not None
        scheduler.next_assignment()

        snapshot = scheduler.snapshot()

        self.assertEqual(snapshot["active_slots"], 1)
        self.assertEqual(snapshot["reservation_slots"], 2)
        self.assertEqual(snapshot["occupied_slots"], 3)
        self.assertEqual(snapshot["remaining_slots"], 1)
        self.assertEqual(
            snapshot["reservations"][held.reservation_id]["purpose"],
            "llm_scheduler",
        )

    def test_invalid_or_oversized_reservations_do_not_mutate_state(self) -> None:
        scheduler = ElasticScheduler(["a"], max_parallel=2, horizon=20)

        self.assertIsNone(
            scheduler.acquire_reservation(slots=3, purpose="llm_scheduler")
        )
        with self.assertRaisesRegex(ValueError, "slots"):
            scheduler.acquire_reservation(slots=0)
        with self.assertRaisesRegex(ValueError, "purpose"):
            scheduler.acquire_reservation(purpose=" ")
        self.assertEqual(scheduler.occupied_slots, 0)
        self.assertEqual(scheduler.history(), ())


if __name__ == "__main__":
    unittest.main()
