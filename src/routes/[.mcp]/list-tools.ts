import { createFileRoute } from "@tanstack/react-router";

import { createTanStackListToolsHandler } from "@lovable.dev/mcp-js/stacks/tanstack";

import mcp from "../../lib/mcp/index";

export const Route = createFileRoute("/.mcp/list-tools")({
  server: {
    handlers: {
      // ANY: TanStack returns SPA HTML for methods not in `handlers`; the SDK 405s instead.
      ANY: createTanStackListToolsHandler(mcp, { resourcePath: "/mcp", metadataPath: "/.well-known/oauth-protected-resource", trustForwardedHost: true }),
    },
  },
});
