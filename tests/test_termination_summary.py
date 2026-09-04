from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from contextswarm_mini.agent_recovery import run_with_recovery
from contextswarm_mini.config import (
    TerminationSummaryConfig,
    load_config,
)
from contextswarm_mini.cps import CPSStore, make_policy
from contextswarm_mini.models import AgentResult, Verdict
from contextswarm_mini.judge_broker import JudgeBroker
from contextswarm_mini.pi_agent import PiAgent
from contextswarm_mini.prompts import build_termination_summary_prompt
from contextswarm_mini.runner import (
    RunLogger,
    _TerminationSummaryCancelEvent,
    _run_elastic_cps,
    _termination_summary_final_evidence,
    load_tasks,
)


ROOT = Path(__file__).resolve().parents[1]


def _fake_config(fake: Path, *, timeout: int = 1):
    return replace(
        load_config("configs/smoke.toml", ROOT),
        pi_binary=str(fake),
        aisw_enabled=False,
        pi_timeout_seconds=timeout,
    )


def _write_fake(root: Path, source: str) -> Path:
    path = root / "fake-pi"
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o755)
    return path


class _Broker:
    @contextmanager
    def session(self, **_kwargs):
        yield {
            "CONTEXTSWARM_JUDGE_URL": "http://127.0.0.1:1/test-token",
            "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "9999999999999",
        }


