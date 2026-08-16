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
bun run dev             # dev server on :8080, proxies /api → :8000
```

Other commands:

```bash
bun run build           # production build (client + SSR worker → dist/)
bun run lint            # eslint
bun run format          # prettier
npx tsc --noEmit        # type-check
```

## Layout

| Concern                                 | File                                  |
| --------------------------------------- | ------------------------------------- |
| Landing page                            | `src/routes/index.tsx`                |
| Control room / workspace (`/console`)   | `src/routes/console.tsx`              |
| App shell, `<head>`, providers          | `src/routes/__root.tsx`               |
| Login/register form                     | `src/components/operator-console.tsx` |
| Auth + typed API client                 | `src/lib/api.ts`                      |
| Supabase browser client                 | `src/integrations/supabase/client.ts` |
| Provider OAuth (Google)                 | `src/integrations/supabase/oauth.ts`  |
| `/api/*` stub (JSON 404)                | `src/server-api.ts`                   |
| Worker entry (SSR + error wrapper)      | `src/server.ts`                       |
| Generated route tree (do not hand-edit) | `src/routeTree.gen.ts`                |

## Auth

Sign-in runs **browser → Supabase Auth** directly; there is no server-side auth
code in this repo.

- Email/password: `supabase.auth.signInWithPassword` / `signUp`, wrapped by
  `login()` and `register()` in `src/lib/api.ts`.
- Google: `src/integrations/supabase/oauth.ts`. Enable the provider and add your
  deployed origin as a redirect URL under **Authentication → Providers** in the
  Supabase dashboard, or the callback is rejected.
- `__root.tsx` subscribes to `supabase.auth.onAuthStateChange` and routes the
  user on return from the OAuth redirect.

## The `/api` split

This app ships **no backend**. `src/server-api.ts` answers every `/api/*` request
with a JSON 404 so client data calls fail predictably instead of parsing an HTML
error page. Point `VITE_API_BASE` at a real backend to enable project/run data.

In dev, `vite.config.ts` proxies `/api` → `http://localhost:8000`; override with
`VITE_API_PROXY_TARGET`.

## Environment

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored — never commit
real keys.

`VITE_*` values are **inlined into the client bundle at build time**, not read at
runtime, so they must be present when you build. Only put publishable keys there.

## Deploy (Cloudflare Workers)

### From CI (recommended)

`.github/workflows/deploy.yml` builds and deploys on every push to `main`, and
can be re-run on demand via **Actions → Deploy to Cloudflare Workers → Run
workflow**.

The workflow has to be on `main` before either trigger works — GitHub only
exposes `workflow_dispatch` for workflows present on the default branch. So the
order is: add the secrets, then merge, and the merge itself deploys.

Add these repo secrets under **Settings → Secrets and variables → Actions**:

| Secret | Where to find it |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | Cloudflare dashboard → API Tokens → "Edit Cloudflare Workers" template |
| `CLOUDFLARE_ACCOUNT_ID` | The dashboard URL, or `wrangler whoami` |
| `VITE_SUPABASE_URL` | Supabase → Project Settings → API |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | Same page — the publishable/anon key, **never** service-role |
| `VITE_SUPABASE_PROJECT_ID` | The project ref |

CI is the reliable path because some sandboxed environments block outbound
connections to `api.cloudflare.com`, and `wrangler deploy` then fails with
`fetch failed` regardless of how valid the token is.

### From a laptop

```bash
npm install
npx wrangler login                 # once

VITE_SUPABASE_URL=… \
VITE_SUPABASE_PUBLISHABLE_KEY=… \
VITE_SUPABASE_PROJECT_ID=… \
  npm run build

npm run deploy                     # wrangler deploy, using ./wrangler.jsonc
```

Deploy with the committed `wrangler.jsonc`, **not** the
`.output/server/wrangler.json` that nitro generates. The generated file derives
the Worker name from the repo directory, so using it publishes a second Worker
on a different URL — see the comments in `wrangler.jsonc`.

Then add the resulting `*.workers.dev` origin to Supabase's redirect allow-list,
or OAuth sign-in will fail on the deployed site.
