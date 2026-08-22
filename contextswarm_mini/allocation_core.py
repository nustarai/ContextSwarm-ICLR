"""Pure, auditable allocation policies for the registered allocation arms.

This module deliberately has no dependency on the runner, configuration loader,
or CPS projection implementation.  Callers construct one immutable snapshot per
decision and policies return a decision without mutating run state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import hashlib
import json
import math
import re
import time
import unicodedata
from types import MappingProxyType
from typing import Any, Callable, ClassVar, Mapping


POLICY_UNIFORM_REFILL = "uniform_refill"
POLICY_TASK_STATE = "task_state"
POLICY_TRACE_STATE = "trace_state"
POLICY_LLM_SCHEDULER = "llm_scheduler"
SCHEDULER_OUTCOMES = frozenset(
    {
        "accepted",
        "invalid_output",
        "provider_error",
        "policy_timeout",
        "horizon_truncated",
        "not_invoked",
    }
)
MAX_SNAPSHOT_TASKS = 512
MAX_TRACE_REFERENCES_PER_TASK = 100
MAX_IDENTIFIER_CHARS = 512
# Allocation parameters are manifest-owned in normal runs, but the pure core
# also accepts them from callers.  Bound their shape before JSON encoding so a
# malicious/custom Mapping cannot force an arbitrarily deep traversal or make
# state-id construction materialize an unbounded object.
MAX_ALLOCATION_PARAMETER_DEPTH = 16
MAX_ALLOCATION_PARAMETER_NODES = 4096
MAX_ALLOCATION_PARAMETER_KEYS = 256
MAX_ALLOCATION_PARAMETER_ITEMS = 256
MAX_ALLOCATION_PARAMETER_STRING_CHARS = 4096
MAX_ALLOCATION_PARAMETER_BYTES = 256 * 1024
MAX_ALLOCATION_PARAMETER_INTEGER_DIGITS = 128
MAX_SCHEDULER_REASON_CHARS = 1000
MAX_SCHEDULER_REASON_BYTES = 4096
# The pure policy may be used without the bounded Pi transport.  Reject an
# untrusted provider response before stripping or JSON-decoding it so direct
# callers cannot make the parser materialize an unbounded payload.  Keep both
# limits: the character check is allocation-free, while the byte check handles
# compact strings containing large UTF-8 code points.
MAX_SCHEDULER_OUTPUT_CHARS = 64 * 1024
MAX_SCHEDULER_OUTPUT_BYTES = 64 * 1024
MAX_SCHEDULER_JSON_DEPTH = 64
# The scheduler receives a bounded UTF-8 wire prompt.  The limit is a policy
# input (and is therefore configurable by the runner), while this default keeps
# direct users of the pure core safe when they do not have a manifest object.
LLM_SCHEDULER_PROMPT_MAX_BYTES = 64 * 1024
LLM_SCHEDULER_PROMPT_MAX_TOKENS = 64 * 1024


class _SchedulerPromptError(ValueError):
    """Internal bounded-prompt rejection with a non-sensitive stable kind."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _finite(name: str, value: float, *, minimum: float | None = None) -> float:
    # JSON-facing numeric fields are deliberately restricted to the two
    # scalar types JSON can represent losslessly here. ``float(value)`` alone
    # accepts numeric strings, Decimal-like objects, and arbitrary user-defined
    # coercions, making malformed artifacts indistinguishable from values that
    # came from the registered manifest.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        # Very large integers can raise OverflowError during conversion; keep
        # all malformed numeric inputs on the stable ValueError path.
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return 0.0 if result == 0.0 else result


