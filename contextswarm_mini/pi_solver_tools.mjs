// Controlled solver capabilities.  This extension deliberately exposes no
// arbitrary process or network primitive: every dynamic operation is forwarded
// to the runner-owned, token-bound loopback broker.
import { existsSync, lstatSync, realpathSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";

const objectSchema = (properties, required = []) => ({
  type: "object",
  properties,
  required,
  additionalProperties: false,
});

const stringSchema = (description, maxLength) => ({ type: "string", description, maxLength });
const integerSchema = (description, maximum = 8) => ({
  type: "integer",
  description,
  minimum: 1,
  maximum,
});

function brokerBaseUrl() {
  const raw = String(process.env.CONTEXTSWARM_JUDGE_URL ?? "").trim();
  if (!raw) throw new Error("The controlled experiment broker is unavailable.");
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("The controlled experiment broker configuration is invalid.");
  }
  if (
    parsed.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("The controlled experiment broker configuration is invalid.");
  }
  return raw.replace(/\/+$/, "");
}

async function brokerCall(operation, payload, signal) {
  const controller = new AbortController();
  const rawDeadline = Number(process.env.CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS ?? "");
  const timeoutMs = Number.isFinite(rawDeadline) && rawDeadline > 0
    ? Math.min(2_147_000_000, Math.max(1_000, rawDeadline - Date.now() + 10_000))
    : null;
  // The broker and evaluator stop at the runner-owned absolute deadline.  The
  // client grace prevents a fixed local timer from cancelling a legitimate
  // gate wait or Judge Retry-After first; Pi's parent signal remains the final
  // cancellation authority when no broker deadline is present.
  const timeout = timeoutMs === null ? null : setTimeout(() => controller.abort(), timeoutMs);
  const abortFromParent = () => controller.abort();
  signal?.addEventListener("abort", abortFromParent, { once: true });
  try {
    const response = await fetch(`${brokerBaseUrl()}/${operation}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload ?? {}),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error("The controlled experiment broker rejected this capability call.");
    }
    const result = await response.json();
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      throw new Error("The controlled experiment broker returned an invalid response.");
    }
    return result;
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error("The controlled experiment broker call was cancelled or timed out.");
    }
    if (error instanceof Error && error.message.startsWith("The controlled")) throw error;
    throw new Error("The controlled experiment broker call failed.");
  } finally {
    if (timeout !== null) clearTimeout(timeout);
    signal?.removeEventListener("abort", abortFromParent);
  }
}

function toolResult(payload) {
  let text = JSON.stringify(payload, null, 2);
  if (text.length > 64_000) {
    text = `${text.slice(0, 64_000)}\n[controlled tool output truncated]`;
  }
  return {
    content: [{ type: "text", text }],
    details: {
      status: typeof payload?.status === "string" ? payload.status : undefined,
      ok: payload?.ok === true,
    },
  };
}

function registerBrokerTool(pi, definition, hooks = {}) {
  pi.registerTool({
    ...definition,
    executionMode: "sequential",
    async execute(_toolCallId, params, signal) {
      try {
        const result = await brokerCall(definition.name, params, signal);
        hooks.onResult?.(result, params);
        return toolResult(result);
      } catch (error) {
        const fallback = hooks.onError?.(error, params);
        if (fallback !== undefined) return toolResult(fallback);
        throw error;
      }
    },
  });
}

function enabledCapability(name, defaultValue = false) {
  const raw = String(process.env[name] ?? "").trim().toLowerCase();
  if (!raw) return defaultValue;
  return ["1", "true", "yes", "on"].includes(raw);
}

function activityFeedbackEnabled() {
  return enabledCapability("CONTEXTSWARM_CPS_ACTIVITY_FEEDBACK_ENABLED");
}

function routeClaimCapabilityEnabled() {
  return (
    enabledCapability("CONTEXTSWARM_CPS_ROUTE_CLAIM_REQUIRED") ||
    enabledCapability("CONTEXTSWARM_CPS_ROUTE_CLAIMS") ||
    enabledCapability("CONTEXTSWARM_ROUTE_CLAIM_REQUIRED") ||
    activityFeedbackEnabled()
  );
}

function routeClaimSurfaceEnabled() {
  return (
    routeClaimCapabilityEnabled() ||
    activityFeedbackEnabled() ||
    enabledCapability("CONTEXTSWARM_CPS_ROUTE_CLAIMS_ENABLED") ||
    enabledCapability("CONTEXTSWARM_CPS_ACTIVE_ROSTER_ENABLED")
  );
}

function routeClaimTtlSeconds() {
  const raw = Number(process.env.CONTEXTSWARM_CPS_ROUTE_CLAIM_TTL_SECONDS ?? "900");
  if (!Number.isInteger(raw) || raw < 1 || raw > 86_400) return 900;
  return raw;
}

function normalizeRouteBypassReason(value) {
  const normalized = String(value ?? "unavailable").trim().toLowerCase();
  return ["unavailable", "error", "expired", "cancelled"].includes(normalized)
    ? normalized
    : "unavailable";
}

function routeClaimBypassResult(operation) {
  return {
    ok: false,
    accepted: false,
    bypassed: true,
    acquired: false,
    claimed: false,
    status: "route_claim_bypassed",
    operation,
    route_claim_bypass_reason: "unavailable",
  };
}

function routeClaimRow(result) {
  if (result?.claim && typeof result.claim === "object") return result.claim;
  if (typeof result?.claim_id === "string" && result.claim_id.trim()) return result;
  return null;
}

function routeClaimRowId(row) {
  const value = row?.claim_id;
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function routeClaimRowIsActive(row) {
  if (!row || typeof row !== "object") return false;
  const status = String(row.status ?? "").trim().toLowerCase();
  // A blocked lease remains visible to peers but cannot satisfy the worker
  // write gate.  Require the canonical active status and reject stale
  // ``active=true`` echoes for terminal/blocked rows.
  return status === "active" && row.active === true;
}

const routeClaimNegativeStatuses = new Set([
  "conflict",
  "route_conflict",
  "not_admitted",
  "actor_not_admitted",
  "invalid_actor_status",
  "actor_finished",
  "episode_mismatch",
  "not_found",
  "claim_terminal",
  "not_owner",
  "invalid_request",
  "invalid_task_selection",
  "expired",
  "released",
  "done",
  "closed",
  "finished",
]);

const routeClaimErrorStatuses = new Set([
  "route_claim_error",
  "broker_error",
  "invalid_response",
  "malformed",
  "error",
  "failed",
  "failure",
  "unavailable",
  "timeout",
  "timed_out",
  "cancelled",
  "canceled",
]);

const routeClaimKnownStatuses = new Set([
  "ok",
  "active",
  "independent_verification",
  "blocked",
  "done",
  "released",
  "expired",
  "conflict",
  "route_conflict",
  "not_admitted",
  "actor_not_admitted",
  "actor_finished",
  "episode_mismatch",
  "not_found",
  "claim_terminal",
  "not_owner",
  "invalid_request",
  "invalid_task_selection",
  "invalid_actor_status",
  "closed",
  "finished",
  "route_claim_bypass",
  "route_claim_bypassed",
  ...routeClaimErrorStatuses,
]);

function routeClaimSemanticNegative(result) {
  if (!result || typeof result !== "object") return false;
  return [result.status, result.error, result.reason].some((value) => {
    const normalized = String(value ?? "").trim().toLowerCase();
    return routeClaimNegativeStatuses.has(normalized);
  });
}

function routeClaimTransportError(result) {
  if (!result || typeof result !== "object") return true;
  return [result.status, result.error, result.reason].some((value) => {
    const normalized = String(value ?? "").trim().toLowerCase();
    return routeClaimErrorStatuses.has(normalized);
  });
}

function routeClaimStatusMalformed(result) {
  const status = String(result?.status ?? "").trim().toLowerCase();
  return Boolean(status) && !routeClaimKnownStatuses.has(status);
}

function routeClaimEnvelopeMalformed(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return true;
  const booleanFields = [
    "ok",
    "accepted",
    "acquired",
    "claimed",
    "idempotent",
    "bypassed",
    "independent_verification_accepted",
  ];
  if (booleanFields.some((key) => key in result && typeof result[key] !== "boolean")) {
    return true;
  }
  for (const key of ["status", "error", "reason", "route_claim_bypass_reason"]) {
    if (key in result && result[key] !== null && typeof result[key] !== "string") {
      return true;
    }
  }
  if (
    typeof result.acquired === "boolean" &&
    typeof result.claimed === "boolean" &&
    result.acquired !== result.claimed
  ) {
    return true;
  }
  const positive = ["acquired", "claimed", "independent_verification_accepted"];
  if (result.ok === false && positive.some((key) => result[key] === true)) return true;
  if (result.accepted === false && positive.some((key) => result[key] === true)) return true;
  // ``conflict`` may be omitted/false or carry a complete owner row. A bare
  // true bit has no owner identity and cannot be used as independent-claim
  // evidence.
  if (result.conflict === true) return true;
  if (
    result.conflict !== undefined &&
    result.conflict !== null &&
    result.conflict !== false &&
    (typeof result.conflict !== "object" || Array.isArray(result.conflict))
  ) return true;
  return false;
}

function routeClaimUnknownDiagnostic(result) {
  if (!result || typeof result !== "object") return true;
  const known = new Set([...routeClaimNegativeStatuses, ...routeClaimErrorStatuses]);
  return [result.error, result.reason].some((value) => {
    const normalized = String(value ?? "").trim().toLowerCase();
    return normalized && !known.has(normalized);
  });
}

function routeClaimPrimaryMarker(row) {
  if (!row || typeof row !== "object") return null;
  const values = [];
  for (const key of ["is_primary", "primary"]) {
    if (!(key in row)) continue;
    if (typeof row[key] !== "boolean") return null;
    values.push(row[key]);
  }
  if (!values.length || values.some((value) => value !== values[0])) return null;
  return values[0];
}

function routeClaimRowMatchesSession(row, expected = {}) {
  if (!row || typeof row !== "object") return false;
  const taskId = String(process.env.CONTEXTSWARM_TASK_ID ?? "").trim();
  const actorId = String(process.env.CONTEXTSWARM_ACTOR_ID ?? "").trim();
  // A route row is a session-bound capability.  If the runner failed to
  // inject either identity, do not let a response with only a matching route
  // key satisfy the local write gate.
  if (!taskId || !actorId) return false;
  if (typeof row.task_id !== "string" || !row.task_id.trim()) return false;
  if (typeof row.actor_id !== "string" || !row.actor_id.trim()) return false;
  // IDs are runner-issued opaque bindings. Do not normalize whitespace on the
  // row side: a padded value is a different (and potentially forged) identity
  // and must not pass the local gate merely because it trims to the session id.
  if (row.task_id !== taskId) return false;
  if (row.actor_id !== actorId) return false;
  const expectedTask = expected.task_id === undefined ? taskId : String(expected.task_id ?? "").trim();
  const expectedActor = expected.actor_id === undefined ? actorId : String(expected.actor_id ?? "").trim();
  if (expectedTask && row.task_id !== expectedTask) return false;
  if (expectedActor && row.actor_id !== expectedActor) return false;
  if (expected.route_key !== undefined) {
    if (typeof row.route_key !== "string" || row.route_key !== String(expected.route_key)) return false;
  }
  if (expected.claim_id !== undefined) {
    if (typeof row.claim_id !== "string" || row.claim_id !== String(expected.claim_id)) return false;
  }
  const episodeRaw = String(process.env.CONTEXTSWARM_EPISODE ?? "").trim();
  if (episodeRaw) {
    if (!/^\d+$/.test(episodeRaw) || !Number.isSafeInteger(Number(episodeRaw))) return false;
    if (!Number.isInteger(row.episode) || row.episode !== Number(episodeRaw)) return false;
  }
  if (expected.episode !== undefined) {
    if (!Number.isInteger(row.episode) || row.episode !== Number(expected.episode)) return false;
  }
  return true;
}

function normalizeExistingPath(rawPath, cwd) {
  if (typeof rawPath !== "string" || !rawPath.trim()) return null;
  const lexical = isAbsolute(rawPath) ? resolve(rawPath) : resolve(cwd, rawPath);
  if (!existsSync(lexical)) return null;
  try {
    if (lstatSync(lexical).isSymbolicLink()) return null;
    return realpathSync(lexical);
  } catch {
    return null;
  }
}

function relativeInside(path, cwd) {
  const rel = relative(cwd, path);
  if (!rel || rel === ".") return ".";
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) return null;
  return rel.split(sep).join("/");
}

// The runner binds the candidate filename per worker session.  Keep the
// historical formal default and reject every other spelling so task data cannot
// widen this capability into an arbitrary path.
function candidateFilename() {
  const configured = String(process.env.CONTEXTSWARM_CANDIDATE_FILENAME ?? "").trim();
  return configured === "result.cpp" || configured === "result.lean"
    ? configured
    : "result.lean";
}

function candidateExtension() {
  return candidateFilename() === "result.cpp" ? "cpp" : "lean";
}

function escapedCandidateFilename() {
  return candidateFilename().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function isReadableFile(rel) {
  const candidate = candidateFilename();
  const candidatePattern = escapedCandidateFilename();
  const extension = candidateExtension();
  return (
    ["problem.md", candidate, "metadata.json", "PUBLIC_FILES.md"].includes(rel) ||
    new RegExp(`^baseline/[^/]+\\.${extension}$`).test(rel) ||
    new RegExp(`^tasks/[^/]+/(?:problem\\.md|${candidatePattern}|metadata\\.json|PUBLIC_FILES\\.md)$`).test(rel) ||
    new RegExp(`^tasks/[^/]+/baseline/[^/]+\\.${extension}$`).test(rel)
  );
}

function isWritableCandidate(rel) {
  const candidate = candidateFilename();
  return rel === candidate || new RegExp(`^tasks/[^/]+/${escapedCandidateFilename()}$`).test(rel);
}

// Unlike read/search paths, the final candidate may not exist yet (the write
// tool is allowed to create result.lean).  Resolve the path lexically, but
// inspect every existing component with lstat before allowing a write/edit.
// Otherwise a pre-existing result.lean symlink (or a symlinked task directory)
// could make an apparently in-workspace write modify an arbitrary host file.
function writableRelative(rawPath, cwd) {
  if (typeof rawPath !== "string" || !rawPath.trim()) return null;
  const lexical = isAbsolute(rawPath) ? resolve(rawPath) : resolve(cwd, rawPath);
  const rel = relativeInside(lexical, cwd);
  if (!rel || !isWritableCandidate(rel)) return null;
  const parts = rel.split("/");
  let current = cwd;
  for (let index = 0; index < parts.length; index += 1) {
    current = resolve(current, parts[index]);
    try {
      if (lstatSync(current).isSymbolicLink()) return null;
    } catch (error) {
      // A missing final result.lean is valid, but a missing parent directory
      // (or any non-ENOENT lookup failure) must not be accepted because the
      // tool could otherwise operate on an unexpected path after the error is
      // resolved.  In particular, ENOTDIR at the final component means a
      // parent is a regular file, not that result.lean is safely absent.
      if (index !== parts.length - 1 || error?.code !== "ENOENT") return null;
    }
  }
  return rel;
}

function isSafeSearchDirectory(rel) {
  return (
    rel === "baseline" ||
    rel === "tasks" ||
    /^tasks\/[^/]+$/.test(rel) ||
    /^tasks\/[^/]+\/baseline$/.test(rel)
  );
}

function guardedRelative(rawPath, ctx) {
  const configured = String(process.env.CONTEXTSWARM_WORKDIR ?? "").trim();
  let cwd;
  try {
    cwd = realpathSync(configured || ctx.cwd);
  } catch {
    return null;
  }
  const target = normalizeExistingPath(rawPath, cwd);
  return target ? relativeInside(target, cwd) : null;
}

function boundedShellTokens(command) {
  if (typeof command !== "string" || command.length < 1 || command.length > 16_000) return null;
  const tokens = [];
  let token = "";
  let quote = null;
  let tokenStarted = false;
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index];
    if (char === "\0" || char === "\n" || char === "\r") return null;
    if (quote === "'") {
      if (char === "'") quote = null;
      else token += char;
      tokenStarted = true;
      continue;
    }
    if (quote === '"') {
      if (char === '"') {
        quote = null;
      } else {
        // Double-quoted shell text still performs substitutions and escapes.
        if (char === "$" || char === "`" || char === "\\") return null;
        token += char;
      }
      tokenStarted = true;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      tokenStarted = true;
      continue;
    }
    if (/\s/.test(char)) {
      if (tokenStarted) tokens.push(token);
      token = "";
      tokenStarted = false;
      continue;
    }
    // Reject every shell control, substitution, redirection, globbing, and
    // expansion character. Quoted Lean snippets remain ordinary argv text.
    if (/[;&|<>`$\\*?\[\](){}#]/.test(char)) return null;
    token += char;
    tokenStarted = true;
  }
  if (quote !== null) return null;
  if (tokenStarted) tokens.push(token);
  if (tokens.length < 1 || tokens.length > 80 || tokens.some((value) => value.length > 8_000)) {
    return null;
  }
  return tokens;
}

function formalHelperRelative(rawTarget, ctx) {
  const configured = String(process.env.CONTEXTSWARM_WORKDIR ?? "").trim();
  let cwd;
  try {
    cwd = realpathSync(configured || ctx.cwd);
  } catch {
    return null;
  }
  const target = normalizeExistingPath(rawTarget, cwd);
  return target ? relativeInside(target, cwd) : null;
}

function isAllowedFormalCommand(command, ctx) {
  // Coding workers never receive the formal helper capability, and must not
  // be able to reach it even if a tool-call event is forged locally.
  if (candidateFilename() !== "result.lean") return false;
  const tokens = boundedShellTokens(command);
  if (!tokens) return false;
  const mode = String(process.env.CONTEXTSWARM_EXPERIMENT_MODE ?? "").trim().toLowerCase();
  if (tokens[0] === "python3") {
    // ``python3`` is intentionally a short spelling in the public helper
    // contract, so bind its resolution to the supervisor's fixed PATH.  A
    // worker-controlled PATH (or a same-named executable in the workspace)
    // must not turn this into arbitrary code execution.
    if (process.env.PATH !== "/usr/local/bin:/usr/bin:/bin" || tokens.length !== 2) return false;
    const rel = formalHelperRelative(tokens[1], ctx);
    return rel === "evaluate.py" || (mode === "mono" && /^tasks\/[^/]+\/evaluate\.py$/.test(rel ?? ""));
  }
  // A slash is required so the shell executes the exact path we validated,
  // rather than resolving a same-named executable from PATH afterward.
  if (!tokens[0].includes("/")) return false;
  const rel = formalHelperRelative(tokens[0], ctx);
  return rel === "formal_query" || (mode === "mono" && /^tasks\/[^/]+\/formal_query$/.test(rel ?? ""));
}

function installPathGuard(pi, routeState) {
  pi.on("tool_call", (event, ctx) => {
    const input = event?.input && typeof event.input === "object" ? event.input : {};
    if (event.toolName === "read") {
      const rel = guardedRelative(input.path, ctx);
      if (!rel || !isReadableFile(rel)) {
        return { block: true, reason: "read is restricted to assigned public task files" };
      }
      return;
    }
    if (event.toolName === "write" || event.toolName === "edit") {
      routeState.refresh();
      if (
        routeState.required &&
        !routeState.satisfied &&
        !routeState.bypassReason
      ) {
        return {
          block: true,
          reason:
            "write/edit requires cps_claim_route success or an explicit " +
            "independent_verification_reason before candidate changes",
        };
      }
      const configured = String(process.env.CONTEXTSWARM_WORKDIR ?? "").trim();
      let cwd;
      try {
        cwd = realpathSync(configured || ctx.cwd);
      } catch {
        return { block: true, reason: "assigned workspace is unavailable" };
      }
      const rel = writableRelative(input.path, cwd);
      if (!rel) {
        return {
          block: true,
          reason: `write/edit is restricted to assigned ${candidateFilename()}`,
        };
      }
      return;
    }
    if (event.toolName === "grep") {
      const rel = guardedRelative(input.path ?? "", ctx);
      const safeGlob =
        input.glob === undefined ||
        (typeof input.glob === "string" && !/[\\/]/.test(input.glob) && !input.glob.includes(".."));
      if (!rel || (!isReadableFile(rel) && !isSafeSearchDirectory(rel)) || !safeGlob) {
        return { block: true, reason: "grep requires an explicit assigned task file or safe task directory" };
      }
      return;
    }
    if (event.toolName === "find") {
      const rel = guardedRelative(input.path ?? "", ctx);
      const safePattern =
        typeof input.pattern === "string" &&
        !/[\\/]/.test(input.pattern) &&
        !input.pattern.includes("..");
      if (!rel || !isSafeSearchDirectory(rel) || !safePattern) {
        return { block: true, reason: "find is restricted to safe assigned task directories" };
      }
      return;
    }
    if (event.toolName === "ls") {
      const rel = guardedRelative(input.path ?? "", ctx);
      if (!rel || !isSafeSearchDirectory(rel)) {
        return { block: true, reason: "ls is restricted to safe assigned task directories" };
      }
      return;
    }
    if (event.toolName === "bash") {
      const command = typeof input.command === "string" ? input.command : input.cmd;
      if (!isAllowedFormalCommand(command, ctx)) {
        return {
          block: true,
          reason: "bash is restricted to the staged formal helper commands",
        };
      }
      const configuredTimeout = Number(
        process.env.CONTEXTSWARM_FORMAL_COMMAND_TIMEOUT_SECONDS ?? "420",
      );
      input.timeout = Number.isFinite(configuredTimeout)
        ? Math.max(1, Math.min(3_600, Math.trunc(configuredTimeout)))
        : 420;
    }
  });
}

export default function registerContextSwarmSolverTools(pi) {
  const routeState = {
    required: routeClaimCapabilityEnabled(),
    satisfied: false,
    // The runner may already know that actor admission/roster persistence was
    // unavailable.  Seed the explicit fail-open marker before the first tool
    // call so a solver cannot get stuck behind the write gate while the broker
    // is correctly bypassed.
    bypassReason: ["unavailable", "error", "expired", "cancelled"].includes(
      String(process.env.CONTEXTSWARM_CPS_ROUTE_CLAIM_BYPASS_REASON ?? "").trim().toLowerCase(),
    )
      ? String(process.env.CONTEXTSWARM_CPS_ROUTE_CLAIM_BYPASS_REASON).trim().toLowerCase()
      : "",
    claims: new Map(),
    clear(claimId) {
      if (claimId) this.claims.delete(claimId);
      else this.claims.clear();
      this.refresh();
    },
    clearRoute(routeKey) {
      const canonical = typeof routeKey === "string" ? routeKey.trim() : "";
      if (canonical) {
        for (const [claimId, row] of this.claims.entries()) {
          if (row?.route_key === canonical) this.claims.delete(claimId);
        }
      }
      this.refresh();
    },
    remember(row) {
      const claimId = routeClaimRowId(row);
      if (!claimId || !routeClaimRowIsActive(row)) return;
      const copy = { ...row };
      // Older adapters may omit the lease timestamp. Bound their local
      // compatibility window to the manifest TTL instead of making the write
      // gate immortal.
      if (typeof copy.expires_at !== "string" || !copy.expires_at.trim()) {
        copy.expires_at = new Date(
          Date.now() + routeClaimTtlSeconds() * 1_000,
        ).toISOString();
      }
      this.claims.set(claimId, copy);
    },
    refresh() {
      let active = false;
      for (const [claimId, row] of this.claims.entries()) {
        if (!routeClaimRowIsActive(row)) {
          this.claims.delete(claimId);
          continue;
        }
        const expiresAt = row?.expires_at;
        if (typeof expiresAt === "string" && expiresAt.trim()) {
          const expiry = Date.parse(expiresAt);
          // A malformed lease is fail-closed locally. The solver can obtain a
          // fresh claim/update response, while a stale response cannot keep
          // the write gate open indefinitely.
          if (!Number.isFinite(expiry) || expiry <= Date.now()) {
            this.claims.delete(claimId);
            continue;
          }
        }
        active = true;
      }
      this.satisfied = active;
    },
  };
  installPathGuard(pi, routeState);

  const candidate = candidateFilename();
  const language = candidate === "result.cpp" ? "C++" : "Lean";
  // Direct messaging was part of the original CPS contract, so its default
  // remains enabled.  The runner sets these explicit, non-secret capability
  // bits for allocation/selection experiments that must remain message-free.
  const directMessages = enabledCapability("CONTEXTSWARM_CPS_DIRECT_MESSAGES", true);
  const selectionEnabled = enabledCapability("CONTEXTSWARM_CPS_SELECTION_ENABLED");

  registerBrokerTool(pi, {
    name: "judge_check",
    label: "Controlled Judge Check",
    description:
      `Submit the runner-bound ${candidate} to the controlled external ${language} Judge. The task, baseline, environment, profile, endpoint, deadline, and concurrency are fixed by the runner. For a normal single-task worker call with no arguments; Mono must provide task_id.`,
    promptSnippet: `Check the current ${candidate} through the controlled external Judge`,
    promptGuidelines: [
      "Use judge_check one candidate at a time; never attempt local compilation or raw Judge access.",
      "A retryable busy result is not permission to use a local fallback.",
    ],
    parameters: objectSchema({
      task_id: stringSchema("Mono task slug; omit in a single-task worker", 256),
    }),
  });

  registerBrokerTool(pi, {
    name: "cps_search",
    label: "Search Context Pieces",
    description: "Search bounded shared context for this runner-bound task.",
    promptSnippet: "Search shared CPS evidence for the assigned task",
    parameters: objectSchema({
      query: stringSchema("Search terms", 500),
      limit: integerSchema("Maximum returned pieces", 8),
    }),
  });

  registerBrokerTool(pi, {
    name: "cps_publish",
    label: "Publish Context Piece",
    description: "Publish a concise typed handoff to runner-owned CPS state.",
    promptSnippet: "Publish a concise CPS proof handoff",
    parameters: objectSchema(
      {
        kind: stringSchema("Piece type, such as proof_strategy, lemma, blocker, or handoff", 64),
        title: stringSchema("Concise title", 300),
        body: stringSchema("Reusable proof information", 8_000),
        tags: { type: "array", items: stringSchema("Tag", 64), maxItems: 8 },
        scope: { type: "string", enum: ["task", "global"], description: "global is available only in hybrid mode" },
      },
      ["title", "body"],
    ),
  });

  if (directMessages) registerBrokerTool(pi, {
    name: "cps_inbox",
    label: "CPS Inbox",
    description: "Read bounded unacknowledged direct messages for this actor.",
    promptSnippet: "Read direct CPS messages for this actor",
    parameters: objectSchema({ limit: integerSchema("Maximum returned messages", 8) }),
  });

  if (directMessages) registerBrokerTool(pi, {
    name: "cps_send",
    label: "Send CPS Message",
    description: "Send a bounded direct message using the runner-bound actor identity.",
    promptSnippet: "Send a direct CPS handoff",
    parameters: objectSchema(
      {
        recipient: stringSchema("Recipient actor id; omit for a broadcast", 256),
        body: stringSchema("Message body", 8_000),
        scope: { type: "string", enum: ["task", "global"], description: "global is available only in hybrid mode" },
      },
      ["body"],
    ),
  });

  if (routeClaimSurfaceEnabled()) {
    registerBrokerTool(
      pi,
      {
        name: "cps_active_routes",
        label: "List Active Routes",
        description:
          "List currently active peer activity declarations for the assigned task. This is a pre-Judge coordination capability and only returns bounded runner-owned public summaries.",
        promptSnippet: "Inspect active peer routes before choosing a direction",
        parameters: objectSchema({
          task_id: stringSchema("Optional task slug; runner binds and validates this", 256),
          query: stringSchema("Optional bounded route/summary filter", 500),
          limit: {
            type: "integer",
            minimum: 1,
            maximum: 100,
            description: "Maximum returned active routes",
          },
          include_closing: {
            type: "boolean",
            description: "Include actors/claims that are closing but not yet expired",
          },
        }),
      },
      {
        onResult(result) {
          const status = String(result?.status ?? "").trim().toLowerCase();
          const transportError = routeClaimTransportError(result);
          if (
            ["actor_not_admitted", "actor_finished", "episode_mismatch", "invalid_actor_status"].includes(status)
          ) {
            routeState.clear();
          }
          if (
            result?.bypassed === true ||
            ["route_claim_bypassed", "route_claim_bypass"].includes(
              String(result?.status ?? "").toLowerCase(),
            )
          ) {
            routeState.bypassReason = normalizeRouteBypassReason(
              result?.route_claim_bypass_reason,
            );
            routeState.clear();
          } else if (transportError || routeClaimStatusMalformed(result)) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
          }
        },
        onError() {
          routeState.bypassReason = "unavailable";
          routeState.clear();
          return routeClaimBypassResult("cps_active_routes");
        },
      },
    );

    // Claim is the only route operation that can satisfy the pre-edit gate.
    // On a transient CPS/broker failure the treatment is explicitly fail-open,
    // but the returned bounded marker makes that path auditable.
    registerBrokerTool(
      pi,
      {
        name: "cps_claim_route",
        label: "Claim Exploration Route",
        description:
          "Declare this admitted actor's current exploration activity. The summary is one concise sentence visible to concurrent peers; route_key is an opaque technical handle and may overlap another actor's handle in the activity-feedback treatment. An independent-verification reason may explain an intentional repeat.",
        promptSnippet: "Claim or independently verify an exploration route",
        parameters: objectSchema(
          {
            route_key: stringSchema("Opaque technical activity handle (not a semantic uniqueness key in activity-feedback mode)", 512),
            summary: stringSchema("One concise sentence describing what you are currently exploring or testing; visible to peers", 1_000),
            ttl_seconds: {
              type: "integer",
              minimum: 1,
              maximum: 86_400,
              description: "Optional claim lease duration; defaults to the manifest TTL",
            },
            independent_verification_reason: stringSchema(
              "Why deliberately checking a peer's route is independent and useful",
              1_000,
            ),
          },
          ["route_key", "summary"],
        ),
      },
      {
        onResult(result, params) {
          if (routeClaimEnvelopeMalformed(result)) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          const reason = String(params?.independent_verification_reason ?? "").trim();
          const status = String(result?.status ?? "").trim().toLowerCase();
          const bypassed =
            result?.bypassed === true ||
            ["route_claim_bypassed", "route_claim_bypass"].includes(status);
          if (bypassed) {
            routeState.bypassReason = normalizeRouteBypassReason(
              result?.route_claim_bypass_reason,
            );
            routeState.clear();
          }
          const conflict =
            (result?.conflict !== undefined &&
              result?.conflict !== null &&
              result?.conflict !== false) ||
            status === "conflict" ||
            status === "route_conflict";
          const explicitAcquired =
            result?.acquired === true ||
            result?.claimed === true;
          const row = routeClaimRow(result);
          const claimId = String(row?.claim_id ?? result?.claim_id ?? "").trim();
          const claimActive = Boolean(claimId) && routeClaimRowIsActive(row);
          const semanticNegative = routeClaimSemanticNegative(result);
          const transportError = routeClaimTransportError(result);
          const unknownDiagnostic = routeClaimUnknownDiagnostic(result);
          const malformedStatus = routeClaimStatusMalformed(result);
          if (transportError && !bypassed) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          if (
            ["actor_not_admitted", "actor_finished", "episode_mismatch", "invalid_actor_status"].includes(status)
          ) {
            routeState.clear();
          }
          if (unknownDiagnostic && !bypassed) {
            // Treat an unrecognized adapter diagnostic as an unavailable
            // coordination dependency.  This is an explicit fail-open marker
            // rather than an implicit successful claim.
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          if (malformedStatus && !bypassed) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          if (semanticNegative) {
            // A handled negative for a route (conflict, not_found, or an
            // ownership/episode rejection) invalidates any stale local lease
            // for that same canonical route. Keep unrelated route leases
            // intact; an invalid request with no usable route key cannot
            // revoke a different valid lease.
            const requestedRoute = String(params?.route_key ?? "").trim();
            if (
              ["actor_not_admitted", "actor_finished", "episode_mismatch", "invalid_actor_status"].includes(status)
            ) {
              routeState.clear();
            } else if (requestedRoute) {
              routeState.clearRoute(requestedRoute);
            }
            return;
          }
          const conflictRow =
            result?.conflict && typeof result.conflict === "object"
              ? result.conflict
              : null;
          const claimPrimary = routeClaimPrimaryMarker(row);
          const conflictPrimary = routeClaimPrimaryMarker(conflictRow);
          const secondaryResponse = status === "independent_verification";
          const primaryShape =
            claimPrimary !== null &&
            ((conflict || secondaryResponse)
              ? claimPrimary === false &&
                (!conflict || (conflictRow !== null && conflictPrimary === true))
              : claimPrimary === true);
          const positiveAdmission =
            !bypassed &&
            !semanticNegative &&
            !unknownDiagnostic &&
            !malformedStatus &&
            routeClaimRowMatchesSession(row, {
              // The broker trims outer whitespace before persisting the
              // canonical route key. Match that canonical request value
              // locally while keeping the returned row comparison exact.
              route_key: String(params?.route_key ?? "").trim(),
            }) &&
            result?.ok === true &&
            explicitAcquired &&
            claimActive &&
            primaryShape &&
            ["active", "independent_verification"].includes(status);
          const independentlyAccepted =
            positiveAdmission &&
            Boolean(reason) &&
            (result?.independent_verification_accepted === true ||
              status === "independent_verification" ||
              row?.independent_verification_reason === reason ||
              result?.independent_verification_reason === reason);
          // A conflict is only cleared by explicit acceptance evidence.  A
          // request reason or echoed active row alone must not turn a duplicate
          // exploration into a write-enabled claim.
          const acquired =
            positiveAdmission &&
            (!conflict || independentlyAccepted) &&
            !["blocked", "released", "conflict", "route_conflict"].includes(status);
          if (acquired || independentlyAccepted) {
            routeState.remember(row);
          }
          routeState.refresh();
        },
        onError() {
          routeState.bypassReason = "unavailable";
          routeState.clear();
          return routeClaimBypassResult("cps_claim_route");
        },
      },
    );

    registerBrokerTool(
      pi,
      {
        name: "cps_update_route",
        label: "Update Route Claim",
        description: "Update this actor's concise peer-visible activity summary, lifecycle status, independent-verification reason, or TTL.",
        promptSnippet: "Refresh the current route claim",
        parameters: objectSchema({
          claim_id: stringSchema("Runner-issued route claim id", 128),
          status: {
            type: "string",
            enum: ["active", "blocked", "done", "released"],
            description: "New lifecycle status",
          },
          summary: stringSchema("Replacement concise sentence describing current activity", 1_000),
          ttl_seconds: {
            type: "integer",
            minimum: 1,
            maximum: 86_400,
            description: "Replacement lease duration",
          },
          independent_verification_reason: stringSchema(
            "Reason this claim independently verifies a peer route",
            1_000,
          ),
        }, ["claim_id"]),
      },
      {
        onResult(result, params) {
          if (routeClaimEnvelopeMalformed(result)) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          if (
            result?.bypassed === true ||
            ["route_claim_bypassed", "route_claim_bypass"].includes(
              String(result?.status ?? "").toLowerCase(),
            )
          ) {
            routeState.bypassReason = normalizeRouteBypassReason(
              result?.route_claim_bypass_reason,
            );
            routeState.clear();
            return;
          }
          const row = routeClaimRow(result);
          const requestedClaimId = String(params?.claim_id ?? "").trim();
          const returnedClaimId = routeClaimRowId(row) || String(result?.claim_id ?? "").trim();
          const status = String(result?.status ?? "").trim().toLowerCase();
          const unknownDiagnostic = routeClaimUnknownDiagnostic(result);
          const malformedStatus = routeClaimStatusMalformed(result);
          const semanticNegative = routeClaimSemanticNegative(result);
          const transportError = routeClaimTransportError(result);
          if (transportError) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          if (unknownDiagnostic || malformedStatus) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          const rowBound =
            row !== null &&
            routeClaimRowMatchesSession(row, { claim_id: requestedClaimId }) &&
            returnedClaimId === requestedClaimId;
          // A semantic negative such as ``not_owner`` may legitimately echo
          // the peer's active row for diagnostics.  It must never seed this
          // actor's write-gate lease: only an explicit positive broker
          // response can refresh a claim here.
          if (semanticNegative) {
            // A handled terminal/identity negative (not_found, not_owner,
            // actor_finished, etc.) means this lease can no longer authorize
            // writes. Retire the local id but keep the required gate blocked;
            // only malformed/outage responses set the explicit fail-open bit.
            if (
              ["actor_not_admitted", "actor_finished", "episode_mismatch", "invalid_actor_status"].includes(status)
            ) {
              routeState.clear();
            } else {
              routeState.clear(requestedClaimId);
            }
            return;
          }
          if (
            ["blocked", "released", "done", "expired", "closed", "finished"].includes(status) &&
            returnedClaimId
          ) {
            routeState.clear(returnedClaimId);
            return;
          }
          if (unknownDiagnostic || malformedStatus || result?.ok !== true || !rowBound) {
            routeState.clear(requestedClaimId);
            routeState.refresh();
            return;
          }
          if (requestedClaimId && routeClaimRowIsActive(row)) {
            routeState.remember(row);
          } else if (requestedClaimId) {
            routeState.clear(requestedClaimId);
          }
          routeState.refresh();
        },
        onError() {
          routeState.bypassReason = "unavailable";
          routeState.clear();
          return routeClaimBypassResult("cps_update_route");
        },
      },
    );

    registerBrokerTool(
      pi,
      {
        name: "cps_release_route",
        label: "Release Route Claim",
        description: "Release a route claim when the route is finished, abandoned, or superseded.",
        promptSnippet: "Release a completed or abandoned route claim",
        parameters: objectSchema(
          {
            claim_id: stringSchema("Runner-issued route claim id", 128),
            status: {
              type: "string",
              enum: ["released", "done"],
              description: "Terminal route status",
            },
            reason: stringSchema("Short bounded release reason", 1_000),
          },
          ["claim_id"],
        ),
      },
      {
        onResult(result, params) {
          if (routeClaimEnvelopeMalformed(result)) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          if (
            result?.bypassed === true ||
            ["route_claim_bypassed", "route_claim_bypass"].includes(
              String(result?.status ?? "").toLowerCase(),
            )
          ) {
            routeState.bypassReason = normalizeRouteBypassReason(
              result?.route_claim_bypass_reason,
            );
            routeState.clear();
            return;
          }
          const requestedClaimId = String(params?.claim_id ?? "").trim();
          const row = routeClaimRow(result);
          const returnedClaimId = routeClaimRowId(row) || String(result?.claim_id ?? "").trim();
          const unknownDiagnostic = routeClaimUnknownDiagnostic(result);
          const malformedStatus = routeClaimStatusMalformed(result);
          const handledNegative = routeClaimSemanticNegative(result);
          const transportError = routeClaimTransportError(result);
          if (unknownDiagnostic || malformedStatus || transportError) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          const rowBound =
            row !== null &&
            routeClaimRowMatchesSession(row, { claim_id: requestedClaimId }) &&
            returnedClaimId === requestedClaimId;
          // A release is allowed to return a terminal/not-found negative, but
          // only when the response is still bound to the exact requested claim
          // id and this session.  Never delete a local lease based on a peer's
          // echoed id or a sparse adapter response.
          if (handledNegative) {
            // Releasing an already-missing/terminal claim is an idempotent
            // handled outcome, not a broker outage. Drop the local lease and
            // leave the required write gate blocked until a fresh claim.
            routeState.clear(requestedClaimId);
            return;
          }
          if (!rowBound || result?.ok !== true) {
            routeState.bypassReason = "unavailable";
            routeState.clear();
            return;
          }
          routeState.clear(requestedClaimId);
        },
        onError() {
          routeState.bypassReason = "unavailable";
          routeState.clear();
          return routeClaimBypassResult("cps_release_route");
        },
      },
    );
  }

  if (selectionEnabled) registerBrokerTool(pi, {
    name: "cps_feedback",
    label: "Record CPS Exposure Feedback",
    description: "Record attributed feedback for one previously exposed ContextSwarm selection item. Supply the exposure identifiers exactly as returned by the selection surface.",
    promptSnippet: "Record attributed feedback for an exposed CPS selection item",
    parameters: objectSchema(
      {
        request_key: stringSchema("Idempotency key for this feedback event", 256),
        exposure_item_id: stringSchema("Identifier of the previously exposed selection item", 256),
        trace_id: stringSchema("Trace identifier returned with the exposed item", 256),
        feedback_kind: {
          type: "string",
          enum: ["useful", "not_useful", "misleading", "stale", "unsafe", "duplicate", "diagnostic_useful", "needs_refinement", "not_used", "route_attempted", "route_improving"],
          description: "Canonical attribution feedback kind",
        },
        value: { type: "number", description: "Optional numeric feedback value" },
        note: stringSchema("Optional concise attribution note", 8_000),
      },
      ["request_key", "exposure_item_id", "trace_id", "feedback_kind"],
    ),
  });

  if (directMessages) registerBrokerTool(pi, {
    name: "cps_ack",
    label: "Acknowledge CPS Message",
    description: "Acknowledge one message that is visible to this runner-bound actor.",
    promptSnippet: "Acknowledge a consumed CPS direct message",
    parameters: objectSchema(
      { message_id: stringSchema("Visible CPS message id", 64) },
      ["message_id"],
    ),
  });

  if (directMessages) registerBrokerTool(pi, {
    name: "cps_actors",
    label: "List CPS Actors",
    description: "Inspect the bounded public actor roster for recipient discovery.",
    promptSnippet: "Find a CPS actor for a direct handoff",
    parameters: objectSchema({ query: stringSchema("Optional actor/task filter", 300) }),
  });
}
