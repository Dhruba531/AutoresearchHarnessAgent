# AgentLab Research Hub

Marketing landing page + **operator console** for AgentLab. A single front-end
app (TanStack Start, SSR React 19) that also serves its own auth backend from
the same Worker.

## Stack

- **TanStack Start** (SSR React 19) + **TanStack Router** — file-based routes in `src/routes/`
- **Tailwind v4**, **Radix UI**, **react-hook-form + zod**, **sonner**
- Build: **Vite**, bundling Nitro → Cloudflare Workers
- Package manager: **bun** (`bun.lock`); npm works too. Node ≥ 23.
- Deploy target: **Cloudflare Worker** — `src/server.ts` is the `fetch(request, env, ctx)` entry.

## Getting started

```bash
cp .env.example .env    # then fill in your Supabase values
bun install             # or: npm install
bun run dev             # dev server on :3000, proxies /api → :8000
```

Other commands:

```bash
bun run build           # production build (client + SSR worker → dist/)
bun run lint            # eslint
bun run format          # prettier
npx tsc --noEmit        # type-check
```

## Layout

| Concern | File |
| --- | --- |
| Landing page | `src/routes/index.tsx` |
| Control room / workspace (`/console`) | `src/routes/console.tsx` |
| App shell, `<head>`, providers | `src/routes/__root.tsx` |
| Login/register form | `src/components/operator-console.tsx` |
| Typed API client (browser → `/api`) | `src/lib/api.ts` |
| In-app auth backend | `src/server-api.ts` |
| Worker entry (routes `/api/*`, then SSR) | `src/server.ts` |
| Generated route tree (do not hand-edit) | `src/routeTree.gen.ts` |

## The `/api` split

- **Dev** (`bun run dev`): `vite.config.ts` proxies `/api` → `http://localhost:8000`
  (the separate FastAPI backend). Override with `VITE_API_PROXY_TARGET`.
- **Prod**: no proxy — `src/server-api.ts` serves `/api/*` from the Worker.

Only auth is implemented in-app (`/api/auth/login`, `/register`, `/logout`,
`GET /api/auth/me`), using a stateless HMAC-SHA256 signed `HttpOnly` cookie
(`agentlab_session`). Set `SESSION_SECRET` to override the signing secret.
Project and run endpoints return a clean JSON 404 — they need persistence and
live in the FastAPI backend.

## Environment

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored — never
commit real keys.
