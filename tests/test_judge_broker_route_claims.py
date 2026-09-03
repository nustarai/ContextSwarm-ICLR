from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from contextswarm_mini.judge_broker import JudgeBroker
from contextswarm_mini.models import Task, Verdict


def _post(url: str, operation: str, payload: dict[str, object]) -> dict[str, object]:
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


def _task(root: Path) -> Task:
    return Task(
        slug="task",
        root=root,
        problem_text="problem",
        baseline_code="import Mathlib\ntheorem task : True := by sorry\n",
        metadata={"problem_id": "Task", "theorem_name": "task"},
    )


class _TerminalEvaluator:
    def expected_task_contract_sha256(self, _task: Task) -> str:
        return "a" * 64

    def probe(
        self,
        task: Task,
        _candidate: Path,
        *,
        deadline_monotonic: float | None,
    ) -> Verdict:
        del deadline_monotonic
        return Verdict(
            task.slug,
            "VERIFY_FAIL",
            0.0,
            0.0,
            task_contract_sha256="a" * 64,
            judge_job_id="route-claim-test-job",
        )


class _RouteStore:
    """Small CPS double with the staged route-claim API."""

    def __init__(self) -> None:
        self.actors: set[tuple[str, str]] = set()
        self.routes: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def record_event(self, **kwargs):
        self.events.append(dict(kwargs))
        return "event"

    def register_actor(self, *, task_id, actor_id, episode, **_kwargs):
        self.actors.add((task_id, actor_id))
        return {"task_id": task_id, "actor_id": actor_id, "episode": episode}

    def list_active_actors(self, *, task_id=None, include_closing=True, limit=100):
        del include_closing
        rows = [
            {"task_id": task, "actor_id": actor, "episode": 1, "status": "admitted"}
            for task, actor in sorted(self.actors)
            if task_id is None or task == task_id
        ]
        return rows[:limit]

    def list_active_routes(self, *, task_id=None, actor_id=None, limit=100):
        rows = [
            dict(row)
            for row in self.routes
            if row["status"] in {"active", "blocked"}
            and (task_id is None or row["task_id"] == task_id)
            and (actor_id is None or row["actor_id"] == actor_id)
        ]
        return rows[:limit]

    def claim_route(
        self,
        *,
        task_id,
        actor_id,
        episode,
        route_key,
        summary,
        ttl_seconds,
        independent_verification_reason=None,
        **_kwargs,
    ):
        with self._lock:
            own = next(
                (
                    row
                    for row in self.routes
                    if row["task_id"] == task_id
                    and row["route_key"] == route_key
                    and row["actor_id"] == actor_id
                    and row["status"] in {"active", "blocked"}
                ),
                None,
            )
            if own is not None:
                return {"ok": True, "acquired": True, "claim": dict(own), "idempotent": True}
            primary = next(
                (
                    row
                    for row in self.routes
                    if row["task_id"] == task_id
                    and row["route_key"] == route_key
                    and row["status"] in {"active", "blocked"}
                    and row.get("is_primary") is True
                ),
                None,
            )
            if primary is not None and not independent_verification_reason:
                return {"ok": False, "acquired": False, "status": "conflict", "conflict": dict(primary)}
            row = {
                "claim_id": f"claim-{len(self.routes) + 1}",
                "task_id": task_id,
                "actor_id": actor_id,
                "episode": episode,
                "route_key": route_key,
                "summary": summary,
                "status": "active",
                "ttl_seconds": ttl_seconds,
                "independent_verification_reason": independent_verification_reason,
                "is_primary": primary is None,
            }
            self.routes.append(row)
            return {
                "ok": True,
                "acquired": True,
                "claimed": True,
                "claim": dict(row),
                "conflict": dict(primary) if primary is not None else None,
            }

    def update_route_claim(self, *, claim_id, actor_id, status=None, summary=None, **_kwargs):
        for row in self.routes:
            if row["claim_id"] == claim_id and row["actor_id"] == actor_id:
                if status:
                    row["status"] = status
                if summary:
                    row["summary"] = summary
                return {"ok": True, "claim": dict(row)}
        return {"ok": False, "status": "not_found"}

    def release_route_claim(self, *, claim_id, actor_id, status="released", **_kwargs):
        return self.update_route_claim(claim_id=claim_id, actor_id=actor_id, status=status)


class _BrokenRouteStore(_RouteStore):
    def list_active_routes(self, **_kwargs):
        raise RuntimeError("store unavailable")


class _MalformedRoutesStore(_RouteStore):
    def list_active_routes(self, **_kwargs):
        return None


class _MalformedActorsStore(_RouteStore):
    def list_active_actors(self, **_kwargs):
        return [None]


class _SparseActorsStore(_RouteStore):
    def list_active_actors(self, **_kwargs):
        return [{"task_id": "task", "actor_id": "agent"}]


class _CrossTaskActorsStore(_RouteStore):
    def list_active_actors(self, **_kwargs):
        return [
            {
                "task_id": "other-task",
                "actor_id": "peer",
                "episode": 1,
                "status": "admitted",
            },
            {
                "task_id": "task",
                "actor_id": "agent",
                "episode": 1,
                "status": "admitted",
            },
        ]


class _TerminalActorsStore(_RouteStore):
    def list_active_actors(self, **_kwargs):
        return [
            {
                "task_id": "task",
                "actor_id": "agent",
                "status": "FINISHED",
                "episode": 1,
            }
        ]


class _MalformedClaimStore(_RouteStore):
    def claim_route(self, **_kwargs):
        return None


