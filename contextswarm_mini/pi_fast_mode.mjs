// Minimal Pi extension used by the paper harness.  It preserves the provider
// payload and only fixes the service tier plus a redacted request tuple.
import { appendFile } from "node:fs/promises";

function evidencePath() {
  const value = String(process.env.CONTEXTSWARM_PI_FAST_MODE_EVIDENCE_PATH ?? "").trim();
  return value || null;
}

async function record(model, payload) {
  const path = evidencePath();
  if (!path) return;
  const reasoning = payload?.reasoning;
  const row = {
    observed_at: new Date().toISOString(),
    provider: model?.provider ?? null,
    model: typeof payload?.model === "string" ? payload.model : model?.id ?? null,
    reasoning_effort: reasoning && typeof reasoning.effort === "string" ? reasoning.effort : null,
    service_tier: payload?.service_tier ?? null,
  };
  await appendFile(path, `${JSON.stringify(row)}\n`, { encoding: "utf8", mode: 0o600 });
}

export default function registerContextSwarmFastMode(pi) {
  pi.on("before_provider_request", async (event, ctx) => {
    const payload = event?.payload && typeof event.payload === "object"
      ? { ...event.payload, service_tier: "priority" }
      : { service_tier: "priority" };
    await record(ctx?.model, payload);
    return payload;
  });
}