def _unit_interval(name: str, value: float) -> float:
    result = _finite(name, value)
    if result > 1.0 or result < 0.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return result


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _bounded_text(
    name: str,
    value: Any,
    *,
    allow_empty: bool = False,
    max_chars: int = MAX_IDENTIFIER_CHARS,
    max_bytes: int | None = None,
    reject_controls: bool = False,
) -> str:
    """Validate an externally supplied text field without coercing it.

    The allocation core is an immutable boundary.  Coercing ``None``, bytes,
    integers, or arbitrary objects with ``str(...)`` changes the identity of a
    decision and can make a caller-owned iterable look like a list of IDs.
    Keep normalization limited to the historical surrounding-whitespace trim,
    and make the type/size contract explicit before hashing or serializing.
    """

    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if reject_controls and any(
        ord(char) < 0x20
        or ord(char) == 0x7F
        or unicodedata.category(char) in {"Cc", "Cf", "Cs"}
        for char in value
    ):
        if name == "decision_id":
            raise ValueError("decision_id is an unsafe decision identifier")
        raise ValueError(f"{name} contains control characters")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{name} must be non-empty")
    if len(normalized) > max_chars:
        raise ValueError(f"{name} must be at most {max_chars} characters")
    if max_bytes is not None:
        try:
            encoded_length = len(normalized.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError(f"{name} must be valid UTF-8 text") from exc
        if encoded_length > max_bytes:
            raise ValueError(f"{name} exceeds its bounded UTF-8 size")
    return normalized


def _bounded_identifier_sequence(
    name: str,
    value: Any,
    *,
    max_items: int = MAX_TRACE_REFERENCES_PER_TASK,
) -> tuple[str, ...]:
    """Normalize a declared tuple/list of opaque IDs without broad coercion."""

    # A scalar string is iterable, which was the source of a particularly
    # subtle bug: ``"abc"`` became the three IDs ``("a", "b", "c")``.
    # Accept only the two sequence shapes represented by the public dataclass
    # contract; callers still receive an immutable tuple in the result.
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} must be a tuple or list of strings")
    if len(value) > max_items:
        raise ValueError(f"{name} must contain at most {max_items} values")
    normalized = tuple(
        _bounded_text(
            f"{name} values", item, allow_empty=False, reject_controls=True
        )
        for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must be unique")
    return normalized


def _weight_dict(instance: object) -> dict[str, float]:
    return {item.name: float(getattr(instance, item.name)) for item in fields(instance)}


def _validate_allocation_parameters(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
    active: set[int] | None = None,
) -> None:
    """Validate and bound an allocation-parameter JSON tree before copying it.

    This deliberately runs before ``json.dumps``.  The previous implementation
    let the JSON encoder recurse through caller-owned data first, so a deeply
    nested value could raise ``RecursionError`` (or consume substantial memory)
    before the scheduler's prompt bound had any chance to reject it.
    """

    if budget is None:
        budget = [0]
    if active is None:
        active = set()
    budget[0] += 1
    if budget[0] > MAX_ALLOCATION_PARAMETER_NODES:
        raise ValueError("allocation_parameters exceeds the bounded node limit")
    if depth > MAX_ALLOCATION_PARAMETER_DEPTH:
        raise ValueError("allocation_parameters exceeds the bounded depth limit")
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("allocation_parameters must not contain cycles")
        active.add(identity)
        try:
            count = len(value)
        except Exception as exc:  # pragma: no cover - hostile custom Mapping
            active.remove(identity)
            raise ValueError("allocation_parameters mapping length is unavailable") from exc
        if count > MAX_ALLOCATION_PARAMETER_KEYS:
            raise ValueError("allocation_parameters exceeds the bounded key limit")
        try:
            items = value.items()
            for index, (key, child) in enumerate(items):
                if index >= MAX_ALLOCATION_PARAMETER_KEYS:
                    raise ValueError("allocation_parameters exceeds the bounded key limit")
                if not isinstance(key, str) or not key or len(key) > MAX_IDENTIFIER_CHARS:
                    raise ValueError("allocation_parameters keys must be bounded strings")
                _validate_allocation_parameters(
                    child, depth=depth + 1, budget=budget, active=active
                )
        except ValueError:
            raise
        except Exception as exc:  # pragma: no cover - hostile custom Mapping
            raise ValueError("allocation_parameters mapping is not readable") from exc
        finally:
            active.remove(identity)
        return
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("allocation_parameters must not contain cycles")
        active.add(identity)
        if len(value) > MAX_ALLOCATION_PARAMETER_ITEMS:
            active.remove(identity)
            raise ValueError("allocation_parameters exceeds the bounded item limit")
        try:
            for child in value:
                _validate_allocation_parameters(
                    child, depth=depth + 1, budget=budget, active=active
                )
        finally:
            active.remove(identity)
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if len(str(abs(value))) > MAX_ALLOCATION_PARAMETER_INTEGER_DIGITS:
            raise ValueError("allocation_parameters integer is too large")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("allocation_parameters must contain finite numbers")
        return
    if isinstance(value, str):
        if len(value) > MAX_ALLOCATION_PARAMETER_STRING_CHARS:
            raise ValueError("allocation_parameters strings are too long")
        return
    raise ValueError("allocation_parameters must be finite JSON data")


def _freeze_json(value: Any) -> Any:
    """Detach and deeply freeze a bounded JSON-compatible manifest fragment."""

    _validate_allocation_parameters(value)

    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_ALLOCATION_PARAMETER_BYTES:
            raise ValueError("allocation_parameters exceeds the bounded byte limit")
        detached = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        if str(exc).startswith("allocation_parameters exceeds"):
            raise
        raise ValueError("allocation_parameters must be finite JSON data") from exc
    def freeze_detached(item: Any) -> Any:
        if isinstance(item, dict):
            return MappingProxyType(
                {str(key): freeze_detached(child) for key, child in item.items()}
            )
        if isinstance(item, list):
            return tuple(freeze_detached(child) for child in item)
        return item

    return freeze_detached(detached)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _scheduler_identifier_is_safe(
    value: str,
    *,
    allow_empty: bool = True,
    allow_structured_decision: bool = False,
) -> bool:
    """Return whether an opaque ID can be shown to the read-only scheduler.

    Task and trace IDs are needed verbatim in the scheduler response, so they
    cannot be replaced with a redaction token.  Reject obvious path/URI and
    multiline values instead; a rejected snapshot takes the deterministic
    fallback path and never reaches the model.
    """

    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > MAX_IDENTIFIER_CHARS:
        return False
    if any(
        ord(char) < 0x20
        or ord(char) == 0x7F
        or unicodedata.category(char) == "Cf"
        for char in value
    ):
        return False
    # Canonical paired decision IDs may contain one slash (for example
    # ``paired-007/decision-000042``).  Permit only that registered shape;
    # absolute/relative paths and arbitrary path-like IDs fail closed.
    if "\\" in value or re.match(r"^[A-Za-z]:[\\/]", value):
        return False
    if "/" in value:
        if not allow_structured_decision or re.fullmatch(
            r"paired-[A-Za-z0-9_.-]+/decision-[A-Za-z0-9_.-]+",
            value,
        ) is None:
            return False
    if "://" in value or value.startswith(("file:", "path:", "./", "../")):
        return False
    return True


def _scheduler_parameter_key_is_safe(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered or len(lowered) > MAX_IDENTIFIER_CHARS:
        return False
    # Manifest parameter names are normally simple identifiers.  A future
    # caller may attach arbitrary metadata to ``allocation_parameters``; those
    # fields must not become a covert path/transcript channel.
    sensitive = (
        "path",
        "transcript",
        "secret",
        "token",
        "endpoint",
        "private",
        "payload",
        "raw",
        "content",
    )
    return not any(fragment in lowered for fragment in sensitive)


def _scheduler_parameter_value(value: Any, *, key: str = "") -> Any:
    """Copy manifest parameters into a JSON-safe, non-sensitive view.

    Allocation parameters are expected to be numeric tables.  We retain
    bounded scalar labels for forward compatibility, but redact suspicious
    keys/values rather than serializing arbitrary caller-owned text.  The
    redaction token is deterministic and contains no source material.
    """

    if key and not _scheduler_parameter_key_is_safe(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in sorted(value.items(), key=lambda item: str(item[0])):
            child_key = str(raw_key).strip()
            if not _scheduler_parameter_key_is_safe(child_key):
                # Keep the shape auditable without retaining the field name or
                # value, either of which may disclose private provenance.
                continue
            result[child_key] = _scheduler_parameter_value(raw_value, key=child_key)
        return result
    if isinstance(value, (tuple, list)):
        if len(value) > MAX_TRACE_REFERENCES_PER_TASK:
            return "<redacted>"
        return [_scheduler_parameter_value(item) for item in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return "<redacted>"
        # Preserve integer JSON spelling for manifest hashes and exact state
        # comparisons where possible.
        return value
    if isinstance(value, str):
        # Registered allocation parameters are numeric tables.  Do not carry
        # any caller-owned string into a model prompt, even when it happens to
        # look identifier-like: a transcript/path can be encoded without
        # whitespace or a URI marker.
        return "<redacted>"
    return "<redacted>"


def _scheduler_reason_is_safe(value: Any) -> bool:
    """Check the bounded model explanation before persisting it in artifacts.

    The reason is model output, not an opaque identifier.  Accepting arbitrary
    control characters or path/URI-like text here would let a provider inject
    terminal/log markup or echo operator-local provenance into the run record.
    Unsafe reasons are rejected and therefore use the charged deterministic
    fallback; the raw value is never copied into an error message.
    """

    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped or len(stripped) > MAX_SCHEDULER_REASON_CHARS:
        return False
    try:
        if len(stripped.encode("utf-8")) > MAX_SCHEDULER_REASON_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    if any(
        ord(char) < 0x20
        or ord(char) == 0x7F
        or unicodedata.category(char) in {"Cc", "Cf", "Cs"}
        for char in stripped
    ):
        return False
    lowered = stripped.lower()
    if "\\" in stripped or "://" in stripped:
        return False
    if lowered.startswith(("file:", "path:", "data:", "mailto:")):
        return False
    # A slash is fine in ordinary prose (for example ``A/B``), but reject
    # absolute/relative path-looking fragments rather than every slash.
    if re.search(r"(?:^|[\s(])(?:\.\.?/|/[A-Za-z0-9_.-]|[A-Za-z]:[\\/])", stripped):
        return False
    # These markers are not useful allocation semantics and are common ways
    # for a provider to echo caller-owned provenance.  Keep the rejection
    # stable and case-insensitive without including the source text in errors.
    if any(marker in lowered for marker in ("transcript", "private", "secret", "payload")):
        return False
    return True


def _scheduler_state_dict(snapshot: "AllocationStateSnapshot") -> dict[str, Any]:
    """Build the exact bounded state view allowed in an LLM prompt.

    This intentionally mirrors :meth:`AllocationStateSnapshot.public_dict`
    field-for-field for the core state and task/trace records.  It does not
    include arbitrary Python objects or caller-owned raw payloads.  Unsafe IDs
    fail closed because the scheduler must return the original ID verbatim.
    """

    def safe_id(
        value: str,
        field_name: str,
        *,
        allow_empty: bool = False,
        allow_structured_decision: bool = False,
    ) -> str:
        if not _scheduler_identifier_is_safe(
            value,
            allow_empty=allow_empty,
            allow_structured_decision=allow_structured_decision,
        ):
            raise ValueError(f"scheduler snapshot contains an unsafe {field_name}")
        return value

    # Start with the exact immutable snapshot projection.  This keeps the LLM
    # arm's causal input identical to Trace-State; only unsafe caller-owned
    # strings are replaced/removed from the wire representation.
    public = snapshot.public_dict()
    for task in snapshot.tasks:
        safe_id(task.task_id, "task identifier", allow_empty=False)
        for value in task.trace_reference_ids:
            safe_id(value, "trace reference", allow_empty=False)
    safe_id(
        snapshot.decision_id,
        "decision identifier",
        allow_empty=False,
        allow_structured_decision=True,
    )

    # Opaque watermarks and outcome IDs are useful for audit identity but are
    # never echoed by the scheduler.  Keep their shape while removing path,
    # URI, control-character, and overlong values.
    public["trace_watermark"] = (
        snapshot.trace_watermark
        if _scheduler_identifier_is_safe(snapshot.trace_watermark)
        else "<opaque>"
    )
    public["allocation_config_sha256"] = (
        snapshot.allocation_config_sha256
        if _scheduler_identifier_is_safe(snapshot.allocation_config_sha256)
        else "<opaque>"
    )
    for task in public.get("tasks", []):
        if not isinstance(task, dict):
            raise ValueError("scheduler snapshot task projection is malformed")
        for key in (
            "trace_source_outcome_ids",
            "checker_outcome_ids",
        ):
            values = task.get(key, [])
            if not isinstance(values, list):
                raise ValueError("scheduler snapshot identifier projection is malformed")
            task[key] = [
                value if _scheduler_identifier_is_safe(value) else "<opaque>"
                for value in values
            ]
    public["allocation_parameters"] = _scheduler_parameter_value(
        _thaw_json(snapshot.allocation_parameters)
    )
    return public


def _resolve_prompt_bytes(
    *,
    max_bytes: int | None = None,
    prompt_max_bytes: int | None = None,
    max_prompt_bytes: int | None = None,
    llm_scheduler_prompt_max_bytes: int | None = None,
) -> int:
    """Resolve compatibility aliases to one positive prompt bound."""

    provided = [
        value
        for value in (
            max_bytes,
            prompt_max_bytes,
            max_prompt_bytes,
            llm_scheduler_prompt_max_bytes,
        )
        if value is not None
    ]
    for value in provided:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("LLM scheduler prompt max bytes must be a positive integer")
    if provided and any(value != provided[0] for value in provided[1:]):
        raise ValueError("LLM scheduler prompt byte limits disagree")
    value = provided[0] if provided else LLM_SCHEDULER_PROMPT_MAX_BYTES
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("LLM scheduler prompt max bytes must be a positive integer")
    return value


def _prompt_token_count(prompt: str) -> int:
    """Deterministic tokenizer-independent upper bound for prompt tokens."""

    # Provider tokenizers differ and are not available in the pure policy core.
    # Count one token per UTF-8 byte as a conservative provider-independent
    # ceiling.  This deliberately over-counts ordinary text, but makes the
    # optional token guard fail closed rather than undercounting punctuation- or
    # non-ASCII-heavy prompts.  The byte limit remains the hard transport bound.
    return len(prompt.encode("utf-8"))


def _resolve_prompt_tokens(
    *,
    prompt_max_tokens: int | None = None,
    max_prompt_tokens: int | None = None,
    llm_scheduler_prompt_max_tokens: int | None = None,
) -> int:
    provided = [
        value
        for value in (
            prompt_max_tokens,
            max_prompt_tokens,
            llm_scheduler_prompt_max_tokens,
        )
        if value is not None
    ]
    for value in provided:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("LLM scheduler prompt max tokens must be a positive integer")
    if provided and any(value != provided[0] for value in provided[1:]):
        raise ValueError("LLM scheduler prompt token limits disagree")
    value = provided[0] if provided else LLM_SCHEDULER_PROMPT_MAX_TOKENS
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("LLM scheduler prompt max tokens must be a positive integer")
    return value


@dataclass(frozen=True)
class TraceFeatures:
    """Normalized trace-only evidence; zero is a neutral/absent projection.

    ``evidence_association`` is the registered ``V`` term.  It is deliberately
    not named validation: authoritative checker outcomes belong only in
    :class:`TaskState` and must not be counted a second time through the trace.

    ``drag`` is the historical aggregate field.  New projections provide the
    four explicit density components below and the scorer then evaluates the
    registered ``D = dDup*duplication + dRef*refutation + dStale*staleness +
    dLineage*lineage_stagnation`` expression.  Keeping the optional constructor
    values as a private mode marker lets old callers which only provide
    ``drag`` retain their exact behavior without ever adding both forms of
    density to one increment.
    """

    actionability: float = 0.0
    evidence_association: float = 0.0
    positive_feedback: float = 0.0
    negative_feedback: float = 0.0
    drag: float = 0.0
    duplication: float | None = None
    refutation: float | None = None
    staleness: float | None = None
    lineage_stagnation: float | None = None
    _component_mode: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "actionability",
            "evidence_association",
            "positive_feedback",
            "negative_feedback",
            "drag",
        ):
            object.__setattr__(
                self,
                name,
                _unit_interval(f"trace.{name}", getattr(self, name)),
            )
        component_mode = False
        for name in (
            "duplication",
            "refutation",
            "staleness",
            "lineage_stagnation",
        ):
            value = getattr(self, name)
            if value is not None:
                component_mode = True
                value = _unit_interval(f"trace.{name}", value)
            else:
                # Keep the public/state representation JSON-shaped while the
                # private mode marker remembers whether this was an old
                # aggregate-only construction.
                value = 0.0
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_component_mode", component_mode)

    def public_dict(self) -> dict[str, float]:
        return {
            "actionability": self.actionability,
            "evidence_association": self.evidence_association,
            "positive_feedback": self.positive_feedback,
            "negative_feedback": self.negative_feedback,
            # Retain the compatibility aggregate in public snapshots.  The
            # explicit fields are always emitted as well so state IDs and
            # scheduler prompts expose the complete trace contract.
            "drag": self.drag,
            "duplication": self.duplication,
            "refutation": self.refutation,
            "staleness": self.staleness,
            "lineage_stagnation": self.lineage_stagnation,
        }

    @property
    def uses_drag_components(self) -> bool:
        """Whether the instance was constructed with explicit D components."""

        return self._component_mode

    @property
    def drag_components(self) -> tuple[float, float, float, float]:
        """Return components in the registered duplication/refutation order."""

        return (
            self.duplication,
            self.refutation,
            self.staleness,
            self.lineage_stagnation,
        )

    as_dict = public_dict


@dataclass(frozen=True)
class TaskState:
    """One task's normalized, causal state at an allocation decision."""

    task_id: str
    eligible: bool
    active_allocations: int
    checker_quality: float = 0.0
    recent_progress: float = 0.0
    starvation: float = 0.0
    failure_no_progress: float = 0.0
    trace: TraceFeatures = field(default_factory=TraceFeatures)
    trace_reference_ids: tuple[str, ...] = ()
    checker_outcome_ids: tuple[str, ...] = ()
    trace_source_outcome_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        task_id = _bounded_text("task_id", self.task_id, reject_controls=True)
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a boolean")
        if not isinstance(self.trace, TraceFeatures):
            raise ValueError("trace must be TraceFeatures")
        identifier_fields: dict[str, tuple[str, ...]] = {}
        for name in (
            "trace_reference_ids",
            "checker_outcome_ids",
            "trace_source_outcome_ids",
        ):
            identifier_fields[name] = _bounded_identifier_sequence(
                name, getattr(self, name)
            )
        if set(identifier_fields["checker_outcome_ids"]) & set(
            identifier_fields["trace_source_outcome_ids"]
        ):
            raise ValueError("checker_outcome_ids and trace_source_outcome_ids must be disjoint")
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(
            self,
            "active_allocations",
            _nonnegative_int("active_allocations", self.active_allocations),
        )
        for name in (
            "checker_quality",
            "recent_progress",
            "starvation",
            "failure_no_progress",
        ):
            object.__setattr__(self, name, _unit_interval(name, getattr(self, name)))
        for name, values in identifier_fields.items():
            object.__setattr__(self, name, values)

    def public_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "task_id": self.task_id,
            "eligible": self.eligible,
            "active_allocations": self.active_allocations,
            "checker_quality": self.checker_quality,
            "recent_progress": self.recent_progress,
            "starvation": self.starvation,
            "failure_no_progress": self.failure_no_progress,
        }
        if include_trace:
            result["trace"] = self.trace.public_dict()
            result["trace_reference_ids"] = list(self.trace_reference_ids)
            result["trace_source_outcome_ids"] = list(self.trace_source_outcome_ids)
        result["checker_outcome_ids"] = list(self.checker_outcome_ids)
        return result

    as_dict = public_dict


@dataclass(frozen=True)
class AllocationStateSnapshot:
    """Immutable common state supplied to every registered allocation arm."""

    SCHEMA_VERSION: ClassVar[str] = "contextswarm_allocation_state_v1"

    decision_id: str
    decision_index: int
    elapsed_seconds: float
    remaining_seconds: float
    total_capacity: int
    active_solver_slots: int
    scheduler_reserved_slots: int
    free_slots: int
    tasks: tuple[TaskState, ...]
    owned_scheduler_reservation_slots: int = 0
    trace_watermark: str = ""
    allocation_config_sha256: str = ""
    allocation_parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        decision_id = _bounded_text(
            "decision_id", self.decision_id, reject_controls=True
        )
        if not isinstance(self.tasks, (tuple, list)):
            raise ValueError("tasks must be a tuple or list of TaskState records")
        tasks = tuple(self.tasks)
        if any(not isinstance(task, TaskState) for task in tasks):
            raise ValueError("tasks must contain only TaskState records")
        if len(tasks) > MAX_SNAPSHOT_TASKS:
            raise ValueError(f"snapshot must contain at most {MAX_SNAPSHOT_TASKS} tasks")
        task_ids = [task.task_id for task in tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("task IDs must be unique within a snapshot")
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(
            self, "decision_index", _nonnegative_int("decision_index", self.decision_index)
        )
        object.__setattr__(
            self, "elapsed_seconds", _finite("elapsed_seconds", self.elapsed_seconds, minimum=0.0)
        )
        object.__setattr__(
            self,
            "remaining_seconds",
            _finite("remaining_seconds", self.remaining_seconds, minimum=0.0),
        )
        object.__setattr__(
            self, "total_capacity", _nonnegative_int("total_capacity", self.total_capacity)
        )
        object.__setattr__(
            self,
            "active_solver_slots",
            _nonnegative_int("active_solver_slots", self.active_solver_slots),
        )
        object.__setattr__(
            self,
            "scheduler_reserved_slots",
            _nonnegative_int("scheduler_reserved_slots", self.scheduler_reserved_slots),
        )
        object.__setattr__(
            self,
            "owned_scheduler_reservation_slots",
            _nonnegative_int(
                "owned_scheduler_reservation_slots",
                self.owned_scheduler_reservation_slots,
            ),
        )
        object.__setattr__(self, "free_slots", _nonnegative_int("free_slots", self.free_slots))
        object.__setattr__(self, "tasks", tasks)
        if self.owned_scheduler_reservation_slots > 1:
            raise ValueError("owned_scheduler_reservation_slots must be at most 1")
        if self.owned_scheduler_reservation_slots > self.scheduler_reserved_slots:
            raise ValueError(
                "owned_scheduler_reservation_slots must not exceed scheduler_reserved_slots"
            )
        if sum(task.active_allocations for task in tasks) != self.active_solver_slots:
            raise ValueError("active_solver_slots must equal the sum of task active_allocations")
        if (
            self.active_solver_slots + self.scheduler_reserved_slots + self.free_slots
            != self.total_capacity
        ):
            raise ValueError(
                "active_solver_slots + scheduler_reserved_slots + free_slots must equal total_capacity"
            )
        for name in ("trace_watermark", "allocation_config_sha256"):
            value = _bounded_text(
                name,
                getattr(self, name),
                allow_empty=True,
                reject_controls=True,
            )
            object.__setattr__(self, name, value)
        object.__setattr__(self, "allocation_parameters", _freeze_json(self.allocation_parameters))

    @property
    def state_id(self) -> str:
        """Canonical identity for the entire same-state counterfactual input."""

        canonical = {
            "schema_version": self.SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "decision_index": self.decision_index,
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_seconds": self.remaining_seconds,
            "total_capacity": self.total_capacity,
            "active_solver_slots": self.active_solver_slots,
            "scheduler_reserved_slots": self.scheduler_reserved_slots,
            "owned_scheduler_reservation_slots": self.owned_scheduler_reservation_slots,
            "free_slots": self.free_slots,
            "trace_watermark": self.trace_watermark,
            "allocation_config_sha256": self.allocation_config_sha256,
            "allocation_parameters": _thaw_json(self.allocation_parameters),
            "task_order": [task.task_id for task in self.tasks],
            "eligible_task_ids": list(self.eligible_task_ids),
            "tasks": [task.public_dict() for task in self.tasks],
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def capacity(self) -> int:
        """Compatibility alias for callers using the early core API sketch."""

        return self.total_capacity

    @property
    def eligible_tasks(self) -> tuple[TaskState, ...]:
        return tuple(sorted((task for task in self.tasks if task.eligible), key=lambda task: task.task_id))

    @property
    def eligible_task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.eligible_tasks)

    @property
    def trace_reference_ids(self) -> frozenset[str]:
        return frozenset(
            reference
            for task in self.eligible_tasks
            for reference in task.trace_reference_ids
        )

    def public_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "state_id": self.state_id,
            "decision_index": self.decision_index,
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_seconds": self.remaining_seconds,
            "total_capacity": self.total_capacity,
            "active_solver_slots": self.active_solver_slots,
            "scheduler_reserved_slots": self.scheduler_reserved_slots,
            "owned_scheduler_reservation_slots": self.owned_scheduler_reservation_slots,
            "free_slots": self.free_slots,
            "trace_watermark": self.trace_watermark,
            "allocation_config_sha256": self.allocation_config_sha256,
            "allocation_parameters": _thaw_json(self.allocation_parameters),
            "task_order": [task.task_id for task in self.tasks],
            "eligible_task_ids": list(self.eligible_task_ids),
            "tasks": [task.public_dict(include_trace=include_trace) for task in self.tasks],
        }

    as_dict = public_dict


@dataclass(frozen=True)
class TaskScoreWeights:
    """Manifest-owned coefficients for ``(vQ*Q+vDelta*Delta+vX*X-vG*G)/(1+n)``."""

    checker_quality: float = 1.0
    recent_progress: float = 1.0
    starvation: float = 1.0
    failure_no_progress: float = 1.0

    def __post_init__(self) -> None:
        for item in fields(self):
            object.__setattr__(
                self,
                item.name,
                _finite(item.name, getattr(self, item.name), minimum=0.0),
            )

    @classmethod
    def from_mapping(cls, values: Mapping[str, float] | None = None) -> TaskScoreWeights:
        if values is None:
            values = {}
        if not isinstance(values, Mapping):
            raise ValueError("task score weights must be a mapping")
        if any(not isinstance(key, str) for key in values):
            raise ValueError("task score weight keys must be strings")
        allowed = {item.name for item in fields(cls)}
        unknown = {key for key in values if key not in allowed}
        if unknown:
            raise ValueError("unknown task score weights: " + ", ".join(sorted(unknown)))
        # Let __post_init__ enforce the exact numeric contract; never coerce
        # strings or booleans through float(...).
        return cls(**dict(values))

    def public_dict(self) -> dict[str, float]:
        return _weight_dict(self)

    as_dict = public_dict


@dataclass(frozen=True)
class TraceScoreWeights:
    """Manifest-owned coefficients for the registered trace terms.

    ``drag`` remains a compatibility alias for older manifests.  A new
    manifest supplies ``density_penalty_weight`` and the four component
    weights explicitly.  The latter are optional at the Python boundary so
    old direct callers continue to work; ``from_mapping`` preserves whichever
    contract the mapping actually declared.
    """

    actionability: float = 1.0
    evidence_association: float = 1.0
    positive_feedback: float = 1.0
    negative_feedback: float = 1.0
    drag: float = 1.0
    density_penalty_weight: float | None = None
    duplication_component_weight: float | None = None
    # ``duplicate_component_weight`` is the spelling frozen by the v1
    # artifact fixture.  Keep ``duplication_component_weight`` as a source
    # compatibility alias for early development manifests.
    duplicate_component_weight: float | None = None
    refutation_component_weight: float | None = None
    staleness_component_weight: float | None = None
    lineage_stagnation_component_weight: float | None = None
    _component_mode: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "actionability",
            "evidence_association",
            "positive_feedback",
            "negative_feedback",
            "drag",
        ):
            object.__setattr__(
                self,
                name,
                _finite(name, getattr(self, name), minimum=0.0),
            )
        component_mode = False
        for name in (
            "density_penalty_weight",
            "duplication_component_weight",
            "duplicate_component_weight",
            "refutation_component_weight",
            "staleness_component_weight",
            "lineage_stagnation_component_weight",
        ):
            value = getattr(self, name)
            if value is not None:
                component_mode = True
                value = _finite(name, value, minimum=0.0)
            object.__setattr__(self, name, value)
        if (
            self.duplication_component_weight is not None
            and self.duplicate_component_weight is not None
            and self.duplication_component_weight
            != self.duplicate_component_weight
        ):
            raise ValueError(
                "duplication_component_weight and duplicate_component_weight "
                "contradict each other"
            )
        object.__setattr__(self, "_component_mode", component_mode)

    @classmethod
    def from_mapping(cls, values: Mapping[str, float] | None = None) -> TraceScoreWeights:
        if values is None:
            values = {}
        if not isinstance(values, Mapping):
            raise ValueError("trace score weights must be a mapping")
        if any(not isinstance(key, str) for key in values):
            raise ValueError("trace score weight keys must be strings")
        allowed = {
            "actionability",
            "evidence_association",
            "positive_feedback",
            "negative_feedback",
            "drag",
            "density_penalty_weight",
            "duplication_component_weight",
            "duplicate_component_weight",
            "refutation_component_weight",
            "staleness_component_weight",
            "lineage_stagnation_component_weight",
        }
        unknown = {key for key in values if key not in allowed}
        if unknown:
            raise ValueError("unknown trace score weights: " + ", ".join(sorted(unknown)))
        if "drag" in values and "density_penalty_weight" in values:
            try:
                aliases_agree = float(values["drag"]) == float(
                    values["density_penalty_weight"]
                )
            except (TypeError, ValueError, OverflowError):
                aliases_agree = False
            if not aliases_agree:
                raise ValueError(
                    "drag and density_penalty_weight contradict each other"
                )
        if (
            "duplication_component_weight" in values
            and "duplicate_component_weight" in values
        ):
            try:
                aliases_agree = float(values["duplication_component_weight"]) == float(
                    values["duplicate_component_weight"]
                )
            except (TypeError, ValueError, OverflowError):
                aliases_agree = False
            if not aliases_agree:
                raise ValueError(
                    "duplication_component_weight and duplicate_component_weight "
                    "contradict each other"
                )
        return cls(**dict(values))

    def public_dict(self) -> dict[str, float]:
        density = self.density_penalty_weight
        if density is None:
            density = self.drag
        components = self.component_weights
        canonical = {
            "actionability": self.actionability,
            "evidence_association": self.evidence_association,
            "positive_feedback": self.positive_feedback,
            "negative_feedback": self.negative_feedback,
            "density_penalty_weight": density,
            "duplicate_component_weight": components[0],
            "refutation_component_weight": components[1],
            "staleness_component_weight": components[2],
            "lineage_stagnation_component_weight": components[3],
        }
        if self._component_mode:
            return canonical
        # Preserve the exact five-key shape for callers using the historical
        # aggregate-only weight mapping.
        return {
            "actionability": self.actionability,
            "evidence_association": self.evidence_association,
            "positive_feedback": self.positive_feedback,
            "negative_feedback": self.negative_feedback,
            "drag": self.drag,
        }

    @property
    def uses_drag_components(self) -> bool:
        """Whether the manifest/direct mapping declared explicit D weights."""

        return self._component_mode

    @property
    def density_weight(self) -> float:
        """Coefficient multiplying the component density ``D``."""

        return self.density_penalty_weight if self.density_penalty_weight is not None else self.drag

    @property
    def component_weights(self) -> tuple[float, float, float, float]:
        """Return ``(dDup, dRef, dStale, dLineage)`` with safe legacy defaults."""

        if not self._component_mode:
            # An old ``drag`` coefficient represented one aggregate density;
            # using it for each explicit component is the least surprising
            # compatibility behavior if an old weight object is paired with a
            # newly projected trace.
            return (self.drag, self.drag, self.drag, self.drag)
        return tuple(
            0.0 if value is None else value
            for value in (
                (
                    self.duplicate_component_weight
                    if self.duplicate_component_weight is not None
                    else self.duplication_component_weight
                ),
                self.refutation_component_weight,
                self.staleness_component_weight,
                self.lineage_stagnation_component_weight,
            )
        )  # type: ignore[return-value]

    as_dict = public_dict


DEFAULT_TASK_SCORE_WEIGHTS = TaskScoreWeights()
DEFAULT_TRACE_SCORE_WEIGHTS = TraceScoreWeights()


class TaskStateScorer:
    """Score only ordinary task/checker state; trace fields are never read."""

    def __init__(self, weights: TaskScoreWeights = DEFAULT_TASK_SCORE_WEIGHTS) -> None:
        if not isinstance(weights, TaskScoreWeights):
            raise TypeError("weights must be TaskScoreWeights")
        self.weights = weights

    def score_task(self, task: TaskState) -> float:
        weights = self.weights
        numerator = (
            weights.checker_quality * task.checker_quality
            + weights.recent_progress * task.recent_progress
            + weights.starvation * task.starvation
            - weights.failure_no_progress * task.failure_no_progress
        )
        return numerator / (1.0 + task.active_allocations)

    def score_snapshot(self, snapshot: AllocationStateSnapshot) -> dict[str, float]:
        return {task.task_id: self.score_task(task) for task in snapshot.eligible_tasks}


class TraceStateScorer:
    """Add normalized trace evidence to the shared task-only scorer."""

    def __init__(
        self,
        task_scorer: TaskStateScorer | None = None,
        weights: TraceScoreWeights = DEFAULT_TRACE_SCORE_WEIGHTS,
    ) -> None:
        if not isinstance(weights, TraceScoreWeights):
            raise TypeError("weights must be TraceScoreWeights")
        self.task_scorer = task_scorer or TaskStateScorer()
        self.weights = weights

    def trace_increment(self, task: TaskState) -> float:
        weights = self.weights
        trace = task.trace
        if trace.uses_drag_components:
            d_dup, d_ref, d_stale, d_lineage = weights.component_weights
            density = (
                d_dup * trace.duplication
                + d_ref * trace.refutation
                + d_stale * trace.staleness
                + d_lineage * trace.lineage_stagnation
            )
            density_penalty = weights.density_weight
        else:
            # Legacy aggregate-only callers retain the pre-component formula.
            density = trace.drag
            density_penalty = weights.drag
        numerator = (
            weights.actionability * trace.actionability
            + weights.evidence_association * trace.evidence_association
            + weights.positive_feedback * trace.positive_feedback
            - weights.negative_feedback * trace.negative_feedback
            - density_penalty * density
        )
        return numerator / (1.0 + task.active_allocations)

    def score_task(self, task: TaskState) -> float:
        return self.task_scorer.score_task(task) + self.trace_increment(task)

    def score_snapshot(self, snapshot: AllocationStateSnapshot) -> dict[str, float]:
        return {task.task_id: self.score_task(task) for task in snapshot.eligible_tasks}

    def increments(self, snapshot: AllocationStateSnapshot) -> dict[str, float]:
        return {task.task_id: self.trace_increment(task) for task in snapshot.eligible_tasks}


def _immutable_scores(values: Mapping[str, float]) -> Mapping[str, float]:
    if not isinstance(values, Mapping):
        raise ValueError("scores must be a mapping")
    normalized: dict[str, float] = {}
    for key, value in values.items():
        if not isinstance(key, str):
            raise ValueError("score keys must be strings")
        normalized_key = _bounded_text(
            "score key", key, allow_empty=False, reject_controls=True
        )
        if normalized_key in normalized:
            raise ValueError("score keys must be unique after normalization")
        normalized[normalized_key] = _finite(f"score[{normalized_key!r}]", value)
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class LLMSchedulerCost:
    """One scheduler call's explicit contribution to the common cost ledger."""

    calls: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    reservation_slots: int = 1
    occupied_slot_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("calls", "input_tokens", "output_tokens", "reservation_slots"):
            object.__setattr__(self, name, _nonnegative_int(name, getattr(self, name)))
        latency = _finite("latency_seconds", self.latency_seconds, minimum=0.0)
        if self.calls != 1:
            raise ValueError("one scheduler-call cost record must have calls=1")
        if self.reservation_slots != 1:
            raise ValueError("one scheduler call must reserve exactly one capacity slot")
        occupied = self.occupied_slot_seconds
        if occupied is None:
            occupied = latency * self.reservation_slots
        occupied = _finite("occupied_slot_seconds", occupied, minimum=0.0)
        object.__setattr__(self, "latency_seconds", latency)
        object.__setattr__(self, "occupied_slot_seconds", occupied)

    def public_dict(self) -> dict[str, int | float]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_seconds": self.latency_seconds,
            "reservation_slots": self.reservation_slots,
            "occupied_slot_seconds": float(self.occupied_slot_seconds or 0.0),
            "total_tokens": self.input_tokens + self.output_tokens,
            "reserved_slot_seconds": float(self.occupied_slot_seconds or 0.0),
        }

    as_dict = public_dict


