// The simplest MCP tool in the project — a connectivity check.
//
// All four tools in this folder share one structure, so this file carries the
// full explanation and the other three only annotate what differs. Read this
// one first.
//
// THE ANATOMY OF AN MCP TOOL:
//   name         the identifier an AI client calls
//   description  prose telling the AI when to use it (this is prompt text)
//   inputSchema  the arguments it accepts, validated before the handler runs
//   annotations  behavioural hints that let clients decide how freely to call it
//   handler      the function that actually does the work

import { defineTool } from "@lovable.dev/mcp-js";
import { z } from "zod";
import { agentlabFetch } from "../client";

export default defineTool({
  // Snake_case by MCP convention. This string is what the AI emits to invoke
  // the tool, so renaming it breaks existing callers.
  name: "health",
  // Human-readable label for a client's UI.
  title: "Health check",
  // Written for the MODEL, not for a developer. The AI picks tools by reading
  // these descriptions, so being specific about what a tool does directly
  // improves how reliably it gets chosen. Compare the much longer description
  // in list-projects.ts, which also documents a failure mode.
  description: "Ping the AgentLab backend and return its health status.",
  // No arguments. An empty object still declares the schema explicitly, which
  // tells clients "this takes nothing" rather than leaving it unspecified.
  inputSchema: {},
  // Behavioural hints. These matter because they govern how cautious a client
  // must be — an AI agent may call a read-only tool freely, whereas a
  // destructive one might require user confirmation first.
  //   readOnlyHint   — changes no state; safe to call any number of times
  //   idempotentHint — repeat calls produce the same result
  //   openWorldHint  — touches the outside world (network), so results can vary
  //                    and calls can fail for external reasons
  annotations: { readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  handler: async () => {
    try {
      const data = await agentlabFetch("/health");
      // The MCP response format: `content` is an array of typed blocks. Text is
      // the common case; the protocol also allows images and embedded
      // resources, which is why it is an array of tagged objects rather than a
      // plain string.
      return { content: [{ type: "text", text: JSON.stringify(data) }] };
    } catch (err) {
      // CRITICAL PATTERN: errors are RETURNED, not thrown.
      //
      // A thrown exception would surface to the AI client as a protocol-level
      // failure — opaque, and usually unrecoverable. Returning a normal
      // response flagged `isError: true` hands the model a readable explanation
      // it can act on: retry, try a different tool, or tell the user what is
      // wrong. Every tool in this folder follows this convention.
      return {
        // `(err as Error).message` — a cast, needed because TypeScript types
        // `catch` bindings as `unknown` (anything can be thrown in JS).
        content: [{ type: "text", text: `Health check failed: ${(err as Error).message}` }],
        isError: true,
      };
    }
  },
});

// keep z imported for consumers that want stricter schemas later
//
// `void <expr>` evaluates the expression and discards the result. Here it is a
// trick to mark `z` as "used" so the linter does not flag the unused import and
// strip it. See get-project.ts for zod actually doing its job.
void z;