class _EchoOnlyClaimStore(_RouteStore):
    def claim_route(self, **kwargs):
        return {
            "ok": False,
            "status": "released",
            "independent_verification_reason": kwargs.get(
                "independent_verification_reason"
            ),
            "claim": {
                "claim_id": "terminal-claim",
                "status": "released",
                "independent_verification_reason": kwargs.get(
                    "independent_verification_reason"
                ),
            },
        }


class _BareSuccessClaimStore(_RouteStore):
    def claim_route(self, **_kwargs):
        return {"ok": True}


class _ConflictWithoutAcceptanceStore(_RouteStore):
    def claim_route(self, **kwargs):
        own = {
            "claim_id": "secondary-without-acceptance",
            "task_id": kwargs["task_id"],
            "actor_id": kwargs["actor_id"],
            "episode": kwargs["episode"],
            "route_key": kwargs["route_key"],
            "summary": kwargs["summary"],
            "status": "active",
            "active": True,
            "is_primary": False,
        }
        peer = {
            "claim_id": "primary-peer",
            "task_id": kwargs["task_id"],
            "actor_id": "peer",
            "episode": kwargs["episode"],
            "route_key": kwargs["route_key"],
            "summary": "peer route",
            "status": "active",
            "active": True,
            "is_primary": True,
        }
        # Deliberately echo an acquired row and a conflict without any
        # independent-verification acceptance evidence.
        return {
            "ok": True,
            "acquired": True,
            "claimed": True,
            "claim": own,
            "conflict": peer,
        }


class _BlockedClaimStore(_RouteStore):
    def claim_route(self, **kwargs):
        return {
            "ok": True,
            "acquired": True,
            "claimed": True,
            "claim": {
                "claim_id": "blocked-claim",
                "task_id": kwargs["task_id"],
                "actor_id": kwargs["actor_id"],
                "episode": kwargs["episode"],
                "route_key": kwargs["route_key"],
                "summary": kwargs["summary"],
                "status": "blocked",
                "active": True,
            },
        }


class _BlockedUpdateStore(_RouteStore):
    def update_route_claim(self, *, claim_id, actor_id, **_kwargs):
        for row in self.routes:
            if row["claim_id"] == claim_id and row["actor_id"] == actor_id:
                row["status"] = "blocked"
                # Deliberately echo the stale active bit. The broker must
                # still retire its local write-gate lease for blocked state.
                row["active"] = True
                return {
                    "ok": True,
                    "acquired": True,
                    "claimed": True,
                    "claim": dict(row),
                }
        return {"ok": False, "status": "not_found"}


class _ExplicitBypassWithoutReasonStore(_RouteStore):
    def claim_route(self, **_kwargs):
        return {"ok": True, "bypassed": True, "status": "route_claim_bypass"}


class _TerminalRouteProjectionStore(_RouteStore):
    def list_active_routes(self, **kwargs):
        task_id = kwargs.get("task_id", "task")
        return [
            {
                "claim_id": "live",
                "task_id": task_id,
                "actor_id": "peer",
                "episode": 1,
                "route_key": "live-route",
                "status": "active",
                "active": True,
            },
            {
                "claim_id": "stale",
                "task_id": task_id,
                "actor_id": "peer",
                "episode": 1,
                "route_key": "stale-route",
                "status": " FINISHED ",
                "active": True,
            },
        ]


class _TerminalActorProjectionStore(_RouteStore):
    def list_active_actors(self, **kwargs):
        task_id = kwargs.get("task_id", "task")
        return [
            {
                "task_id": task_id,
                "actor_id": "agent",
                "episode": 1,
                "status": "admitted",
                "active": True,
            },
            {
                "task_id": task_id,
                "actor_id": "finished-peer",
                "episode": 1,
                "status": " FINISHED ",
                "active": True,
            },
        ]


class _NotOwnerEchoStore(_RouteStore):
    """Echo a peer row for a handled ownership rejection."""

    def update_route_claim(self, *, claim_id, actor_id, **_kwargs):
        row = next((item for item in self.routes if item["claim_id"] == claim_id), None)
        if row is None:
            return {"ok": False, "status": "not_found"}
        if row["actor_id"] != actor_id:
            return {
                "ok": False,
                "error": "not_owner",
                "claim": dict(row),
            }
        return {"ok": True, "claim": dict(row)}

    def release_route_claim(self, *, claim_id, actor_id, **kwargs):
        return self.update_route_claim(
            claim_id=claim_id,
            actor_id=actor_id,
            **kwargs,
        )


class _ForeignNotOwnerEchoStore(_RouteStore):
    """Return a handled negative while smuggling a foreign claim row."""

    def claim_route(self, **kwargs):
        return {
            "ok": False,
            "status": "not_owner",
            "claim": {
                "claim_id": "foreign-claim",
                "task_id": "other-task",
                "actor_id": "other-actor",
                "episode": kwargs["episode"],
                "route_key": kwargs["route_key"],
                "status": "active",
                "active": True,
                "is_primary": True,
            },
        }


class _BareConflictStore(_RouteStore):
    def claim_route(self, **_kwargs):
        return {
            "conflict": {
                "claim_id": "bare-conflict",
                "task_id": "task",
                "actor_id": "peer",
                "episode": 1,
                "route_key": "bare",
                "status": "active",
                "active": True,
                "is_primary": True,
            }
        }


class _TypedEnvelopeClaimStore(_RouteStore):
    def __init__(self, field: str, value: object):
        super().__init__()
        self.field = field
        self.value = value

    def claim_route(self, **kwargs):
        return {
            "ok": True,
            "acquired": True,
            "status": "active",
            self.field: self.value,
            "claim": {
                "claim_id": "typed-envelope",
                "task_id": kwargs["task_id"],
                "actor_id": kwargs["actor_id"],
                "episode": kwargs["episode"],
                "route_key": kwargs["route_key"],
                "status": "active",
                "active": True,
                "is_primary": True,
            },
        }