@dataclass(frozen=True)
class LLMSchedulerResponse:
    """Transport-neutral result returned by a runner-owned model invoker."""

    output: str
    returncode: int = 0
    timed_out: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    reservation_slots: int = 1
    occupied_slot_seconds: float | None = None
    # True means the provider call was truncated by the fixed experiment
    # horizon (as opposed to an ordinary provider timeout).  The runner uses
    # this bit to record ``not_admitted_horizon`` without charging a
    # deterministic policy fallback or admitting a late assignment.
    run_horizon_reached: bool = False
    recoverable_invocation_error: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.output, str):
            raise ValueError("output must be a string")
        if isinstance(self.returncode, bool) or not isinstance(self.returncode, int):
            raise ValueError("returncode must be an integer")
        if not isinstance(self.timed_out, bool):
            raise ValueError("timed_out must be a boolean")
        if not isinstance(self.run_horizon_reached, bool):
            raise ValueError("run_horizon_reached must be a boolean")
        if not isinstance(self.recoverable_invocation_error, bool):
            raise ValueError("recoverable_invocation_error must be a boolean")
        LLMSchedulerCost(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_seconds=self.latency_seconds,
            reservation_slots=self.reservation_slots,
            occupied_slot_seconds=self.occupied_slot_seconds,
        )

    @property
    def cost(self) -> LLMSchedulerCost:
        return LLMSchedulerCost(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_seconds=self.latency_seconds,
            reservation_slots=self.reservation_slots,
            occupied_slot_seconds=self.occupied_slot_seconds,
        )


