import { createFileRoute } from "@tanstack/react-router";

import { createTanStackMcpHandler } from "@lovable.dev/mcp-js/stacks/tanstack";

import mcp from "../lib/mcp/index";

export const Route = createFileRoute("/mcp")({
  server: {
    handlers: {
      ANY: createTanStackMcpHandler(mcp, { resourcePath: "/mcp", metadataPath: "/.well-known/oauth-protected-resource", trustForwardedHost: true }),
    },
  },
});
