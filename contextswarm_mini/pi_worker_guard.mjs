// Formal worker public-surface guard for Pi built-in tools.
// The trusted evaluator broker remains the authority; this extension prevents
// ordinary model tool calls from reaching sibling workspaces, private config,
// helper source, raw network clients, or aggregate run artifacts.
import { readFileSync } from "node:fs";
import { basename, dirname, relative, resolve, sep } from "node:path";

const enabled = String(process.env.CONTEXTSWARM_WORKER_GUARD ?? "") === "1";
const workerRoot = resolve(String(process.env.CONTEXTSWARM_WORKDIR ?? process.cwd()));
const experimentMode = String(process.env.CONTEXTSWARM_EXPERIMENT_MODE ?? "").toLowerCase();
const bestCandidate = String(process.env.CONTEXTSWARM_BEST_CANDIDATE_FILE ?? "").trim();
const maxWriteBytes = Number.parseInt(process.env.CONTEXTSWARM_WORKER_MAX_WRITE_BYTES ?? "2097152", 10);
const evaluatorTimeout = Number.parseInt(
  process.env.CONTEXTSWARM_EVALUATOR_COMMAND_TIMEOUT_SECONDS ?? "420",
  10,
);

function inside(path, root) {
  const rel = relative(root, path);
  return rel === "" || (!rel.startsWith(`..${sep}`) && rel !== ".." && !rel.startsWith("/"));
}

function relativeWorkerPath(path, cwd = workerRoot) {
  const absolute = resolve(cwd, String(path ?? ""));
  return { absolute, relative: relative(workerRoot, absolute).replaceAll(sep, "/") };
}

function taskPublicRelative(rel) {
  const parts = rel.split("/");
  let leaf = rel;
  if (experimentMode === "mono") {
    if (parts.length < 3 || parts[0] !== "tasks") return false;
    leaf = parts.slice(2).join("/");
  }
  return (
    leaf === "problem.md"
    || leaf === "metadata.json"
    || leaf === "PUBLIC_FILES.md"
    || leaf === "result.lean"
    || /^baseline\/[^/]+\.lean$/.test(leaf)
    || /^scratch\/(?:[^/]+\/)*[^/]+$/.test(leaf)
  );
}

function allowedRead(path, cwd = workerRoot) {
  const { absolute, relative: rel } = relativeWorkerPath(path, cwd);
  if (bestCandidate && absolute === resolve(bestCandidate)) return true;
  if (experimentMode === "mono" && (rel === "PUBLIC_FILES.md" || rel === "result.json")) return true;
  return inside(absolute, workerRoot) && taskPublicRelative(rel);
}

function allowedWrite(path, cwd = workerRoot) {
  const { absolute, relative: rel } = relativeWorkerPath(path, cwd);
  if (!inside(absolute, workerRoot)) return false;
  if (experimentMode === "mono") {
    return /^tasks\/[^/]+\/result\.lean$/.test(rel) || /^tasks\/[^/]+\/scratch\//.test(rel);
  }
  return rel === "result.lean" || rel.startsWith("scratch/");
}

function boundedEdit(path, edits) {
  if (!Array.isArray(edits) || edits.length === 0) return false;
  const { absolute } = relativeWorkerPath(path);
  let projected;
  try {
    projected = readFileSync(absolute).byteLength;
  } catch {
    return false;
  }
  for (const edit of edits) {
    if (
      !edit
      || typeof edit !== "object"
      || typeof edit.oldText !== "string"
      || typeof edit.newText !== "string"
    ) return false;
    projected += Buffer.byteLength(edit.newText, "utf8") - Buffer.byteLength(edit.oldText, "utf8");
    if (projected < 0 || projected > maxWriteBytes) return false;
  }
  return projected <= maxWriteBytes;
}

function helperKind(path, cwd) {
  const { absolute, relative: rel } = relativeWorkerPath(path, cwd);
  if (!inside(absolute, workerRoot)) return null;
  const leaf = basename(absolute);
  if (!new Set(["evaluate.py", "formal_query", "context_piece"]).has(leaf)) return null;
  const parentRel = relative(workerRoot, dirname(absolute)).replaceAll(sep, "/");
  if (experimentMode === "mono") {
    if (!/^tasks\/[^/]+$/.test(parentRel)) return null;
  } else if (parentRel !== "") {
    return null;
  }
  return leaf;
}

function shellTokens(segment) {
  const tokens = [];
  let token = "";
  let quote = null;
  let escaped = false;
  for (let index = 0; index < segment.length; index += 1) {
    const char = segment[index];
    if (escaped) {
      token += char;
      escaped = false;
      continue;
    }
    if (char === "\\" && quote !== "'") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (char === quote) quote = null;
      else token += char;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
    } else if (/\s/.test(char)) {
      if (token) tokens.push(token);
      token = "";
    } else {
      token += char;
    }
  }
  if (quote || escaped) return null;
  if (token) tokens.push(token);
  return tokens;
}

function splitShell(command) {
  const segments = [];
  let current = "";
  let quote = null;
  let escaped = false;
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index];
    const next = command[index + 1] ?? "";
    if (escaped) {
      current += char;
      escaped = false;
      continue;
    }
    if (char === "\\" && quote !== "'") {
      current += char;
      escaped = true;
      continue;
    }
    if (quote) {
      if (char === quote) quote = null;
      if ((quote === '"' && (char === "$" || char === "`")) || char === "\n" || char === "\r") return null;
      current += char;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      current += char;
      continue;
    }
    if (
      char === "$"
      || char === "`"
      || char === "<"
      || char === ">"
      || char === "\n"
      || char === "\r"
      || char === "("
      || char === ")"
      || char === "{"
      || char === "}"
    ) return null;
    if (char === "&" && next !== "&") return null;
    if (char === ";" || char === "|" || (char === "&" && next === "&")) {
      if (current.trim()) segments.push(current.trim());
      current = "";
      if ((char === "|" || char === "&") && next === char) index += 1;
      continue;
    }
    current += char;
  }
  if (quote || escaped || !current.trim() && segments.length === 0) return null;
  if (current.trim()) segments.push(current.trim());
  return segments;
}