@dataclass(frozen=True)
class AllocationDecision:
    """Immutable and JSON-friendly result of one pure allocation decision."""

    SCHEMA_VERSION: ClassVar[str] = "contextswarm_allocation_core_decision_v1"

    decision_id: str
    state_id: str
    decision_index: int
    policy: str
    selected_task_id: str
    reason: str
    scores: Mapping[str, float] = field(default_factory=dict)
    task_scores: Mapping[str, float] = field(default_factory=dict)
    trace_increments: Mapping[str, float] = field(default_factory=dict)
    trace_reference_ids: tuple[str, ...] = ()
    fallback: bool = False
    fallback_reason: str = ""
    scheduler_cost: LLMSchedulerCost | None = None
    scheduler_outcome: str = "not_invoked"
    scheduler_call_id: str = ""
    invalid_output: bool = False
    recoverable_invocation_error: bool = False
    # Kept in the legacy runner's terminology so the adapter can propagate a
    # horizon-truncated scheduler result without manufacturing a fallback.
    agent_run_horizon_reached: bool = False
    run_horizon_reached: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_id",
            _bounded_text("decision_id", self.decision_id, reject_controls=True),
        )
        object.__setattr__(
            self,
            "state_id",
            _bounded_text("state_id", self.state_id, reject_controls=True),
        )
        object.__setattr__(
            self,
            "decision_index",
            _nonnegative_int("decision_index", self.decision_index),
        )
        object.__setattr__(
            self,
            "policy",
            _bounded_text("policy", self.policy, reject_controls=True),
        )
        object.__setattr__(
            self,
            "selected_task_id",
            _bounded_text(
                "selected_task_id",
                self.selected_task_id,
                allow_empty=True,
                reject_controls=True,
            ),
        )
        object.__setattr__(
            self,
            "reason",
            _bounded_text(
                "reason",
                self.reason,
                max_chars=MAX_SCHEDULER_REASON_CHARS,
                max_bytes=MAX_SCHEDULER_REASON_BYTES,
                reject_controls=True,
            ),
        )
        object.__setattr__(self, "scores", _immutable_scores(self.scores))
        object.__setattr__(self, "task_scores", _immutable_scores(self.task_scores))
        object.__setattr__(self, "trace_increments", _immutable_scores(self.trace_increments))
        object.__setattr__(
            self,
            "trace_reference_ids",
            _bounded_identifier_sequence(
                "trace_reference_ids",
                self.trace_reference_ids,
                max_items=20,
            ),
        )
        if not isinstance(self.fallback, bool):
            raise ValueError("fallback must be a boolean")
        object.__setattr__(
            self,
            "fallback_reason",
            _bounded_text(
                "fallback_reason",
                self.fallback_reason,
                allow_empty=True,
                max_chars=MAX_SCHEDULER_REASON_CHARS,
                max_bytes=MAX_SCHEDULER_REASON_BYTES,
                reject_controls=True,
            ),
        )
        if self.scheduler_cost is not None and not isinstance(
            self.scheduler_cost, LLMSchedulerCost
        ):
            raise ValueError("scheduler_cost must be LLMSchedulerCost or None")
        if self.scheduler_outcome not in SCHEDULER_OUTCOMES:
            raise ValueError("scheduler_outcome is not recognized")
        for name in (
            "invalid_output",
            "recoverable_invocation_error",
            "agent_run_horizon_reached",
            "run_horizon_reached",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        horizon = self.agent_run_horizon_reached or self.run_horizon_reached
        object.__setattr__(self, "agent_run_horizon_reached", horizon)
        object.__setattr__(self, "run_horizon_reached", horizon)
        # A charged scheduler invocation is keyed by the immutable decision
        # identity. Validate the call ID before it enters any lifecycle
        # artifact; arbitrary aliases would let one invocation masquerade as
        # another while still passing cardinality joins.
        scheduler_call_id = _bounded_text(
            "scheduler_call_id",
            self.scheduler_call_id,
            allow_empty=True,
            reject_controls=True,
        )
        object.__setattr__(self, "scheduler_call_id", scheduler_call_id)
        if self.scheduler_cost is not None and not self.scheduler_call_id:
            object.__setattr__(self, "scheduler_call_id", self.decision_id)
        # Keep the outcome fields as one closed, symmetric state machine.  A
        # decision artifact is consumed independently of the in-memory
        # object, so checking only ``flag -> outcome`` permits a forged row
        # such as ``outcome=provider_error, recoverable=false`` to pass core
        # validation and be miscounted at closeout.  The inverse implications
        # below make every representable outcome unambiguous and keep the
        # cost/call/fallback lifecycle tied to the invocation itself.
        costed = self.scheduler_cost is not None
        if costed and not self.scheduler_call_id:
            # The default above normally supplies the decision ID.  Retain an
            # explicit guard for callers that pass a non-string falsey value.
            raise ValueError("costed scheduler call requires scheduler_call_id")
        if costed and self.scheduler_call_id != self.decision_id:
            raise ValueError("scheduler_call_id must equal decision_id")
        if not costed and self.scheduler_call_id:
            raise ValueError("scheduler_call_id requires scheduler cost")

        if self.scheduler_outcome == "not_invoked":
            if costed:
                raise ValueError("not_invoked must not have scheduler cost")
            if self.scheduler_call_id:
                raise ValueError("not_invoked must not have scheduler_call_id")
            if self.fallback or self.invalid_output or self.recoverable_invocation_error or horizon:
                raise ValueError(
                    "not_invoked cannot carry fallback, error, or horizon flags"
                )
            return

        if not costed:
            raise ValueError(
                f"{self.scheduler_outcome} requires one costed scheduler call"
            )

        if self.scheduler_outcome == "accepted":
            if self.fallback or self.invalid_output or self.recoverable_invocation_error or horizon:
                raise ValueError(
                    "accepted cannot carry fallback, error, or horizon flags"
                )
            return

        if self.scheduler_outcome == "invalid_output":
            if not self.invalid_output:
                raise ValueError(
                    "scheduler_outcome=invalid_output requires invalid_output=True"
                )
            if self.recoverable_invocation_error or horizon or not self.fallback:
                raise ValueError(
                    "invalid_output requires fallback and no provider or horizon flag"
                )
            return

        if self.scheduler_outcome == "provider_error":
            if not self.recoverable_invocation_error:
                raise ValueError(
                    "scheduler_outcome=provider_error requires "
                    "recoverable_invocation_error=True"
                )
            if self.invalid_output or horizon or not self.fallback:
                raise ValueError(
                    "provider_error requires fallback and no invalid or horizon flag"
                )
            return

        if self.scheduler_outcome == "policy_timeout":
            if self.invalid_output or self.recoverable_invocation_error or horizon or not self.fallback:
                raise ValueError(
                    "policy_timeout requires fallback and no invalid, provider, or horizon flag"
                )
            return

        # The only remaining admitted value is horizon_truncated.  It is a
        # charged invocation that must never be converted into a policy
        # fallback or an assignment after the fixed horizon.
        if self.scheduler_outcome == "horizon_truncated":
            if not horizon:
                raise ValueError(
                    "scheduler_outcome=horizon_truncated requires run_horizon_reached=True"
                )
            if self.invalid_output or self.recoverable_invocation_error or self.fallback:
                raise ValueError(
                    "horizon_truncated requires one costed, non-fallback scheduler call"
                )

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "state_id": self.state_id,
            "decision_index": self.decision_index,
            "policy": self.policy,
            "selected_task_id": self.selected_task_id,
            "reason": self.reason,
            "scores": dict(self.scores),
            "task_scores": dict(self.task_scores),
            "trace_increments": dict(self.trace_increments),
            "trace_reference_ids": list(self.trace_reference_ids),
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
            "agent_run_horizon_reached": self.agent_run_horizon_reached,
            "run_horizon_reached": self.run_horizon_reached,
            "scheduler_call_id": self.scheduler_call_id,
            "scheduler_outcome": self.scheduler_outcome,
            "invalid_output": self.invalid_output,
            "recoverable_invocation_error": self.recoverable_invocation_error,
            "scheduler_cost": self.scheduler_cost.public_dict() if self.scheduler_cost else None,
        }

    as_dict = public_dict


