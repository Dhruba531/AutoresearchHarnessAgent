// MCP SERVER DEFINITION — the entry point for the whole `src/lib/mcp/` folder.
//
// ===========================================================================
// WHAT IS MCP, AND WHY DOES THIS APP HAVE ONE?
// ===========================================================================
// MCP (Model Context Protocol) is a standard that lets an AI assistant call
// your application's functions. Normally your app exposes a UI for humans; MCP
// exposes a parallel interface for AI clients.
//
// So this folder makes AgentLab controllable by an AI. Instead of a person
// opening the console and reading the metrics, an assistant can call the
// `system_status` tool and get the same numbers back as structured JSON.
//
// The moving parts:
//
//   index.ts          ← you are here. Declares the server: its identity, its
//                       auth requirements, and which tools it offers.
//   client.ts           A small fetch helper the tools share.
//   tools/*.ts          One file per callable tool.
//
// The HTTP endpoints that expose all this live in `src/routes/[.mcp]/` and
// `src/routes/mcp.ts`.
//
// This is the most specialised corner of the codebase. If you are still
// learning the stack, it is entirely reasonable to read this folder last —
// nothing in the user-facing app depends on it.

import { auth, defineMcp } from "@lovable.dev/mcp-js";
import healthTool from "./tools/health";
import systemStatusTool from "./tools/system-status";
import listProjectsTool from "./tools/list-projects";
import getProjectTool from "./tools/get-project";

// OAuth issuer MUST be the direct Supabase host — the .lovable.cloud proxy
// URL that publish rewrites SUPABASE_URL to is rejected by mcp-js as an
// issuer mismatch (RFC 8414). The project ref is the one Supabase value
// that survives publish unchanged; the fallback keeps the issuer well-formed
// during the throwaway manifest-extract eval (a token can never verify
// against the sentinel, so unauthenticated access is not possible).
//
// Unpacking that, because it is doing something subtle:
//
// An OAuth "issuer" is the URL identifying who minted a token. Verification is
// an EXACT STRING MATCH — a URL that merely proxies to the same server still
// fails. Publishing rewrites SUPABASE_URL to a .lovable.cloud proxy address,
// which would break that match, so the issuer is reconstructed here from the
// project ref (the one value publishing leaves alone).
//
// The `?? "project-ref-unset"` fallback exists for build-time tooling that
// evaluates this module without real environment variables. Note why it is safe
// rather than a security hole: no genuine token can ever verify against a
// nonsense issuer, so the fallback fails CLOSED. It keeps the value well-formed
// enough not to crash the build while guaranteeing every request is rejected.
const projectRef = import.meta.env.VITE_SUPABASE_PROJECT_ID ?? "project-ref-unset";

export default defineMcp({
  // Machine-readable identifier used by MCP clients.
  name: "agentlab-mcp",
  // Human-readable label shown in a client's UI.
  title: "AgentLab MCP",
  version: "0.1.0",
  // `instructions` is prose aimed at the AI, not at a developer. The client
  // feeds it to the model as context so it knows which tool suits which task.
  // Treat it as prompt engineering: naming each tool and its purpose measurably
  // improves tool selection, and the last sentence sets the expectation that
  // tools act on behalf of the signed-in user.
  instructions:
    "Tools for the AgentLab operator console. Use `health` to verify connectivity, `system_status` for live operator metrics, and `list_projects` / `get_project` to inspect research projects. Callers must sign in as an AgentLab user; tools act on behalf of that user.",
  // Require an OAuth 2.1 bearer token from the Supabase authorization server
  // on every tool call. Without this, publishing exposes every tool to any
  // caller on the internet.
  //
  // That warning is meant literally. An MCP server is reachable over the public
  // internet once published; without this block, anyone who found the URL could
  // enumerate and read your projects.
  auth: auth.oauth.issuer({
    issuer: `https://${projectRef}.supabase.co/auth/v1`,
    // The audience claim a token must carry. "authenticated" is Supabase's
    // standard audience for a signed-in user, so anonymous tokens are refused.
    acceptedAudiences: "authenticated",
  }),
  // The tools on offer. Adding a tool means creating a file in `tools/` AND
  // adding it to this array — miss the second step and the tool silently does
  // not exist as far as clients are concerned.
  tools: [healthTool, systemStatusTool, listProjectsTool, getProjectTool],
});