class _ContradictoryNotOwnerStore(_RouteStore):
    def update_route_claim(self, *, claim_id, actor_id, **_kwargs):
        row = next((item for item in self.routes if item["claim_id"] == claim_id), None)
        if row is None:
            return {"ok": False, "status": "not_found"}
        return {
            "ok": True,
            "accepted": True,
            "acquired": True,
            "status": "not_owner",
            "error": "not_owner",
            "claim": dict(row),
        }


class _UnknownNegativeClaimStore(_RouteStore):
    def claim_route(self, **_kwargs):
        return {"ok": False, "status": "mystery"}


class _ErrorOnlyClaimStore(_RouteStore):
    def __init__(self, field: str, value: str):
        super().__init__()
        self.field = field
        self.value = value

    def claim_route(self, **_kwargs):
        return {"ok": False, self.field: self.value}


class _CrossTaskRouteStore(_RouteStore):
    def list_active_routes(self, **_kwargs):
        return [
            {
                "claim_id": "cross-task",
                "task_id": "other-task",
                "actor_id": "peer",
                "episode": 1,
                "route_key": "leak",
                "status": "active",
            }
        ]


class _CrossTaskClaimStore(_RouteStore):
    def claim_route(self, **_kwargs):
        return {
            "ok": True,
            "acquired": True,
            "claim": {
                "claim_id": "cross-task-claim",
                "task_id": "other-task",
                "actor_id": "peer",
                "episode": 1,
                "route_key": "wrong",
                "status": "active",
            },
        }


class _StrayBypassMarkerStore(_RouteStore):
    def claim_route(self, **kwargs):
        return {
            "ok": True,
            "acquired": True,
            "route_claim_bypass_reason": "unavailable",
            "claim": {
                "claim_id": "stray-marker",
                "task_id": kwargs["task_id"],
                "actor_id": kwargs["actor_id"],
                "episode": kwargs["episode"],
                "route_key": kwargs["route_key"],
                "status": "active",
            },
        }


class _UnknownPositiveClaimStore(_RouteStore):
    def claim_route(self, **kwargs):
        return {
            "ok": True,
            "acquired": True,
            "claimed": True,
            "status": "active",
            "error": "mystery-adapter-diagnostic",
            "claim": {
                "claim_id": "unknown-positive",
                "task_id": kwargs["task_id"],
                "actor_id": kwargs["actor_id"],
                "episode": kwargs["episode"],
                "route_key": kwargs["route_key"],
                "status": "active",
                "active": True,
                "is_primary": True,
            },
        }


class _StringActiveClaimStore(_RouteStore):
    def claim_route(self, **kwargs):
        return {
            "ok": True,
            "acquired": True,
            "claimed": True,
            "status": "active",
            "claim": {
                "claim_id": "string-active",
                "task_id": kwargs["task_id"],
                "actor_id": kwargs["actor_id"],
                "episode": kwargs["episode"],
                "route_key": kwargs["route_key"],
                "status": "active",
                "active": "false",
                "is_primary": True,
            },
        }


class _PrimaryContradictionStore(_RouteStore):
    def claim_route(self, **kwargs):
        return {
            "ok": True,
            "acquired": True,
            "claimed": True,
            "status": "active",
            "claim": {
                "claim_id": "contradictory-primary",
                "task_id": kwargs["task_id"],
                "actor_id": kwargs["actor_id"],
                "episode": kwargs["episode"],
                "route_key": kwargs["route_key"],
                "status": "active",
                "active": True,
                "is_primary": True,
                "independent_verification_reason": kwargs.get(
                    "independent_verification_reason"
                ),
            },
            "conflict": {
                "claim_id": "peer-primary",
                "task_id": kwargs["task_id"],
                "actor_id": "peer",
                "episode": kwargs["episode"],
                "route_key": kwargs["route_key"],
                "status": "active",
                "active": True,
                "is_primary": True,
            },
        }


class _ContradictoryClaimEnvelopeStore(_RouteStore):
    def __init__(self, **fields: object):
        super().__init__()
        self.fields = fields

    def claim_route(self, **kwargs):
        return {
            "ok": True,
            "status": "active",
            "claim": {
                "claim_id": "contradictory-envelope",
                "task_id": kwargs["task_id"],
                "actor_id": kwargs["actor_id"],
                "episode": kwargs["episode"],
                "route_key": kwargs["route_key"],
                "status": "active",
                "active": True,
                "is_primary": True,
            },
            **self.fields,
        }


class _RouteKeyMismatchStore(_RouteStore):
    def claim_route(self, **kwargs):
        return {
            "ok": True,
            "acquired": True,
            "claimed": True,
            "status": "active",
            "claim": {
                "claim_id": "wrong-route",
                "task_id": kwargs["task_id"],
                "actor_id": kwargs["actor_id"],
                "episode": kwargs["episode"],
                "route_key": "different-route",
                "status": "active",
                "active": True,
                "is_primary": True,
            },
        }


class _ClaimIdMismatchStore(_RouteStore):
    def update_route_claim(self, *, claim_id, actor_id, **kwargs):
        del claim_id, actor_id, kwargs
        return {
            "ok": True,
            "acquired": True,
            "status": "active",
            "claim": {
                "claim_id": "different-claim",
                "task_id": "task",
                "actor_id": "agent",
                "episode": 1,
                "route_key": "bound",
                "status": "active",
                "active": True,
                "is_primary": True,
            },
        }

    def release_route_claim(self, *, claim_id, actor_id, **kwargs):
        return self.update_route_claim(
            claim_id=claim_id,
            actor_id=actor_id,
            **kwargs,
        )