def _post_capability(
    url: str,
    operation: str,
    payload: dict[str, object],
) -> dict[str, object]:
    request = Request(
        f"{url}/{operation}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        value = json.loads(response.read())
    assert isinstance(value, dict)
    return value


class _BrokerEvaluator:
    def expected_task_contract_sha256(self, _task) -> str:
        return "a" * 64

    def probe(self, task, _candidate, *, deadline_monotonic=None) -> Verdict:
        del deadline_monotonic
        return Verdict(
            task.slug,
            "VERIFY_FAIL",
            0.0,
            0.0,
            judge_job_id="closeout-test-job",
            task_contract_sha256="a" * 64,
        )


class _SkippedEvaluator:
    is_mock_evaluator = True

    def expected_task_contract_sha256(self, _task) -> str:
        return "a" * 64

    def evaluate(self, task, candidate_path: Path, **_kwargs) -> Verdict:
        return Verdict(
            task.slug,
            "VERIFY_FAIL",
            0.0,
            0.0,
            {"mock": True, "candidate_bytes": candidate_path.stat().st_size},
            candidate_sha256="b" * 64,
            task_contract_sha256="a" * 64,
        )


class _PublishingTimeoutPi:
    def __init__(self, store: CPSStore):
        self.store = store
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        prompt = str(kwargs.get("termination_summary_prompt") or "")
        closeout_match = re.search(r"forced_closeout:([A-Za-z0-9_-]+)", prompt)
        closeout_tag = (
            f"forced_closeout:{closeout_match.group(1)}"
            if closeout_match is not None
            else "forced_closeout:test"
        )
        self.store.create_piece(
            task_id=str(kwargs["task_id"]),
            author=str(kwargs["actor_id"]),
            kind="termination_summary",
            title="termination_summary: partial route",
            body=(
                "new_findings: partial route\n"
                "counterexamples_or_ruled_out: none\n"
                "validation_feedback: VERIFY_FAIL\n"
                "next_step: try the remaining lemma"
            ),
            tags=["termination_summary", closeout_tag],
        )
        now = "2026-01-01T00:00:00+00:00"
        return AgentResult(
            agent_id=str(kwargs["actor_id"]),
            task_id=str(kwargs["task_id"]),
            episode=int(kwargs["episode"]),
            returncode=124,
            started_at=now,
            finished_at=now,
            timed_out=True,
            termination_summary_requested=True,
            termination_summary_request_sent=True,
            termination_summary_completed=True,
            termination_summary_reason="timeout",
        )


class _TimeoutThenSuccessPi:
    """Emit one closeout receipt, then a normal recovery result.

    This exercises the boundary where the semantic-summary hook must run
    before ``run_with_recovery`` launches its replacement process.  A runner
    that audits only the final successful result would lose the first piece.
    """

    def __init__(self, store: CPSStore):
        self.store = store
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        attempt = len(self.calls) - 1
        now = "2026-01-01T00:00:00+00:00"
        if attempt:
            return AgentResult(
                agent_id=str(kwargs["actor_id"]),
                task_id=str(kwargs["task_id"]),
                episode=int(kwargs["episode"]),
                returncode=0,
                started_at=now,
                finished_at=now,
            )
        prompt = str(kwargs.get("termination_summary_prompt") or "")
        closeout_match = re.search(r"forced_closeout:([A-Za-z0-9_-]+)", prompt)
        closeout_tag = (
            f"forced_closeout:{closeout_match.group(1)}"
            if closeout_match is not None
            else "forced_closeout:test"
        )
        self.store.create_piece(
            task_id=str(kwargs["task_id"]),
            author=str(kwargs["actor_id"]),
            kind="termination_summary",
            title="termination_summary: recovered route",
            body="new_findings: first attempt\nnext_step: continue",
            tags=[closeout_tag],
        )
        return AgentResult(
            agent_id=str(kwargs["actor_id"]),
            task_id=str(kwargs["task_id"]),
            episode=int(kwargs["episode"]),
            returncode=124,
            started_at=now,
            finished_at=now,
            timed_out=True,
            termination_summary_requested=True,
            termination_summary_request_sent=True,
            termination_summary_completed=True,
            termination_summary_reason="timeout",
        )


class TerminationSummaryConfigTests(unittest.TestCase):
    def test_treatment_enables_summary_and_disables_checkpoint(self) -> None:
        baseline = load_config("configs/formal_1h_cps32_profiled_clean.toml", ROOT)
        treatment = load_config(
            "configs/formal_1h_cps32_profiled_termination_summary.toml", ROOT
        )
        self.assertFalse(baseline.termination_summary.enabled)
        self.assertFalse(treatment.checkpoint.enabled)
        self.assertEqual(
            treatment.termination_summary,
            TerminationSummaryConfig(
                enabled=True,
                grace_seconds=45.0,
                on_timeout=True,
                on_cancel=True,
                max_prompt_chars=4_000,
            ),
        )
        base_public = baseline.public_dict()
        treatment_public = treatment.public_dict()
        base_public.pop("name")
        treatment_public.pop("name")
        base_public.pop("termination_summary")
        treatment_public.pop("termination_summary")
        self.assertEqual(base_public, treatment_public)

    def test_enabled_summary_requires_shared_cps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "invalid.toml"
            manifest.write_text(
                'extends = ["' + str(ROOT / "configs" / "smoke.toml") + '"]\n'
                "[experiment]\ncommunication = \"none\"\n"
                "[termination_summary]\nenabled = true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires a CPS run"):
                load_config(manifest, ROOT)

    def test_summary_bounds_reject_ambiguous_or_unbounded_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for key, value, expected in (
                ("grace_seconds", "true", "grace_seconds must be a finite number"),
                ("grace_seconds", "0.5", "grace_seconds must be between 1 and 300"),
                ("max_prompt_chars", "9000", "max_prompt_chars must be between 512 and 8000"),
            ):
                manifest = root / f"invalid-{key}-{value}.toml"
                manifest.write_text(
                    'extends = ["' + str(ROOT / "configs" / "smoke.toml") + '"]\n'
                    "[termination_summary]\n"
                    "enabled = true\n"
                    f"{key} = {value}\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, expected):
                    load_config(manifest, ROOT)


class TerminationSummaryPromptTests(unittest.TestCase):
    def test_prompt_is_same_agent_cps_closeout_not_checkpoint(self) -> None:
        task = load_tasks(load_config("configs/smoke.toml", ROOT))[0]
        prompt = build_termination_summary_prompt(
            task,
            reason="timeout",
            closeout_id="abc123",
        )
        self.assertIn('kind="termination_summary"', prompt)
        self.assertIn("cps_search", prompt)
        self.assertIn("cps_publish", prompt)
        self.assertIn("forced_closeout:abc123", prompt)
        self.assertIn("same", prompt.lower())
        self.assertNotIn("checkpoint/", prompt)
        self.assertNotIn("result.lean", prompt)


class PiTerminationSummaryTests(unittest.TestCase):
    def test_timeout_sends_steer_and_keeps_terminal_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys\n"
                "json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'type':'response','success':True}), flush=True)\n"
                "print(json.dumps({'type':'agent_start'}), flush=True)\n"
                "while True:\n"
                " line = sys.stdin.readline()\n"
                " if not line: break\n"
                " command = json.loads(line)\n"
                " if command.get('type') == 'steer':\n"
                "  print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
                "  print(json.dumps({'type':'turn_start'}), flush=True)\n"
                "  print(json.dumps({'type':'message_start','message':{'role':'user','content':[{'type':'text','text':'publish closeout'}]}}), flush=True)\n"
                "  print(json.dumps({'type':'message_update','assistantMessageEvent':{'type':'text_delta','delta':'closeout response'}}), flush=True)\n"
                "  print(json.dumps({'type':'agent_end','willRetry':False}), flush=True)\n"
                "  print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "  break\n",
            )
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.25,
            )
            self.assertTrue(result.timed_out)
            self.assertTrue(result.termination_summary_requested)
            self.assertTrue(result.termination_summary_request_sent)
            self.assertTrue(result.termination_summary_completed)
            self.assertEqual(result.returncode, 124)
            self.assertIn("closeout response", result.output_tail)

    def test_multiturn_closeout_keeps_completion_evidence(self) -> None:
        """Continuation turns must not erase the matching closeout message."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys, time\n"
                "request=json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'id':request['id'],'type':'response','success':True}), flush=True)\n"
                "print(json.dumps({'type':'agent_start'}), flush=True)\n"
                "time.sleep(1.2)\n"
                "while True:\n"
                " line=sys.stdin.readline()\n"
                " if not line: break\n"
                " command=json.loads(line)\n"
                " if command.get('type') != 'steer': continue\n"
                " print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
                " print(json.dumps({'type':'turn_start'}), flush=True)\n"
                " print(json.dumps({'type':'message_start','message':{'role':'user','content':[{'type':'text','text':command.get('message','')}]}}), flush=True)\n"
                " print(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'toolUse'}}), flush=True)\n"
                " print(json.dumps({'type':'turn_end'}), flush=True)\n"
                " print(json.dumps({'type':'turn_start'}), flush=True)\n"
                " print(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'stop'}}), flush=True)\n"
                " print(json.dumps({'type':'agent_end','willRetry':False}), flush=True)\n"
                " print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                " break\n",
            )
            result = PiAgent(_fake_config(fake, timeout=2)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.5,
            )
            self.assertTrue(result.timed_out)
            self.assertTrue(result.termination_summary_requested)
            self.assertTrue(result.termination_summary_request_sent)
            self.assertTrue(result.termination_summary_completed)
            # The invocation remains a timeout for recovery/accounting even
            # though its cooperative closeout settled cleanly.
            self.assertEqual(result.returncode, 124)

    def test_normal_completion_does_not_receive_steer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys\n"
                "request=json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'id':request['id'],'type':'response','success':True}), flush=True)\n"
                "print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "sys.stdin.read()\n",
            )
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.25,
            )
            self.assertEqual(result.returncode, 0, result.error_tail)
            self.assertFalse(result.termination_summary_requested)
            self.assertFalse(result.termination_summary_completed)

    def test_live_provider_error_receives_same_session_closeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys\n"
                "request=json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'id':request['id'],'type':'response','success':True}), flush=True)\n"
                "print(json.dumps({'type':'agent_start'}), flush=True)\n"
                "print(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'error','errorMessage':'provider failed'}}), flush=True)\n"
                "print(json.dumps({'type':'agent_end','willRetry':False}), flush=True)\n"
                "print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "while True:\n"
                " line=sys.stdin.readline()\n"
                " if not line: break\n"
                " command=json.loads(line)\n"
                # The provider error has already emitted agent_settled, so a
                # native Pi client must use an idle-session prompt rather than
                # a steer that would only queue behind a completed run.
                " if command.get('type') == 'prompt':\n"
                "  print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
                "  print(json.dumps({'type':'turn_start'}), flush=True)\n"
                "  print(json.dumps({'type':'message_start','message':{'role':'user','content':[{'type':'text','text':command.get('message','')}]}}), flush=True)\n"
                "  print(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'stop'}}), flush=True)\n"
                "  print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "  break\n",
            )
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.25,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(result.timed_out)
            self.assertTrue(result.termination_summary_requested)
            self.assertTrue(result.termination_summary_request_sent)
            self.assertTrue(result.termination_summary_completed)
            self.assertEqual(result.termination_summary_reason, "error")

    def test_live_extension_error_receives_same_session_closeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys\n"
                "request=json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'id':request['id'],'type':'response','success':True}), flush=True)\n"
                "print(json.dumps({'type':'agent_start'}), flush=True)\n"
                "print(json.dumps({'type':'extension_error','error':'extension failed'}), flush=True)\n"
                "print(json.dumps({'type':'agent_end','willRetry':False}), flush=True)\n"
                "print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "while True:\n"
                " line=sys.stdin.readline()\n"
                " if not line: break\n"
                " command=json.loads(line)\n"
                " if command.get('type') == 'prompt':\n"
                "  print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
                "  print(json.dumps({'type':'turn_start'}), flush=True)\n"
                "  print(json.dumps({'type':'message_start','message':{'role':'user','content':[{'type':'text','text':command.get('message','')}]}}), flush=True)\n"
                "  print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "  break\n",
            )
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.25,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.termination_summary_requested)
            self.assertTrue(result.termination_summary_request_sent)
            self.assertTrue(result.termination_summary_completed)
            self.assertEqual(result.termination_summary_reason, "error")

    def test_rejected_initial_prompt_gets_idle_session_closeout(self) -> None:
        """A live session may reject the first prompt but still hold prior context."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys\n"
                "request=json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'id':request['id'],'type':'response','success':False,'error':'provider rejected'}), flush=True)\n"
                "while True:\n"
                " line=sys.stdin.readline()\n"
                " if not line: break\n"
                " command=json.loads(line)\n"
                " if command.get('type') == 'prompt':\n"
                "  print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
                "  print(json.dumps({'type':'turn_start'}), flush=True)\n"
                "  print(json.dumps({'type':'message_start','message':{'role':'user','content':[{'type':'text','text':command.get('message','')}]}}), flush=True)\n"
                "  print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "  break\n",
            )
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.25,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.termination_summary_requested)
            self.assertTrue(result.termination_summary_request_sent)
            self.assertTrue(result.termination_summary_completed)
            self.assertEqual(result.termination_summary_reason, "error")

    def test_cancel_masks_broker_view_only_during_closeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys\n"
                "json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'type':'response','success':True}), flush=True)\n"
                "while True:\n"
                " line=sys.stdin.readline()\n"
                " if not line: break\n"
                " command=json.loads(line)\n"
                " if command.get('type') == 'steer':\n"
                "  print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
                "  print(json.dumps({'type':'turn_start'}), flush=True)\n"
                "  print(json.dumps({'type':'message_start','message':{'role':'user','content':[{'type':'text','text':'publish closeout'}]}}), flush=True)\n"
                "  print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "  break\n",
            )
            source = threading.Event()
            cancel = _TerminationSummaryCancelEvent(source)
            threading.Timer(0.15, source.set).start()
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                cancel_event=cancel,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.5,
            )
            self.assertTrue(result.cancelled)
            self.assertTrue(result.termination_summary_requested)
            self.assertTrue(result.termination_summary_request_sent)
            self.assertTrue(result.termination_summary_completed)
            self.assertTrue(cancel.is_set())

    def test_timeout_closeout_can_be_disabled_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys, time\n"
                "json.loads(sys.stdin.readline())\n"
                "time.sleep(5)\n",
            )
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.25,
                termination_summary_on_timeout=False,
            )
            self.assertTrue(result.timed_out)
            self.assertFalse(result.termination_summary_requested)

    def test_disabled_timeout_trigger_keeps_hard_invocation_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys, time\n"
                "json.loads(sys.stdin.readline())\n"
                "time.sleep(2)\n",
            )
            started = time.monotonic()
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.5,
                termination_summary_on_timeout=False,
            )
            elapsed = time.monotonic() - started
            self.assertTrue(result.timed_out)
            self.assertFalse(result.termination_summary_requested)
            # The disabled trigger must not turn a one-second Pi budget into a
            # half-second budget merely because a grace value is present.
            self.assertGreaterEqual(elapsed, 0.85)

    def test_original_settled_event_race_does_not_skip_closeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys, time\n"
                "json.loads(sys.stdin.readline())\n"
                "time.sleep(0.82)\n"
                "print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "while True:\n"
                " line=sys.stdin.readline()\n"
                " if not line: break\n"
                " command=json.loads(line)\n"
                " if command.get('type') == 'steer':\n"
                "  print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
                "  print(json.dumps({'type':'turn_start'}), flush=True)\n"
                "  print(json.dumps({'type':'message_start','message':{'role':'user','content':[{'type':'text','text':'publish closeout'}]}}), flush=True)\n"
                "  print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "  break\n",
            )
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.25,
            )
            self.assertTrue(result.termination_summary_requested)
            self.assertTrue(result.termination_summary_completed)

    def test_steer_ack_does_not_count_buffered_settlement_as_closeout(self) -> None:
        """A queue-acceptance response is not proof that the steer ran."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys, time\n"
                "json.loads(sys.stdin.readline())\n"
                "time.sleep(0.82)\n"
                "while True:\n"
                " line=sys.stdin.readline()\n"
                " if not line: break\n"
                " command=json.loads(line)\n"
                " if command.get('type') == 'steer':\n"
                "  print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
                "  # This is the old session settlement, not a closeout turn.\n"
                "  print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "  time.sleep(2)\n"
                "  break\n",
            )
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.25,
            )
            self.assertTrue(result.termination_summary_requested)
            self.assertFalse(result.termination_summary_completed)

    def test_old_settlement_before_matching_closeout_message_is_not_completion(self) -> None:
        """A stale settlement must not become valid merely when a later message arrives."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(
                root,
                "import json, sys, time\n"
                "json.loads(sys.stdin.readline())\n"
                "time.sleep(0.82)\n"
                "print(json.dumps({'type':'agent_settled'}), flush=True)\n"
                "while True:\n"
                " line=sys.stdin.readline()\n"
                " if not line: break\n"
                " command=json.loads(line)\n"
                " if command.get('type') == 'steer':\n"
                "  print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
                "  print(json.dumps({'type':'turn_start'}), flush=True)\n"
                "  print(json.dumps({'type':'message_start','message':{'role':'user','content':[{'type':'text','text':'publish closeout'}]}}), flush=True)\n"
                "  break\n",
            )
            result = PiAgent(_fake_config(fake, timeout=1)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="work",
                workdir=root,
                termination_summary_prompt="publish closeout",
                termination_summary_grace_seconds=0.25,
            )
            self.assertTrue(result.termination_summary_requested)
            self.assertFalse(result.termination_summary_completed)


