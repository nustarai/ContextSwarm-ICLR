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

function registerBrokerTool(pi, definition) {
  pi.registerTool({
    ...definition,
    executionMode: "sequential",
    async execute(_toolCallId, params, signal) {
      return toolResult(await brokerCall(definition.name, params, signal));
    },
  });
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

function isReadableFile(rel) {
  return (
    ["problem.md", "result.lean", "metadata.json", "PUBLIC_FILES.md"].includes(rel) ||
    /^baseline\/[^/]+\.lean$/.test(rel) ||
    /^tasks\/[^/]+\/(?:problem\.md|result\.lean|metadata\.json|PUBLIC_FILES\.md)$/.test(rel) ||
    /^tasks\/[^/]+\/baseline\/[^/]+\.lean$/.test(rel)
  );
}

function isWritableCandidate(rel) {
  return rel === "result.lean" || /^tasks\/[^/]+\/result\.lean$/.test(rel);
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

function installPathGuard(pi) {
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
      const configured = String(process.env.CONTEXTSWARM_WORKDIR ?? "").trim();
      let cwd;
      try {
        cwd = realpathSync(configured || ctx.cwd);
      } catch {
        return { block: true, reason: "assigned workspace is unavailable" };
      }
      const raw = typeof input.path === "string" ? input.path : "";
      const lexical = raw ? (isAbsolute(raw) ? resolve(raw) : resolve(cwd, raw)) : "";
      const rel = lexical ? relativeInside(lexical, cwd) : null;
      if (!rel || !isWritableCandidate(rel)) {
        return { block: true, reason: "write/edit is restricted to assigned result.lean" };
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
          reason: "bash is restricted to the staged evaluate.py and formal_query helpers",
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
  installPathGuard(pi);

  registerBrokerTool(pi, {
    name: "judge_check",
    label: "Controlled Judge Check",
    description:
      "Submit the runner-bound result.lean to the controlled external Lean Judge. The task, baseline, environment, profile, endpoint, deadline, and concurrency are fixed by the runner. For a normal single-task worker call with no arguments; Mono must provide task_id.",
    promptSnippet: "Check the current result.lean through the controlled external Judge",
    promptGuidelines: [
      "Use judge_check one candidate at a time; never attempt local Lean or raw Judge access.",
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

  registerBrokerTool(pi, {
    name: "cps_inbox",
    label: "CPS Inbox",
    description: "Read bounded unacknowledged direct messages for this actor.",
    promptSnippet: "Read direct CPS messages for this actor",
    parameters: objectSchema({ limit: integerSchema("Maximum returned messages", 8) }),
  });

  registerBrokerTool(pi, {
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

  registerBrokerTool(pi, {
    name: "cps_ack",
    label: "Acknowledge CPS Message",
    description: "Acknowledge one message that is visible to this runner-bound actor.",
    promptSnippet: "Acknowledge a consumed CPS direct message",
    parameters: objectSchema(
      { message_id: stringSchema("Visible CPS message id", 64) },
      ["message_id"],
    ),
  });

  registerBrokerTool(pi, {
    name: "cps_actors",
    label: "List CPS Actors",
    description: "Inspect the bounded public actor roster for recipient discovery.",
    promptSnippet: "Find a CPS actor for a direct handoff",
    parameters: objectSchema({ query: stringSchema("Optional actor/task filter", 300) }),
  });
}
