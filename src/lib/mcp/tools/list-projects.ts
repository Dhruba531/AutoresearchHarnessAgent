// List every project in the workspace. Structurally identical to the other
// tools — see `health.ts` for the full anatomy of a tool definition.

import { defineTool } from "@lovable.dev/mcp-js";
import { agentlabFetch } from "../client";

export default defineTool({
  name: "list_projects",
  title: "List projects",
  // Note this description does something the others do not: it documents a
  // FAILURE MODE. Telling the model that a missing session cookie produces an
  // auth error means that when the call fails, the model already has the
  // context to explain the cause to the user instead of blindly retrying.
  description:
    "List research projects in the AgentLab workspace. Requires the backend session cookie or an AGENTLAB_SESSION_COOKIE env var; returns an auth error otherwise.",
  inputSchema: {},
  annotations: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  handler: async () => {
    try {
      const data = await agentlabFetch("/api/projects");
      return {
        content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
        // WRAPPED in `{ projects: ... }`, unlike get-project.ts which passes
        // its object through directly. The reason: `structuredContent` must be
        // a JSON object, and this endpoint returns a top-level ARRAY. Wrapping
        // it in a named key both satisfies that requirement and labels the data
        // for the consumer.
        structuredContent: { projects: data },
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Could not list projects: ${(err as Error).message}` }],
        isError: true,
      };
    }
  },
});