class TerminationSummaryCpsTests(unittest.TestCase):
    def test_final_evidence_counts_one_closeout_once_on_write_failure(self) -> None:
        config = load_config("configs/termination_summary_mock.toml", ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            closeout = {
                "task_id": "task",
                "agent_id": "agent",
                "episode": 1,
                "closeout_id": "closeout-1",
            }
            rows = [
                {
                    "event": "termination_summary_requested",
                    **closeout,
                    "request_sent": False,
                },
                {
                    "event": "termination_summary_unavailable",
                    **closeout,
                    "reason": "closeout_command_write_failed",
                    "request_sent": False,
                },
                {
                    "event": "termination_summary_missing",
                    **closeout,
                    "request_sent": False,
                },
            ]
            (run_dir / "events.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            evidence = _termination_summary_final_evidence(run_dir, config)
            self.assertEqual(evidence["requests"], 1)
            self.assertEqual(evidence["request_sent"], 0)
            self.assertEqual(evidence["eligible_terminations"], 1)
            self.assertEqual(evidence["communication_unavailable"], 1)
            self.assertEqual(evidence["missing_publication"], 1)

    def test_actor_scoped_piece_audit_does_not_mix_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            store.create_piece(
                task_id="task",
                author="agent-a",
                kind="termination_summary",
                title="a",
                body="a",
            )
            store.create_piece(
                task_id="task",
                author="agent-b",
                kind="termination_summary",
                title="b",
                body="b",
            )
            a = store.pieces_by_actor(
                task_id="task", author="agent-a", kind="termination_summary"
            )
            self.assertEqual([row["author"] for row in a], ["agent-a"])

    def test_broker_closeout_window_allows_only_summary_search_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "worker"
            workdir.mkdir()
            candidate = workdir / "result.lean"
            candidate.write_text("proof", encoding="utf-8")
            task = load_tasks(load_config("configs/smoke.toml", ROOT))[0]
            source = threading.Event()
            closeout = _TerminationSummaryCancelEvent(source)
            store = CPSStore(root / "cps.sqlite3")
            broker = JudgeBroker(
                _BrokerEvaluator(),
                threading.BoundedSemaphore(1),
                audit_path=root / "audit.jsonl",
                min_probe_interval_seconds=0,
            ).start()
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={task.slug: (task, candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    cancel_event=closeout,
                    termination_summary_event=closeout,
                ) as env:
                    source.set()
                    closeout.begin_termination_summary()
                    denied = _post_capability(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_send",
                        {"body": "must not send during closeout"},
                    )
                    judge_denied = _post_capability(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "judge_check",
                        {},
                    )
                    searched = _post_capability(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_search",
                        {"query": "", "limit": 1},
                    )
                    published = _post_capability(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_publish",
                        {
                            "kind": "termination_summary",
                            "title": "termination_summary: closeout",
                            "body": "new_findings: none",
                            "tags": ["forced_closeout:test"],
                        },
                    )
                    closeout.finish_termination_summary()
                self.assertEqual(denied["status"], "CPS_CAPABILITY_DENIED")
                self.assertEqual(judge_denied["status"], "TERMINATION_CLOSEOUT_ACTIVE")
                self.assertTrue(searched["ok"])
                self.assertTrue(published["ok"])
            finally:
                broker.close()

    def test_runner_records_agent_publication_without_runner_piece(self) -> None:
        base = load_config("configs/smoke.toml", ROOT)
        config = replace(
            base,
            max_tasks=1,
            max_parallel=1,
            initial_agents_per_task=1,
            max_attempts_per_task=1,
            time_limit_seconds=2,
            # Keep this unit focused on closeout auditing; recovery policy is
            # exercised independently and must not be coupled to summary.
            pi_recovery_enabled=False,
            pi_recovery_max_restarts=1,
            termination_summary=TerminationSummaryConfig(
                enabled=True,
                grace_seconds=1.0,
                on_timeout=True,
                on_cancel=True,
                max_prompt_chars=4_000,
            ),
        )
        task = load_tasks(config)[0]
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            store = CPSStore(run_dir / "cps.sqlite3")
            policy = make_policy(config.communication, store)
            pi = _PublishingTimeoutPi(store)
            results = _run_elastic_cps(
                config,
                [task],
                run_dir,
                logger,
                _SkippedEvaluator(),
                pi,
                policy,
                mock_agent=False,
                deadline=time.monotonic() + 2,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=_Broker(),
                scheduler_result_sink=[],
            )
            self.assertEqual(len(pi.calls), 1)
            self.assertIsNotNone(pi.calls[0].get("termination_summary_prompt"))
            self.assertEqual(len(results), 1)
            pieces = store.pieces_by_actor(
                task_id=task.slug,
                author=str(pi.calls[0]["actor_id"]),
                kind="termination_summary",
            )
            self.assertEqual(len(pieces), 1)
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            self.assertTrue(
                any(row.get("event") == "termination_summary_published" for row in events)
            )
            self.assertFalse(any(row.get("event") == "checkpoint_handoff" for row in events))
            self.assertFalse((run_dir / "workers" / task.slug / "checkpoints").exists())

    def test_runner_audits_closeout_before_recovery_replacement(self) -> None:
        base = load_config("configs/smoke.toml", ROOT)
        config = replace(
            base,
            max_tasks=1,
            max_parallel=1,
            initial_agents_per_task=1,
            max_attempts_per_task=1,
            time_limit_seconds=2,
            pi_recovery_enabled=True,
            pi_recovery_max_restarts=1,
            pi_recovery_base_delay_ms=0,
            termination_summary=TerminationSummaryConfig(
                enabled=True,
                grace_seconds=1.0,
                on_timeout=True,
                on_cancel=True,
                on_error=True,
                max_prompt_chars=4_000,
            ),
        )
        task = load_tasks(config)[0]
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            store = CPSStore(run_dir / "cps.sqlite3")
            policy = make_policy(config.communication, store)
            pi = _TimeoutThenSuccessPi(store)
            results = _run_elastic_cps(
                config,
                [task],
                run_dir,
                logger,
                _SkippedEvaluator(),
                pi,
                policy,
                mock_agent=False,
                deadline=time.monotonic() + 2,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=_Broker(),
                scheduler_result_sink=[],
            )
            self.assertEqual(len(pi.calls), 2)
            self.assertEqual(len(results), 1)
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            published = [
                row
                for row in events
                if row.get("event") == "termination_summary_published"
            ]
            self.assertEqual(len(published), 1)
            self.assertEqual(published[0]["process_attempt"], 0)
            self.assertEqual(published[0]["publish_count"], 1)
            self.assertTrue(
                any(
                    row.get("event") == "agent_recovery_started"
                    and row.get("recovery_attempt") == 1
                    for row in events
                )
            )

    def test_runner_audits_real_broker_closeout_receipt(self) -> None:
        """The ending Agent, not the runner, must create the CPS receipt."""

        base = load_config("configs/smoke.toml", ROOT)
        config = replace(
            base,
            max_tasks=1,
            max_parallel=1,
            initial_agents_per_task=1,
            max_attempts_per_task=1,
            time_limit_seconds=2,
            pi_timeout_seconds=1,
            # Keep the receipt assertion to one process attempt; recovery is
            # intentionally tested as an independent policy below.
            pi_recovery_enabled=False,
            pi_binary="",
            termination_summary=TerminationSummaryConfig(
                enabled=True,
                grace_seconds=1.0,
                on_timeout=True,
                on_cancel=True,
                on_error=True,
                max_prompt_chars=4_000,
            ),
        )
        task = load_tasks(config)[0]
        fake_source = (
            "import json, os, re, sys\n"
            "from urllib.request import Request, urlopen\n"
            "request=json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'id':request['id'],'type':'response','success':True}), flush=True)\n"
            "print(json.dumps({'type':'agent_start'}), flush=True)\n"
            "while True:\n"
            " line=sys.stdin.readline()\n"
            " if not line: break\n"
            " command=json.loads(line)\n"
            " if command.get('type') != 'steer': continue\n"
            " tag=re.search(r'forced_closeout:[A-Za-z0-9_-]+', command.get('message','')).group(0)\n"
            " payload={'kind':'termination_summary','title':'termination_summary: fake','body':'new_findings: fake\\nnext_step: none','tags':[tag]}\n"
            " request2=Request(os.environ['CONTEXTSWARM_JUDGE_URL']+'/cps_publish', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST')\n"
            " urlopen(request2, timeout=1).read()\n"
            " print(json.dumps({'id':command.get('id'),'type':'response','success':True}), flush=True)\n"
            " print(json.dumps({'type':'turn_start'}), flush=True)\n"
            " print(json.dumps({'type':'message_start','message':{'role':'user','content':[{'type':'text','text':command.get('message','')}]}}), flush=True)\n"
            " print(json.dumps({'type':'agent_settled'}), flush=True)\n"
            " break\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake(root, fake_source)
            config = replace(config, pi_binary=str(fake))
            run_dir = root / "run"
            logger = RunLogger(run_dir)
            store = CPSStore(run_dir / "cps.sqlite3")
            policy = make_policy(config.communication, store)
            broker = JudgeBroker(
                _SkippedEvaluator(),
                threading.BoundedSemaphore(1),
                audit_path=run_dir / "judge_checks.jsonl",
                min_probe_interval_seconds=0,
            ).start()
            try:
                results = _run_elastic_cps(
                    config,
                    [task],
                    run_dir,
                    logger,
                    _SkippedEvaluator(),
                    PiAgent(config),
                    policy,
                    mock_agent=False,
                    deadline=time.monotonic() + 2,
                    evaluator_gate=threading.BoundedSemaphore(1),
                    judge_broker=broker,
                    scheduler_result_sink=[],
                )
            finally:
                broker.close()
            self.assertEqual(len(results), 1)
            pieces = store.pieces_by_actor(
                task_id=task.slug,
                author=str(results[0][0].agent_id),
                kind="termination_summary",
            )
            self.assertEqual(len(pieces), 1)
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            published = [
                row for row in events if row.get("event") == "termination_summary_published"
            ]
            self.assertEqual(len(published), 1)
            self.assertEqual(published[0]["publish_count"], 1)

    def test_summary_state_does_not_change_recovery_classifier(self) -> None:
        calls: list[int] = []
        now = "2026-01-01T00:00:00+00:00"

        def invoke(attempt: int) -> AgentResult:
            calls.append(attempt)
            return AgentResult(
                agent_id="agent",
                task_id="task",
                episode=1,
                returncode=124,
                started_at=now,
                finished_at=now,
                timed_out=True,
                termination_summary_requested=True,
            )

        result = run_with_recovery(
            invoke,
            task_id="task",
            actor_id="agent",
            episode=1,
            deadline_monotonic=time.monotonic() + 10,
            max_restarts=1,
            base_delay_seconds=0,
        )
        # Recovery policy is deliberately independent from semantic closeout;
        # the future recover workstream may add an explicit handoff rule.
        self.assertEqual(calls, [0, 1])
        self.assertTrue(result.termination_summary_requested)


if __name__ == "__main__":
    unittest.main()