def _highest_score(scores: Mapping[str, float]) -> str:
    return min(scores, key=lambda task_id: (-scores[task_id], task_id)) if scores else ""


def _admission_capacity_available(
    snapshot: AllocationStateSnapshot,
    *,
    allow_owned_scheduler_reservation: bool = False,
) -> bool:
    """Return whether a policy may request one more solver admission.

    ``free_slots`` is physical solver capacity.  The LLM scheduler may use
    ``owned_scheduler_reservation_slots`` when its invocation already holds
    the one reserved slot, but a reservation never extends the fixed horizon.
    Keeping this gate in the pure policy layer prevents callers that bypass the
    runner from manufacturing assignments after capacity or time is exhausted.
    """

    if snapshot.remaining_seconds <= 0.0:
        return False
    if snapshot.free_slots > 0:
        return True
    return bool(
        allow_owned_scheduler_reservation
        and snapshot.owned_scheduler_reservation_slots > 0
    )


class UniformRefillAllocationPolicy:
    """Refill the eligible task with the fewest current active leases."""

    name = POLICY_UNIFORM_REFILL

    def choose(self, snapshot: AllocationStateSnapshot) -> AllocationDecision:
        if not _admission_capacity_available(snapshot):
            return AllocationDecision(
                decision_id=snapshot.decision_id,
                state_id=snapshot.state_id,
                decision_index=snapshot.decision_index,
                policy=self.name,
                selected_task_id="",
                reason="no eligible admission capacity",
            )
        active = {task.task_id: float(task.active_allocations) for task in snapshot.eligible_tasks}
        selected = min(active, key=lambda task_id: (active[task_id], task_id)) if active else ""
        return AllocationDecision(
            decision_id=snapshot.decision_id,
            state_id=snapshot.state_id,
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason="fewest active allocations; task-ID tie break" if selected else "no eligible task",
            scores={task_id: -count for task_id, count in active.items()},
        )