class _StaleLeaseNegativeStore(_RouteStore):
    def update_route_claim(self, *, claim_id, actor_id, **_kwargs):
        del actor_id
        return {
            "ok": False,
            "found": False,
            "status": "not_found",
            "claim_id": claim_id,
        }

    def release_route_claim(self, *, claim_id, actor_id, **_kwargs):
        return self.update_route_claim(claim_id=claim_id, actor_id=actor_id)


class _TerminalActiveActorsStore(_RouteStore):
    def list_active_actors(self, **_kwargs):
        return [
            {
                "task_id": "task",
                "actor_id": "agent",
                "episode": 1,
                "status": "FINISHED",
                "active": True,
            }
        ]


class _UnknownActorStatusStore(_RouteStore):
    def __init__(self, status: str = "garbage"):
        super().__init__()
        self.status = status

    def list_active_actors(self, **kwargs):
        task_id = kwargs.get("task_id", "task")
        return [
            {
                "task_id": task_id,
                "actor_id": "agent",
                "episode": 1,
                # An explicit active bit cannot bless an unknown lifecycle
                # label. The broker must fail open rather than minting a route.
                "status": self.status,
                "active": True,
            }
        ]


class _SlowActorStatusStore(_RouteStore):
    def list_active_actors(self, **kwargs):
        time.sleep(0.20)
        return super().list_active_actors(**kwargs)


class _EpisodeRouteStore(_RouteStore):
    def __init__(self, episode: int):
        super().__init__()
        self.episode = episode

    def list_active_actors(self, *, task_id=None, include_closing=True, limit=100):
        del include_closing
        rows = [
            {
                "task_id": task,
                "actor_id": actor,
                "episode": self.episode,
                "status": "admitted",
            }
            for task, actor in sorted(self.actors)
            if task_id is None or task == task_id
        ]
        return rows[:limit]


