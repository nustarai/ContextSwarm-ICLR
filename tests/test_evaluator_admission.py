from __future__ import annotations

import threading
import time
import unittest

from contextswarm_mini.evaluator_broker import BrokerError, _PriorityAdmission


class EvaluatorAdmissionTests(unittest.TestCase):
    def test_waiter_uses_the_remaining_deadline(self) -> None:
        admission = _PriorityAdmission(1, 0)
        outcome: list[str] = []

        def wait_for_slot() -> None:
            try:
                with admission.acquire("agent_local", deadline=time.monotonic() + 0.5):
                    outcome.append("admitted")
            except BrokerError:
                outcome.append("timed_out")

        with admission.acquire("agent_local", deadline=time.monotonic() + 1):
            thread = threading.Thread(target=wait_for_slot)
            thread.start()
            time.sleep(0.05)
            self.assertEqual(outcome, [])
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["admitted"])

    def test_reserved_capacity_remains_available_to_official_closeout(self) -> None:
        admission = _PriorityAdmission(2, 1)
        local_outcome: list[str] = []

        def second_local() -> None:
            try:
                with admission.acquire("formal_query", deadline=time.monotonic() + 0.08):
                    local_outcome.append("admitted")
            except BrokerError:
                local_outcome.append("timed_out")

        with admission.acquire("agent_local", deadline=time.monotonic() + 1):
            thread = threading.Thread(target=second_local)
            thread.start()
            with admission.acquire("official", deadline=time.monotonic() + 0.5):
                thread.join(timeout=0.5)

        self.assertEqual(local_outcome, ["timed_out"])


if __name__ == "__main__":
    unittest.main()