class TaskStateAllocationPolicy:
    name = POLICY_TASK_STATE

    def __init__(self, scorer: TaskStateScorer | None = None) -> None:
        self.scorer = scorer or TaskStateScorer()

    def choose(self, snapshot: AllocationStateSnapshot) -> AllocationDecision:
        if not _admission_capacity_available(snapshot):
            return AllocationDecision(
                decision_id=snapshot.decision_id,
                state_id=snapshot.state_id,
                decision_index=snapshot.decision_index,
                policy=self.name,
                selected_task_id="",
                reason="no eligible admission capacity",
            )
        scores = self.scorer.score_snapshot(snapshot)
        selected = _highest_score(scores)
        return AllocationDecision(
            decision_id=snapshot.decision_id,
            state_id=snapshot.state_id,
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason="highest task-state utility; task-ID tie break" if selected else "no eligible task",
            scores=scores,
            task_scores=scores,
        )


class TraceStateAllocationPolicy:
    name = POLICY_TRACE_STATE

    def __init__(self, scorer: TraceStateScorer | None = None) -> None:
        self.scorer = scorer or TraceStateScorer()

    def choose(self, snapshot: AllocationStateSnapshot) -> AllocationDecision:
        if not _admission_capacity_available(snapshot):
            return AllocationDecision(
                decision_id=snapshot.decision_id,
                state_id=snapshot.state_id,
                decision_index=snapshot.decision_index,
                policy=self.name,
                selected_task_id="",
                reason="no eligible admission capacity",
            )
        task_scores = self.scorer.task_scorer.score_snapshot(snapshot)
        increments = self.scorer.increments(snapshot)
        scores = {task_id: task_scores[task_id] + increments[task_id] for task_id in task_scores}
        selected = _highest_score(scores)
        return AllocationDecision(
            decision_id=snapshot.decision_id,
            state_id=snapshot.state_id,
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason="highest task-plus-trace utility; task-ID tie break" if selected else "no eligible task",
            scores=scores,
            task_scores=task_scores,
            trace_increments=increments,
        )