class JudgeBrokerRouteClaimTests(unittest.TestCase):
    def _fixture(self, root: Path, store: _RouteStore | None = None):
        workdir = root / "worker"
        workdir.mkdir()
        candidate = workdir / "result.lean"
        candidate.write_text("proof\n", encoding="utf-8")
        store = store or _RouteStore()
        store.register_actor(task_id="task", actor_id="agent", episode=1)
        broker = JudgeBroker(
            _TerminalEvaluator(),
            threading.BoundedSemaphore(1),
            audit_path=root / "audit.jsonl",
            min_probe_interval_seconds=0,
            route_claims_enabled=True,
            route_claim_required=True,
        ).start()
        return broker, store, workdir, candidate

    def test_route_enabled_session_requires_real_cps_surface(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "worker"
            workdir.mkdir()
            candidate = workdir / "result.lean"
            candidate.write_text("proof\n", encoding="utf-8")
            broker = JudgeBroker(
                _TerminalEvaluator(),
                threading.BoundedSemaphore(1),
                audit_path=root / "audit.jsonl",
                min_probe_interval_seconds=0,
                route_claims_enabled=True,
                route_claim_required=True,
            ).start()
            try:
                with self.assertRaisesRegex(ValueError, "route-enabled"):
                    with broker.session(
                        actor_id="agent",
                        episode=1,
                        workdir=workdir,
                        candidates={"task": (_task(root), candidate)},
                        deadline_monotonic=time.monotonic() + 3,
                        cps_store=None,
                        communication="none",
                        route_claims_enabled=True,
                    ):
                        self.fail("route treatment accepted without a CPS surface")
            finally:
                broker.close()

    def test_only_active_and_claim_are_allowed_before_first_judge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root)
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                    route_claim_required=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    self.assertTrue(_post(url, "cps_active_routes", {})["ok"])
                    claimed = _post(
                        url,
                        "cps_claim_route",
                        {"route_key": "induction", "summary": "try induction"},
                    )
                    self.assertTrue(claimed["acquired"])
                    visible = _post(url, "cps_active_routes", {})
                    self.assertTrue(visible["accepted"])
                    self.assertTrue(visible["routes"][0]["is_primary"])
                    self.assertTrue(visible["routes"][0]["primary"])
                    self.assertTrue(visible["routes"][0]["active"])
                    blocked_search = _post(url, "cps_search", {"query": "early"})
                    self.assertEqual(blocked_search["status"], "JUDGE_CHECK_REQUIRED")
                    blocked_update = _post(
                        url,
                        "cps_update_route",
                        {"claim_id": claimed["claim"]["claim_id"], "status": "blocked"},
                    )
                    self.assertEqual(blocked_update["status"], "JUDGE_CHECK_REQUIRED")
            finally:
                broker.close()

    def test_malformed_route_requests_are_invalid_not_fail_open(self):
        """Forged loopback payloads must not turn input errors into bypasses."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root)
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                    route_claim_required=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]

                    bad_active = _post(url, "cps_active_routes", {"limit": "x"})
                    self.assertEqual(bad_active["status"], "INVALID_REQUEST")
                    self.assertFalse(bad_active.get("bypassed", False))

                    missing_route = _post(
                        url,
                        "cps_claim_route",
                        {"summary": "missing route key"},
                    )
                    self.assertEqual(missing_route["status"], "INVALID_REQUEST")
                    self.assertFalse(missing_route.get("bypassed", False))

                    missing_summary = _post(
                        url,
                        "cps_claim_route",
                        {"route_key": "missing-summary"},
                    )
                    self.assertEqual(missing_summary["status"], "INVALID_REQUEST")
                    self.assertFalse(missing_summary.get("bypassed", False))

                    fractional_ttl = _post(
                        url,
                        "cps_claim_route",
                        {
                            "route_key": "strict-input",
                            "summary": "fractional ttl",
                            "ttl_seconds": 1.5,
                        },
                    )
                    self.assertEqual(fractional_ttl["status"], "INVALID_REQUEST")
                    self.assertFalse(fractional_ttl.get("bypassed", False))

                    claimed = _post(
                        url,
                        "cps_claim_route",
                        {"route_key": "strict-input", "summary": "valid"},
                    )
                    self.assertTrue(claimed["acquired"])
                    claim_id = claimed["claim"]["claim_id"]
                    self.assertTrue(_post(url, "judge_check", {})["accepted"])

                    bad_update = _post(
                        url,
                        "cps_update_route",
                        {"claim_id": claim_id, "status": "BOGUS"},
                    )
                    self.assertEqual(bad_update["status"], "INVALID_REQUEST")
                    self.assertFalse(bad_update.get("bypassed", False))

                    bad_release = _post(
                        url,
                        "cps_release_route",
                        {"claim_id": claim_id, "status": "BOGUS"},
                    )
                    self.assertEqual(bad_release["status"], "INVALID_REQUEST")
                    self.assertFalse(bad_release.get("bypassed", False))

                    # Input negatives do not poison the route capability: a
                    # valid terminal closeout remains possible and is not
                    # converted into an outage bypass.
                    released = _post(
                        url,
                        "cps_release_route",
                        {"claim_id": claim_id, "status": "released"},
                    )
                    self.assertEqual(released["status"], "released")
                    self.assertFalse(released.get("bypassed", False))
            finally:
                broker.close()

    def test_conflict_and_independent_verification_are_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root)
            store.register_actor(task_id="task", actor_id="peer", episode=1)
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    first = _post(url, "cps_claim_route", {"route_key": "same", "summary": "primary"})
                    self.assertTrue(first["acquired"])
                    # Simulate the peer owning the same route, then verify that
                    # the broker preserves the CPS conflict/independence result.
                    store.routes[0]["actor_id"] = "peer"
                    conflict = _post(url, "cps_claim_route", {"route_key": "same", "summary": "duplicate"})
                    self.assertEqual(conflict["status"], "conflict")
                    independent = _post(
                        url,
                        "cps_claim_route",
                        {
                            "route_key": "same",
                            "summary": "independent check",
                            "independent_verification_reason": "rederive boundary case",
                        },
                    )
                    self.assertTrue(independent["acquired"])
                    self.assertTrue(independent["independent_verification_accepted"])
                    self.assertEqual(
                        independent["independent_verification_reason"],
                        "rederive boundary case",
                    )
            finally:
                broker.close()

    def test_conflict_reason_without_explicit_acceptance_cannot_unlock_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _ConflictWithoutAcceptanceStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {
                            "route_key": "same",
                            "summary": "duplicate",
                            "independent_verification_reason": "recheck",
                        },
                    )
                    token = env["CONTEXTSWARM_JUDGE_URL"].rstrip("/").rsplit("/", 1)[-1]
                    session_claim = broker._claims[token]
                    self.assertFalse(session_claim.route_claim_satisfied)
                self.assertFalse(result["acquired"])
                self.assertFalse(result.get("independent_verification_accepted", False))
            finally:
                broker.close()

    def test_blocked_claim_is_visible_but_cannot_unlock_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _BlockedClaimStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "blocked", "summary": "waiting"},
                    )
                    token = env["CONTEXTSWARM_JUDGE_URL"].rstrip("/").rsplit("/", 1)[-1]
                    self.assertFalse(broker._claims[token].route_claim_satisfied)
                self.assertFalse(result.get("bypassed", False))
                self.assertFalse(result.get("acquired", False))
            finally:
                broker.close()

    def test_blocked_update_clears_broker_write_lease_even_if_active_echoed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _BlockedUpdateStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    claimed = _post(
                        url,
                        "cps_claim_route",
                        {"route_key": "blocked-update", "summary": "initial"},
                    )
                    claim_id = claimed["claim"]["claim_id"]
                    self.assertTrue(claimed["acquired"])
                    self.assertTrue(_post(url, "judge_check", {})["accepted"])
                    updated = _post(
                        url,
                        "cps_update_route",
                        {"claim_id": claim_id, "status": "blocked"},
                    )
                    self.assertTrue(updated["ok"])
                    self.assertTrue(updated["accepted"])
                    self.assertFalse(updated["acquired"])
                    self.assertFalse(updated["claimed"])
                    token = url.rstrip("/").rsplit("/", 1)[-1]
                    self.assertFalse(broker._claims[token].route_claim_satisfied)
                    self.assertNotIn(claim_id, broker._claims[token].route_claim_ids)
            finally:
                broker.close()

    def test_explicit_bypass_reason_is_normalized_and_positive_flags_cleared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _ExplicitBypassWithoutReasonStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "bypass", "summary": "outage"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
                for key in ("ok", "accepted", "acquired", "claimed", "independent_verification_accepted"):
                    self.assertFalse(result.get(key, False))
            finally:
                broker.close()

    def test_active_route_projection_hides_terminal_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _TerminalRouteProjectionStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(env["CONTEXTSWARM_JUDGE_URL"], "cps_active_routes", {})
                self.assertEqual([row["claim_id"] for row in result["routes"]], ["live"])
            finally:
                broker.close()

    def test_actor_projection_hides_terminal_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _TerminalActorProjectionStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    self.assertTrue(_post(env["CONTEXTSWARM_JUDGE_URL"], "judge_check", {})["accepted"])
                    result = _post(env["CONTEXTSWARM_JUDGE_URL"], "cps_actors", {})
                self.assertEqual([row["actor_id"] for row in result["actors"]], ["agent"])
            finally:
                broker.close()

    def test_store_outage_is_explicit_fail_open_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, _store, workdir, candidate = self._fixture(root, _BrokenRouteStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=_store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    result = _post(url, "cps_active_routes", {})
                    claim_result = _post(
                        url,
                        "cps_claim_route",
                        {"route_key": "unavailable", "summary": "bypass"},
                    )
                self.assertTrue(result["ok"])
                self.assertFalse(result["accepted"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
                self.assertFalse(claim_result["acquired"])
                self.assertEqual(
                    claim_result["route_claim_bypass_reason"], "unavailable"
                )
            finally:
                broker.close()

    def test_bypass_latch_does_not_widen_pre_judge_route_surface(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _BrokenRouteStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    first = _post(url, "cps_active_routes", {})
                    self.assertTrue(first["bypassed"])
                    update = _post(
                        url,
                        "cps_update_route",
                        {"claim_id": "not-yet", "status": "blocked"},
                    )
                    release = _post(
                        url,
                        "cps_release_route",
                        {"claim_id": "not-yet"},
                    )
                self.assertEqual(update["status"], "JUDGE_CHECK_REQUIRED")
                self.assertEqual(release["status"], "JUDGE_CHECK_REQUIRED")
            finally:
                broker.close()

    def test_unregistered_actor_cannot_claim_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root)
            try:
                with broker.session(
                    actor_id="phantom",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "phantom", "summary": "no admission"},
                    )
                self.assertEqual(result["status"], "ACTOR_NOT_ADMITTED")
            finally:
                broker.close()

    def test_route_reads_require_admission_and_matching_episode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _RouteStore()
            broker, _unused, workdir, candidate = self._fixture(root, store)
            try:
                with broker.session(
                    actor_id="phantom",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(env["CONTEXTSWARM_JUDGE_URL"], "cps_active_routes", {})
                self.assertEqual(result["status"], "ACTOR_NOT_ADMITTED")
            finally:
                broker.close()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _EpisodeRouteStore(episode=2)
            broker, _unused, workdir, candidate = self._fixture(root, store)
            # The fixture's normal admission is episode 1; replace it with a
            # stale episode-2 row for the same actor.
            store.actors.clear()
            store.register_actor(task_id="task", actor_id="agent", episode=2)
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(env["CONTEXTSWARM_JUDGE_URL"], "cps_active_routes", {})
                self.assertEqual(result["status"], "ACTOR_EPISODE_MISMATCH")
            finally:
                broker.close()

    def test_malformed_route_rows_are_explicit_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _MalformedRoutesStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(env["CONTEXTSWARM_JUDGE_URL"], "cps_active_routes", {})
                self.assertTrue(result["bypassed"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
                self.assertFalse(result["accepted"])
            finally:
                broker.close()

    def test_malformed_actor_rows_are_explicit_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _MalformedActorsStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(env["CONTEXTSWARM_JUDGE_URL"], "cps_active_routes", {})
                self.assertTrue(result["bypassed"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
            finally:
                broker.close()

    def test_sparse_actor_row_cannot_mint_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _SparseActorsStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_active_routes",
                        {},
                    )
                self.assertTrue(result["bypassed"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
            finally:
                broker.close()

    def test_cross_task_actor_projection_is_explicit_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _CrossTaskActorsStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_active_routes",
                        {},
                    )
                self.assertTrue(result["bypassed"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
            finally:
                broker.close()

    def test_terminal_actor_row_cannot_claim_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _TerminalActorsStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "terminal", "summary": "no"},
                    )
                self.assertEqual(result["status"], "ACTOR_NOT_ADMITTED")
            finally:
                broker.close()

    def test_terminal_actor_flag_cannot_override_terminal_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _TerminalActiveActorsStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "terminal", "summary": "no"},
                    )
                self.assertEqual(result["status"], "ACTOR_NOT_ADMITTED")
            finally:
                broker.close()

    def test_unknown_live_actor_status_cannot_mint_admission(self):
        for status in ("garbage", "queued", "pending"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    broker, store, workdir, candidate = self._fixture(
                        root, _UnknownActorStatusStore(status)
                    )
                    try:
                        with broker.session(
                            actor_id="agent",
                            episode=1,
                            workdir=workdir,
                            candidates={"task": (_task(root), candidate)},
                            deadline_monotonic=time.monotonic() + 3,
                            cps_store=store,
                            communication="blackboard",
                            route_claims_enabled=True,
                        ) as env:
                            result = _post(
                                env["CONTEXTSWARM_JUDGE_URL"],
                                "cps_claim_route",
                                {"route_key": "unknown-status", "summary": "reject"},
                            )
                        self.assertTrue(result["bypassed"])
                        self.assertFalse(result["acquired"])
                        self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
                    finally:
                        broker.close()

    def test_route_call_rechecks_deadline_after_slow_roster_adapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _SlowActorStatusStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 0.10,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(env["CONTEXTSWARM_JUDGE_URL"], "cps_active_routes", {})
                self.assertEqual(result["status"], "OUT_OF_HORIZON")
            finally:
                broker.close()

    def test_cross_task_route_projection_is_explicit_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _CrossTaskRouteStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_active_routes",
                        {},
                    )
                self.assertTrue(result["bypassed"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
            finally:
                broker.close()

    def test_cross_task_positive_claim_cannot_unlock_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _CrossTaskClaimStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "wrong", "summary": "wrong"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertFalse(result.get("acquired", False))
            finally:
                broker.close()

    def test_stray_adapter_bypass_marker_is_not_successful_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _StrayBypassMarkerStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "stray", "summary": "marker"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertFalse(result.get("acquired", False))
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
            finally:
                broker.close()

    def test_not_owner_echo_is_handled_without_fail_open_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _NotOwnerEchoStore()
            )
            store.register_actor(task_id="task", actor_id="peer", episode=1)
            peer_claim = store.claim_route(
                task_id="task",
                actor_id="peer",
                episode=1,
                route_key="peer-route",
                summary="peer work",
                ttl_seconds=60,
                now=100,
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    # Move past the pre-Judge gate; update/release are
                    # intentionally post-checkpoint operations.
                    self.assertTrue(_post(url, "judge_check", {})["accepted"])
                    result = _post(
                        url,
                        "cps_update_route",
                        {"claim_id": peer_claim["claim"]["claim_id"], "status": "blocked"},
                    )
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "not_owner")
                self.assertFalse(result.get("bypassed", False))
                self.assertNotEqual(result.get("route_claim_bypass_reason"), "unavailable")
            finally:
                broker.close()

    def test_contradictory_not_owner_envelope_cannot_unlock_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _ContradictoryNotOwnerStore()
            )
            store.register_actor(task_id="task", actor_id="peer", episode=1)
            peer_claim = store.claim_route(
                task_id="task",
                actor_id="peer",
                episode=1,
                route_key="peer-route",
                summary="peer work",
                ttl_seconds=60,
                now=100,
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    self.assertTrue(_post(url, "judge_check", {})["accepted"])
                    result = _post(
                        url,
                        "cps_update_route",
                        {"claim_id": peer_claim["claim"]["claim_id"], "status": "blocked"},
                    )
                self.assertFalse(result["ok"])
                self.assertFalse(result.get("accepted", False))
                self.assertFalse(result.get("acquired", False))
                self.assertFalse(result.get("bypassed", False))
            finally:
                broker.close()

    def test_unknown_negative_route_response_takes_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _UnknownNegativeClaimStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "mystery", "summary": "unknown"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
            finally:
                broker.close()

    def test_foreign_claim_in_semantic_negative_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _ForeignNotOwnerEchoStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "foreign", "summary": "bad"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertNotIn("foreign-claim", json.dumps(result))
                self.assertNotIn("other-task", json.dumps(result))
            finally:
                broker.close()

    def test_bare_conflict_projection_is_explicit_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _BareConflictStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "bare", "summary": "bad"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
            finally:
                broker.close()

    def test_unknown_positive_diagnostic_cannot_unlock_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _UnknownPositiveClaimStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "unknown", "summary": "bad"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertFalse(result["acquired"])
            finally:
                broker.close()

    def test_explicit_non_boolean_active_cannot_unlock_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _StringActiveClaimStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "string-active", "summary": "bad"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertFalse(result["acquired"])
            finally:
                broker.close()

    def test_primary_marker_contradiction_cannot_unlock_independent_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _PrimaryContradictionStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {
                            "route_key": "contradictory",
                            "summary": "bad",
                            "independent_verification_reason": "recheck",
                        },
                    )
                self.assertTrue(result["bypassed"])
                self.assertFalse(result["acquired"])
                self.assertFalse(result.get("independent_verification_accepted", False))
            finally:
                broker.close()

    def test_contradictory_claim_envelope_is_explicit_bypass(self):
        cases = (
            {"acquired": False, "claimed": True},
            {"ok": False, "acquired": True, "claimed": True},
            {"acquired": True, "claimed": True, "conflict": True},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    broker, store, workdir, candidate = self._fixture(
                        root, _ContradictoryClaimEnvelopeStore(**fields)
                    )
                    try:
                        with broker.session(
                            actor_id="agent",
                            episode=1,
                            workdir=workdir,
                            candidates={"task": (_task(root), candidate)},
                            deadline_monotonic=time.monotonic() + 3,
                            cps_store=store,
                            communication="blackboard",
                            route_claims_enabled=True,
                        ) as env:
                            result = _post(
                                env["CONTEXTSWARM_JUDGE_URL"],
                                "cps_claim_route",
                                {
                                    "route_key": "contradictory-envelope",
                                    "summary": "bad",
                                    "independent_verification_reason": "recheck",
                                },
                            )
                        self.assertTrue(result["bypassed"])
                        self.assertFalse(result["acquired"])
                        self.assertEqual(
                            result["route_claim_bypass_reason"], "unavailable"
                        )
                    finally:
                        broker.close()

    def test_claim_route_binding_requires_requested_route_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _RouteKeyMismatchStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "requested-route", "summary": "bad"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertFalse(result["acquired"])
            finally:
                broker.close()

    def test_update_and_release_binding_require_requested_claim_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, _ClaimIdMismatchStore()
            )
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    self.assertTrue(_post(url, "judge_check", {})["accepted"])
                    update = _post(
                        url,
                        "cps_update_route",
                        {"claim_id": "requested-claim", "status": "active"},
                    )
                    release = _post(
                        url,
                        "cps_release_route",
                        {"claim_id": "requested-claim"},
                    )
                self.assertTrue(update["bypassed"])
                self.assertTrue(release["bypassed"])
            finally:
                broker.close()

    def test_handled_terminal_negative_retires_local_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _StaleLeaseNegativeStore()
            broker, store, workdir, candidate = self._fixture(root, store)
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    claimed = _post(
                        url,
                        "cps_claim_route",
                        {"route_key": "stale", "summary": "temporary"},
                    )
                    self.assertTrue(claimed["acquired"])
                    self.assertTrue(_post(url, "judge_check", {})["accepted"])
                    token = url.rstrip("/").rsplit("/", 1)[-1]
                    self.assertTrue(broker._claims[token].route_claim_satisfied)
                    update = _post(
                        url,
                        "cps_update_route",
                        {"claim_id": claimed["claim"]["claim_id"], "status": "active"},
                    )
                    self.assertEqual(update["status"], "not_found")
                    self.assertFalse(broker._claims[token].route_claim_satisfied)
                    self.assertFalse(broker._claims[token].route_claim_ids)
                    claimed_again = _post(
                        url,
                        "cps_claim_route",
                        {"route_key": "stale-release", "summary": "temporary"},
                    )
                    self.assertTrue(claimed_again["acquired"])
                    released = _post(
                        url,
                        "cps_release_route",
                        {"claim_id": claimed_again["claim"]["claim_id"]},
                    )
                    self.assertEqual(released["status"], "not_found")
                    self.assertFalse(broker._claims[token].route_claim_satisfied)
                    self.assertFalse(broker._claims[token].route_claim_ids)
            finally:
                broker.close()

    def test_known_semantic_negative_in_error_or_reason_stays_visible(self):
        for field, value in (("error", "not_owner"), ("reason", "conflict")):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    broker, store, workdir, candidate = self._fixture(
                        root, _ErrorOnlyClaimStore(field, value)
                    )
                    try:
                        with broker.session(
                            actor_id="agent",
                            episode=1,
                            workdir=workdir,
                            candidates={"task": (_task(root), candidate)},
                            deadline_monotonic=time.monotonic() + 3,
                            cps_store=store,
                            communication="blackboard",
                            route_claims_enabled=True,
                        ) as env:
                            result = _post(
                                env["CONTEXTSWARM_JUDGE_URL"],
                                "cps_claim_route",
                                {"route_key": "semantic", "summary": "handled"},
                            )
                        self.assertFalse(result.get("bypassed", False))
                        self.assertFalse(result.get("acquired", False))
                    finally:
                        broker.close()

    def test_malformed_claim_response_is_explicit_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _MalformedClaimStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {"route_key": "broken", "summary": "broken"},
                    )
                self.assertTrue(result["bypassed"])
                self.assertEqual(result["route_claim_bypass_reason"], "unavailable")
                self.assertFalse(result["acquired"])
            finally:
                broker.close()

    def test_malformed_positive_response_is_explicit_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _BareSuccessClaimStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {
                            "route_key": "bad",
                            "summary": "bad",
                            "independent_verification_reason": "deliberate check",
                        },
                    )
                self.assertTrue(result["bypassed"])
                self.assertFalse(result.get("independent_verification_accepted", False))
            finally:
                broker.close()

    def test_non_boolean_route_envelope_fields_are_explicit_bypass(self):
        for field, value in (("ok", "true"), ("acquired", "true"), ("bypassed", "false")):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    broker, store, workdir, candidate = self._fixture(
                        root, _TypedEnvelopeClaimStore(field, value)
                    )
                    try:
                        with broker.session(
                            actor_id="agent",
                            episode=1,
                            workdir=workdir,
                            candidates={"task": (_task(root), candidate)},
                            deadline_monotonic=time.monotonic() + 3,
                            cps_store=store,
                            communication="blackboard",
                            route_claims_enabled=True,
                        ) as env:
                            result = _post(
                                env["CONTEXTSWARM_JUDGE_URL"],
                                "cps_claim_route",
                                {"route_key": "typed", "summary": "bad"},
                            )
                        self.assertTrue(result["bypassed"])
                        self.assertFalse(result.get("acquired", False))
                    finally:
                        broker.close()

    def test_terminal_reason_echo_cannot_satisfy_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root, _EchoOnlyClaimStore())
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {
                            "route_key": "bad",
                            "summary": "bad",
                            "independent_verification_reason": "deliberate check",
                        },
                    )
                self.assertFalse(result.get("bypassed", False))
                self.assertFalse(result.get("independent_verification_accepted", False))
            finally:
                broker.close()

    def test_route_text_is_redacted_at_broker_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root)
            try:
                with broker.session(
                    actor_id="agent",
                    episode=1,
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    route_claims_enabled=True,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_claim_route",
                        {
                            "route_key": "https://private.example/token",
                            "summary": "/home/ubuntu/private/result",
                        },
                    )
                self.assertNotIn("https://private.example/token", json.dumps(result))
                self.assertNotIn("/home/ubuntu/private/result", json.dumps(result))
                self.assertTrue(result["acquired"])
            finally:
                broker.close()

    def test_session_cannot_widen_broker_route_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "worker"
            workdir.mkdir()
            candidate = workdir / "result.lean"
            candidate.write_text("proof\n", encoding="utf-8")
            store = _RouteStore()
            evaluator = _TerminalEvaluator()
            broker = JudgeBroker(
                evaluator,
                threading.BoundedSemaphore(1),
                audit_path=root / "audit.jsonl",
                min_probe_interval_seconds=0,
                route_claims_enabled=False,
                route_claim_required=False,
            ).start()
            try:
                with self.assertRaisesRegex(ValueError, "cannot widen"):
                    with broker.session(
                        actor_id="agent",
                        episode=1,
                        workdir=workdir,
                        candidates={"task": (_task(root), candidate)},
                        deadline_monotonic=time.monotonic() + 3,
                        cps_store=store,
                        communication="blackboard",
                        route_claims_enabled=True,
                    ):
                        self.fail("session unexpectedly widened a denied route surface")
            finally:
                broker.close()

    def test_session_cannot_relax_manifest_required_route_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root)
            try:
                with self.assertRaisesRegex(ValueError, "disable the required"):
                    with broker.session(
                        actor_id="agent",
                        episode=1,
                        workdir=workdir,
                        candidates={"task": (_task(root), candidate)},
                        deadline_monotonic=time.monotonic() + 3,
                        cps_store=store,
                        communication="blackboard",
                        route_claims_enabled=False,
                        route_claim_required=False,
                    ):
                        self.fail("session unexpectedly relaxed a required route gate")
            finally:
                broker.close()


if __name__ == "__main__":
    unittest.main()
