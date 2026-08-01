// Live operator metrics — the MCP equivalent of what the console dashboard
// shows a human. See `health.ts` for the general anatomy of a tool definition.

import { defineTool } from "@lovable.dev/mcp-js";
import { agentlabFetch } from "../client";

export default defineTool({
  name: "system_status",
  title: "System status",
  // Enumerating the specific metrics returned, rather than saying "get status",
  // lets the model match this tool to a question like "how much have we spent?"
  // without having to call it first to find out what it provides.
  description:
    "Get AgentLab operator system status: active/queued runs, reviewers online, gate latency, throughput, and spend.",
  inputSchema: {},
  // NOTE `idempotentHint: false` — the one place this tool differs from the
  // others, and the difference is meaningful. It is still read-only (it changes
  // nothing), but it is NOT idempotent, because live metrics change between
  // calls: ask twice and you legitimately get two different answers.
  //
  // The practical effect is on caching. A client may reuse the result of an
  // idempotent call; this flag tells it not to, so the model always sees
  // current numbers rather than a stale snapshot.
  annotations: { readOnlyHint: true, idempotentHint: false, openWorldHint: true },
  handler: async () => {
    try {
      const data = await agentlabFetch("/api/status");
      return {
        content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
        structuredContent: data as Record<string, unknown>,
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `System status unavailable: ${(err as Error).message}` }],
        isError: true,
      };
    }
  },
});