function looksLikePath(token) {
  return (
    token.includes("/")
    || token.startsWith(".")
    || /\.(?:lean|md|json|toml|txt)$/.test(token)
    || token === "PUBLIC_FILES.md"
    || token === "result.lean"
    || token === "problem.md"
    || token === "metadata.json"
  );
}

function checkReadCommand(tokens, cwd, piped) {
  const command = basename(tokens[0]);
  if (command === "pwd" && tokens.length === 1) return { ok: true, cwd };
  if (command === "true" || command === "false") return { ok: tokens.length === 1, cwd };
  if (command === "test") {
    const paths = tokens.slice(1).filter(looksLikePath);
    return {
      ok: paths.length > 0 && paths.every((path) => allowedRead(path, cwd) || helperKind(path, cwd)),
      cwd,
    };
  }
  const allowed = new Set(["sed", "head", "tail", "wc", "grep", "rg", "diff"]);
  if (!allowed.has(command)) return { ok: false, cwd };
  if (command === "sed") {
    const safeRange = /^\d+(?:,\d+)?p$/;
    if (tokens.length !== 4 || tokens[1] !== "-n" || !safeRange.test(tokens[2])) {
      return { ok: false, cwd };
    }
    return { ok: allowedRead(tokens[3], cwd), cwd };
  }
  if (
    command === "rg"
    && tokens.some((token) => token === "--pre" || token.startsWith("--pre="))
  ) return { ok: false, cwd };
  if (tokens.some((token) => token === "--files" || token === "--hidden" || token === "-R" || token === "-r")) {
    return { ok: false, cwd };
  }
  const paths = tokens.slice(1).filter(looksLikePath);
  if (paths.length === 0) return { ok: piped && new Set(["head", "tail", "wc"]).has(command), cwd };
  return { ok: paths.every((path) => allowedRead(path, cwd)), cwd };
}

function checkBash(command) {
  const segments = splitShell(command);
  if (!segments) return false;
  let cwd = workerRoot;
  let piped = false;
  for (const segment of segments) {
    const tokens = shellTokens(segment);
    if (!tokens || tokens.length === 0) return false;
    if (tokens[0] === "cd") {
      if (tokens.length !== 2) return false;
      const target = resolve(cwd, tokens[1]);
      const rel = relative(workerRoot, target).replaceAll(sep, "/");
      const allowedDirectory = target === workerRoot || /^tasks\/[^/]+$/.test(rel) || /^(?:tasks\/[^/]+\/)?scratch(?:\/.*)?$/.test(rel);
      if (!inside(target, workerRoot) || !allowedDirectory) return false;
      cwd = target;
      piped = false;
      continue;
    }
    const executable = basename(tokens[0]);
    if (executable === "python3" || executable === "python") {
      if (tokens.length !== 2 || helperKind(tokens[1], cwd) !== "evaluate.py") return false;
      piped = false;
      continue;
    }
    const helper = helperKind(tokens[0], cwd);
    if (helper === "formal_query") {
      piped = false;
      continue;
    }
    if (helper === "context_piece") {
      // argparse accepts unique long-option abbreviations by default, so
      // block --body-f... as well as the full spelling.
      if (tokens.some((token) => token.startsWith("--body-f"))) {
        return false;
      }
      piped = false;
      continue;
    }
    const checked = checkReadCommand(tokens, cwd, piped);
    if (!checked.ok) return false;
    cwd = checked.cwd;
    piped = true;
  }
  return true;
}

export default function registerFormalWorkerGuard(pi) {
  if (!enabled) return;
  pi.on("tool_call", async (event) => {
    const name = String(event.toolName ?? "").toLowerCase();
    const input = event.input ?? {};
    if (name === "read") {
      const path = input.path ?? input.file_path;
      if (!allowedRead(path)) return { block: true, reason: "Path is outside the public formal worker surface" };
      return undefined;
    }
    if (name === "write" || name === "edit") {
      const path = input.path ?? input.file_path;
      if (!allowedWrite(path)) return { block: true, reason: "Only result.lean and bounded scratch are writable" };
      const withinBound = name === "edit"
        ? boundedEdit(path, input.edits)
        : Buffer.byteLength(String(input.content ?? ""), "utf8") <= maxWriteBytes;
      if (!withinBound) {
        return { block: true, reason: "Write exceeds the formal worker byte bound" };
      }
      return undefined;
    }
    if (name === "bash") {
      const command = String(input.command ?? input.cmd ?? "");
      if (!checkBash(command)) {
        return { block: true, reason: "Command is outside the guarded formal worker shell surface" };
      }
      if (/\bevaluate\.py\b|(?:^|\/)formal_query(?:\s|$)/.test(command)) {
        const current = Number(input.timeout ?? 0);
        input.timeout = Math.max(Number.isFinite(current) ? current : 0, evaluatorTimeout);
      }
      return undefined;
    }
    if (new Set(["grep", "find", "ls"]).has(name)) {
      const path = input.path ?? input.paths;
      if (typeof path !== "string" || !allowedRead(path)) {
        return { block: true, reason: "Broad discovery is disabled; use PUBLIC_FILES.md and direct paths" };
      }
      return undefined;
    }
    return { block: true, reason: "Tool is outside the guarded formal worker surface" };
  });
}
