// Fetch one project by ID. This is the only tool here that takes ARGUMENTS,
// which makes it the interesting one — see `inputSchema` below.
//
// For the general anatomy of a tool definition, read `health.ts` first.

import { defineTool } from "@lovable.dev/mcp-js";
import { z } from "zod";
import { agentlabFetch } from "../client";

export default defineTool({
  name: "get_project",
  title: "Get project",
  description:
    "Fetch a single AgentLab project by ID, including its latest run and review findings.",
  // ZOD VALIDATION — the one thing this file demonstrates that the others do not.
  //
  // Zod is a runtime schema validator. Unlike TypeScript types, which vanish at
  // compile time, a zod schema is real code that CHECKS values while the program
  // runs. That is essential here because the input comes from an AI model, which
  // can and will pass a string, a float, or a negative number.
  //
  // Reading the chain left to right, each call narrowing further:
  //   z.number()    must be a number (rejects "5", null, undefined)
  //   .int()        must be a whole number (rejects 5.7)
  //   .positive()   must be > 0 (rejects 0 and -3)
  //   .describe()   documentation surfaced TO THE MODEL, so it knows what to
  //                 pass. Not a validation rule — it is prompt text, same role
  //                 as `description` above.
  //
  // Validation runs BEFORE `handler`, so by the time the handler executes,
  // `project_id` is guaranteed to be a positive integer. An invalid call is
  // rejected with a clear message and never reaches your code — which is also
  // what stops a malformed value being interpolated into the URL below.
  inputSchema: {
    project_id: z.number().int().positive().describe("Numeric project ID."),
  },
  annotations: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  // The validated arguments arrive as the handler's parameter, destructured
  // here into `project_id`. Its type is inferred from the zod schema — zod
  // derives the TypeScript type from the validator, so the two cannot drift.
  handler: async ({ project_id }) => {
    try {
      const data = await agentlabFetch(`/api/projects/${project_id}`);
      return {
        // Two representations of the same data, deliberately:
        //   `content`           — text for the model to read. The `null, 2`
        //                         arguments to JSON.stringify pretty-print with
        //                         2-space indentation, which genuinely helps a
        //                         model parse nested structures.
        //   `structuredContent` — the same payload as machine-readable JSON,
        //                         for clients that want to process it directly
        //                         rather than parse the text back out.
        content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
        // A cast is needed because `agentlabFetch` honestly returns `unknown`.
        // Note this is an unchecked assertion — nothing verifies the backend
        // actually sent an object. Tightening this would mean parsing the
        // response with a zod schema too, the same way the input is validated.
        structuredContent: data as Record<string, unknown>,
      };
    } catch (err) {
      // Returned, not thrown — see health.ts for why this matters.
      return {
        content: [{ type: "text", text: `Could not fetch project: ${(err as Error).message}` }],
        isError: true,
      };
    }
  },
});