def parse_llm_scheduler_output(
    raw_output: str,
    snapshot: AllocationStateSnapshot,
) -> tuple[str, str, tuple[str, ...]]:
    """Parse the exact, non-Markdown scheduler wire shape or raise ValueError."""

    if not isinstance(raw_output, str):
        raise ValueError("scheduler output must be a bounded UTF-8 string")
    if len(raw_output) > MAX_SCHEDULER_OUTPUT_CHARS:
        raise ValueError("scheduler output exceeds its bounded size")
    try:
        output_bytes = len(raw_output.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("scheduler output must be a bounded UTF-8 string") from exc
    if output_bytes > MAX_SCHEDULER_OUTPUT_BYTES:
        raise ValueError("scheduler output exceeds its bounded size")

    # The stdlib decoder's recursion behavior varies by Python release.  Do a
    # small, allocation-free scan first so a deeply nested malformed response
    # has one deterministic failure mode instead of relying on RecursionError.
    depth = 0
    in_string = False
    escaped = False
    for character in raw_output:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_SCHEDULER_JSON_DEPTH:
                raise ValueError("scheduler output exceeds the bounded JSON depth")
        elif character in "]}":
            depth = max(0, depth - 1)

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("scheduler JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw_output.strip(),
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("scheduler JSON contains a non-finite constant")
            ),
        )
    # ``object_pairs_hook`` and ``parse_constant`` intentionally raise
    # ``ValueError`` for duplicate keys and non-finite JSON constants.  Keep
    # those wire-level failures inside the parser boundary so the caller can
    # apply its deterministic fallback instead of leaking an exception from
    # an untrusted provider payload.
    except RecursionError as exc:
        raise ValueError("scheduler output exceeds the bounded JSON depth") from exc
    except ValueError as exc:
        # Only retain the two stable hook categories.  JSON decoder diagnostics
        # and provider-controlled source text must not enter fallback artifacts.
        detail = str(exc)
        if detail in {
            "scheduler JSON contains duplicate keys",
            "scheduler JSON contains a non-finite constant",
        }:
            raise ValueError(detail) from exc
        raise ValueError("scheduler output must be exactly one JSON object") from exc
    required = {"decision_id", "task_id", "reason", "trace_reference_ids"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("scheduler JSON must contain exactly decision_id, task_id, reason, trace_reference_ids")
    if payload["decision_id"] != snapshot.decision_id:
        raise ValueError("scheduler decision_id is stale or mismatched")
    task_id = payload["task_id"]
    if not isinstance(task_id, str) or task_id not in snapshot.eligible_task_ids:
        raise ValueError("scheduler task_id is not eligible")
    reason = payload["reason"]
    if not _scheduler_reason_is_safe(reason):
        raise ValueError("scheduler reason is unsafe or exceeds its bound")
    references = payload["trace_reference_ids"]
    if (
        not isinstance(references, list)
        or len(references) > 20
        or any(not isinstance(reference, str) for reference in references)
        or len(set(references)) != len(references)
        or not set(references).issubset(
            next(task for task in snapshot.tasks if task.task_id == task_id).trace_reference_ids
        )
    ):
        raise ValueError("scheduler trace_reference_ids are invalid")
    return task_id, reason.strip(), tuple(references)


LLMSchedulerInvoker = Callable[[AllocationStateSnapshot, str], LLMSchedulerResponse]


class ReadOnlyLLMSchedulerPolicy:
    """Model-selected task with strict output validation and deterministic fallback."""

    name = POLICY_LLM_SCHEDULER

    def __init__(
        self,
        invoke: LLMSchedulerInvoker,
        fallback_policy: TaskStateAllocationPolicy | None = None,
        *,
        trace_weights: TraceScoreWeights = DEFAULT_TRACE_SCORE_WEIGHTS,
        prompt_max_bytes: int | None = None,
        max_prompt_bytes: int | None = None,
        llm_scheduler_prompt_max_bytes: int | None = None,
        prompt_max_tokens: int | None = None,
        max_prompt_tokens: int | None = None,
        llm_scheduler_prompt_max_tokens: int | None = None,
    ) -> None:
        if not callable(invoke):
            raise TypeError("invoke must be callable")
        if not isinstance(trace_weights, TraceScoreWeights):
            raise TypeError("trace_weights must be TraceScoreWeights")
        self._invoke = invoke
        self._fallback = fallback_policy or TaskStateAllocationPolicy()
        # The LLM arm's deterministic fallback is task-state selection, but
        # its auditable task/trace score view must remain the same manifest-
        # selected projection as Trace-State.  Keep that scorer immutable and
        # tied to the fallback policy's task scorer so ordinary weights cannot
        # drift between the two arms.
        self._trace_scorer = TraceStateScorer(self._fallback.scorer, trace_weights)
        self._prompt_max_bytes = _resolve_prompt_bytes(
            prompt_max_bytes=prompt_max_bytes,
            max_prompt_bytes=max_prompt_bytes,
            llm_scheduler_prompt_max_bytes=llm_scheduler_prompt_max_bytes,
        )
        self._prompt_max_tokens = _resolve_prompt_tokens(
            prompt_max_tokens=prompt_max_tokens,
            max_prompt_tokens=max_prompt_tokens,
            llm_scheduler_prompt_max_tokens=llm_scheduler_prompt_max_tokens,
        )

    @property
    def prompt_max_bytes(self) -> int:
        """Manifest-owned UTF-8 byte ceiling for one scheduler prompt."""

        return self._prompt_max_bytes

    @property
    def prompt_max_tokens(self) -> int:
        """Manifest-owned conservative token ceiling for one prompt."""

        return self._prompt_max_tokens

    @staticmethod
    def prompt(
        snapshot: AllocationStateSnapshot,
        *,
        max_bytes: int | None = None,
        prompt_max_bytes: int | None = None,
        max_prompt_bytes: int | None = None,
        llm_scheduler_prompt_max_bytes: int | None = None,
        prompt_max_tokens: int | None = None,
        max_prompt_tokens: int | None = None,
        llm_scheduler_prompt_max_tokens: int | None = None,
    ) -> str:
        """Render a bounded, read-only scheduler prompt.

        The entire UTF-8 prompt is measured.  JSON is never sliced: callers
        receive a ``ValueError`` when the complete causal snapshot cannot fit,
        allowing the policy to record a charged deterministic fallback.
        """

        limit = _resolve_prompt_bytes(
            max_bytes=max_bytes,
            prompt_max_bytes=prompt_max_bytes,
            max_prompt_bytes=max_prompt_bytes,
            llm_scheduler_prompt_max_bytes=llm_scheduler_prompt_max_bytes,
        )
        token_limit = _resolve_prompt_tokens(
            prompt_max_tokens=prompt_max_tokens,
            max_prompt_tokens=max_prompt_tokens,
            llm_scheduler_prompt_max_tokens=llm_scheduler_prompt_max_tokens,
        )
        state = json.dumps(
            _scheduler_state_dict(snapshot),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        rendered = (
            "You are a read-only allocation scheduler. Decide only from SNAPSHOT; do not call "
            "tools or change state. Return exactly one JSON object with keys decision_id, task_id, "
            "reason, trace_reference_ids. task_id must be eligible; trace references must appear in "
            "the snapshot. No Markdown or extra keys.\nSNAPSHOT:\n" + state
        )
        prompt_bytes = len(rendered.encode("utf-8"))
        if prompt_bytes > limit:
            raise _SchedulerPromptError(
                "size_limit",
                f"scheduler prompt exceeds max bytes ({prompt_bytes}>{limit})",
            )
        prompt_tokens = _prompt_token_count(rendered)
        if prompt_tokens > token_limit:
            raise _SchedulerPromptError(
                "token_limit",
                f"scheduler prompt exceeds max tokens ({prompt_tokens}>{token_limit})",
            )
        return rendered

    def _prompt(self, snapshot: AllocationStateSnapshot) -> str:
        return self.prompt(
            snapshot,
            max_bytes=self._prompt_max_bytes,
            max_prompt_tokens=self._prompt_max_tokens,
        )

    @staticmethod
    def _charged_fallback_response(
        *,
        error: str,
        latency_seconds: float = 0.0,
        recoverable_invocation_error: bool = False,
    ) -> LLMSchedulerResponse:
        """Represent a skipped/failed call as one auditable scheduler attempt."""

        # The runner reserves capacity before invoking this policy.  A prompt
        # construction failure still consumes that decision's scheduler call
        # budget, but has no model tokens and no model wall time to charge.
        del error  # The human-readable reason is stored on AllocationDecision.
        return LLMSchedulerResponse(
            "",
            returncode=1,
            latency_seconds=max(0.0, latency_seconds),
            occupied_slot_seconds=max(0.0, latency_seconds),
            recoverable_invocation_error=recoverable_invocation_error,
        )

    def choose(self, snapshot: AllocationStateSnapshot) -> AllocationDecision:
        if not snapshot.eligible_task_ids or not _admission_capacity_available(
            snapshot,
            allow_owned_scheduler_reservation=True,
        ):
            return AllocationDecision(
                decision_id=snapshot.decision_id,
                state_id=snapshot.state_id,
                decision_index=snapshot.decision_index,
                policy=self.name,
                selected_task_id="",
                reason="no eligible admission capacity",
            )
        started = time.monotonic()
        invocation_error = ""
        prompt_rejected = False
        invocation_exception = False
        response: LLMSchedulerResponse
        try:
            prompt = self._prompt(snapshot)
        except _SchedulerPromptError as exc:
            # Snapshot privacy/bounds failures are deterministic candidate
            # outcomes.  Do not leak exception text, paths, or transcripts to
            # artifacts; retain only a stable category.
            invocation_error = f"scheduler prompt rejected: {exc.kind}"
            prompt_rejected = True
            response = self._charged_fallback_response(error=invocation_error)
        except Exception:
            invocation_error = "scheduler prompt rejected: unsafe_snapshot"
            prompt_rejected = True
            response = self._charged_fallback_response(error=invocation_error)
        else:
            try:
                response = self._invoke(snapshot, prompt)
            except Exception as exc:  # provider/coordinator noise is recoverable
                invocation_exception = True
                invocation_error = f"scheduler invocation failed: {type(exc).__name__}"
                response = self._charged_fallback_response(
                    error=invocation_error,
                    latency_seconds=max(0.0, time.monotonic() - started),
                    recoverable_invocation_error=True,
                )
        if not isinstance(response, LLMSchedulerResponse):
            invocation_exception = True
            invocation_error = "scheduler invocation returned invalid response"
            response = self._charged_fallback_response(
                error=invocation_error,
                latency_seconds=max(0.0, time.monotonic() - started),
                recoverable_invocation_error=True,
            )
        # A call that crossed the fixed experiment horizon is a lifecycle
        # truncation, not a malformed model decision.  Preserve its one-call
        # cost, but do not invoke the deterministic fallback: the runner will
        # release the reservation and record ``not_admitted_horizon``.
        if response.run_horizon_reached:
            return AllocationDecision(
                decision_id=snapshot.decision_id,
                state_id=snapshot.state_id,
                decision_index=snapshot.decision_index,
                policy=self.name,
                selected_task_id="",
                reason="scheduler call was truncated by the fixed run horizon",
                scheduler_cost=response.cost,
                scheduler_outcome="horizon_truncated",
                agent_run_horizon_reached=True,
                run_horizon_reached=True,
            )
        error = invocation_error
        selected = ""
        reason = ""
        references: tuple[str, ...] = ()
        if not error and response.recoverable_invocation_error:
            error = "scheduler invocation failed"
        if not error:
            if response.timed_out:
                error = "scheduler timed out"
            elif response.returncode != 0:
                error = f"scheduler returned {response.returncode}"
            else:
                try:
                    selected, reason, references = parse_llm_scheduler_output(
                        response.output, snapshot
                    )
                except ValueError as exc:
                    error = str(exc)
        outcome = "accepted"
        invalid_output = False
        recoverable_error = bool(response.recoverable_invocation_error)
        if error:
            # The runner acquires the scheduler's physical reservation before
            # invoking this policy.  Consequently ``snapshot.free_slots`` is
            # often zero while ``owned_scheduler_reservation_slots`` is one.
            # Calling the ordinary Task-State policy would reject the fallback
            # on its capacity gate and return an empty task, causing the runner
            # to spin through stale decisions until the horizon.  A fallback is
            # still a Task-State choice: score the immutable snapshot directly
            # while retaining the caller-owned reservation for admission.
            fallback_scores = self._fallback.scorer.score_snapshot(snapshot)
            selected = _highest_score(fallback_scores)
            reason = "scheduler decision rejected; deterministic task-state fallback"
            if recoverable_error or invocation_exception:
                outcome = "provider_error"
                recoverable_error = True
            elif response.returncode != 0 and not response.timed_out and not prompt_rejected:
                outcome = "provider_error"
                recoverable_error = True
            elif response.timed_out:
                outcome = "policy_timeout"
            else:
                outcome = "invalid_output"
                invalid_output = True
        task_scores = self._trace_scorer.task_scorer.score_snapshot(snapshot)
        trace_increments = self._trace_scorer.increments(snapshot)
        scores = {
            task_id: task_scores[task_id] + trace_increments[task_id]
            for task_id in task_scores
        }
        return AllocationDecision(
            decision_id=snapshot.decision_id,
            state_id=snapshot.state_id,
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason=reason,
            scores=scores,
            task_scores=task_scores,
            trace_increments=trace_increments,
            trace_reference_ids=references,
            fallback=bool(error),
            fallback_reason=error,
            scheduler_cost=response.cost,
            scheduler_outcome=outcome,
            invalid_output=invalid_output,
            recoverable_invocation_error=recoverable_error,
        )


ALLOCATION_POLICY_REGISTRY: Mapping[str, type[Any]] = MappingProxyType(
    {
        POLICY_UNIFORM_REFILL: UniformRefillAllocationPolicy,
        POLICY_TASK_STATE: TaskStateAllocationPolicy,
        POLICY_TRACE_STATE: TraceStateAllocationPolicy,
        POLICY_LLM_SCHEDULER: ReadOnlyLLMSchedulerPolicy,
    }
)


def create_allocation_policy(
    policy: str,
    *,
    task_weights: TaskScoreWeights = DEFAULT_TASK_SCORE_WEIGHTS,
    trace_weights: TraceScoreWeights = DEFAULT_TRACE_SCORE_WEIGHTS,
    llm_invoker: LLMSchedulerInvoker | None = None,
    prompt_max_bytes: int | None = None,
    max_prompt_bytes: int | None = None,
    llm_scheduler_prompt_max_bytes: int | None = None,
    prompt_max_tokens: int | None = None,
    max_prompt_tokens: int | None = None,
    llm_scheduler_prompt_max_tokens: int | None = None,
) -> Any:
    """Construct one registered policy from explicit manifest-owned settings."""

    name = str(policy).strip().lower()
    if name == POLICY_UNIFORM_REFILL:
        return UniformRefillAllocationPolicy()
    task_scorer = TaskStateScorer(task_weights)
    if name == POLICY_TASK_STATE:
        return TaskStateAllocationPolicy(task_scorer)
    if name == POLICY_TRACE_STATE:
        return TraceStateAllocationPolicy(TraceStateScorer(task_scorer, trace_weights))
    if name == POLICY_LLM_SCHEDULER:
        if llm_invoker is None:
            raise ValueError("llm_scheduler requires llm_invoker")
        return ReadOnlyLLMSchedulerPolicy(
            llm_invoker,
            TaskStateAllocationPolicy(task_scorer),
            trace_weights=trace_weights,
            prompt_max_bytes=prompt_max_bytes,
            max_prompt_bytes=max_prompt_bytes,
            llm_scheduler_prompt_max_bytes=llm_scheduler_prompt_max_bytes,
            prompt_max_tokens=prompt_max_tokens,
            max_prompt_tokens=max_prompt_tokens,
            llm_scheduler_prompt_max_tokens=llm_scheduler_prompt_max_tokens,
        )
    raise ValueError(f"unknown allocation policy: {policy}")


__all__ = [
    "ALLOCATION_POLICY_REGISTRY",
    "DEFAULT_TASK_SCORE_WEIGHTS",
    "DEFAULT_TRACE_SCORE_WEIGHTS",
    "LLM_SCHEDULER_PROMPT_MAX_BYTES",
    "LLM_SCHEDULER_PROMPT_MAX_TOKENS",
    "POLICY_LLM_SCHEDULER",
    "POLICY_TASK_STATE",
    "POLICY_TRACE_STATE",
    "POLICY_UNIFORM_REFILL",
    "AllocationDecision",
    "AllocationStateSnapshot",
    "LLMSchedulerCost",
    "LLMSchedulerInvoker",
    "LLMSchedulerResponse",
    "ReadOnlyLLMSchedulerPolicy",
    "TaskScoreWeights",
    "TaskState",
    "TaskStateAllocationPolicy",
    "TaskStateScorer",
    "TraceFeatures",
    "TraceScoreWeights",
    "TraceStateAllocationPolicy",
    "TraceStateScorer",
    "UniformRefillAllocationPolicy",
    "create_allocation_policy",
    "parse_llm_scheduler_output",
]
