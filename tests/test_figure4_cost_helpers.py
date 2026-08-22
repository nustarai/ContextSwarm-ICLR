import json
from pathlib import Path
import tempfile
import unittest

from contextswarm_mini.runner import _allocation_runtime_metrics, _pi_token_usage


class Figure4CostHelperTests(unittest.TestCase):
    def test_reservation_slot_seconds_are_real_occupancy_not_latency(self) -> None:
        events = [
            {"event": "reservation_acquired", "reservation_id": "r1", "slots": 1, "acquired_at": 2.0},
            {"event": "reservation_completed", "reservation_id": "r1", "slots": 1, "outcome": "converted_to_solver", "completed_at": 5.0, "agent_id": "a1", "task_id": "q"},
            {"event": "agent_admitted", "agent_id": "a1", "task_id": "q", "admitted_at": 5.0},
            {"event": "agent_finished", "agent_id": "a1", "task_id": "q", "finished_at": 9.0},
        ]
        metrics = _allocation_runtime_metrics(
            events,
            run_started_monotonic=0.0,
            deadline=10.0,
            max_parallel=2,
            policy_latency_seconds=99.0,
        )
        self.assertEqual(metrics["scheduler_reserved_slot_seconds"], 3.0)
        self.assertEqual(metrics["solver_agent_seconds"], 4.0)
        self.assertEqual(metrics["scheduler_compute_seconds"], 3.0)
        self.assertEqual(metrics["scheduler_policy_latency_seconds"], 99.0)
        self.assertEqual(metrics["max_occupied_slots"], 1)
        self.assertLessEqual(metrics["occupied_slot_seconds"], metrics["capacity_seconds"])

    def test_token_usage_partitions_scheduler_and_solver_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pi_events.jsonl"
            rows = [
                {"actor_id": "solver-a", "session_id": "s", "input_tokens": 10, "output_tokens": 2},
                # Cumulative update: high-water mark, not an additional call.
                {"actor_id": "solver-a", "session_id": "s", "input_tokens": 15, "output_tokens": 3},
                {"actor_id": "allocation-scheduler-1", "session_id": "m", "input_tokens": 20, "output_tokens": 4},
                {"actor_id": "allocation-scheduler-1", "session_id": "m", "input_tokens": 25, "output_tokens": 5},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            solver = _pi_token_usage(path, scheduler=False)
            scheduler = _pi_token_usage(path, scheduler=True)
        self.assertEqual(solver["input_tokens"], 15)
        self.assertEqual(solver["output_tokens"], 3)
        self.assertEqual(solver["total_tokens"], 18)
        self.assertEqual(scheduler["input_tokens"], 25)
        self.assertEqual(scheduler["output_tokens"], 5)
        self.assertEqual(scheduler["total_tokens"], 30)


if __name__ == "__main__":
    unittest.main()
