# lerobot-dashboard frontend

Vite + React + TypeScript + Tailwind + shadcn/ui. Built output lands in
`../static/` and is served by the FastAPI app in production.

## Commands

```bash
npm ci                     # install pinned deps
npm run dev                # dev server on http://localhost:5173 (proxies /api and /ws to 8080)
npm run build              # production build into ../static/
npm run typecheck
npm run lint               # max-warnings 0
npm run generate:api-types # backend OpenAPI -> src/lib/api/schema.d.ts
```

`make dashboard-frontend` from the repo root runs `npm ci && npm run build`.

## OpenAPI type generation workflow

Backend routes are the source of truth. Whenever a teammate adds, renames,
or removes an endpoint on `main`:

1. `git pull` / merge `main` into your worktree.
2. `npm run generate:api-types` — this dumps the live FastAPI schema to
   `openapi.schema.json` and regenerates `src/lib/api/schema.d.ts`.
3. Commit both files together, e.g.
   `chore(dashboard): regenerate OpenAPI types`.
4. Fix any TypeScript errors surfaced by the new schema before pushing.

The dump step invokes `uv run --no-sync python -m
lerobot.dashboard.dev.dump_openapi`, so the dashboard extra must be
installed in the active uv environment.

## Backend endpoint gaps

Hand-written typed clients (e.g. `src/lib/api/robots.ts`) mirror the
Pydantic registry models until the matching endpoints land. Each client
function treats a `404` as "endpoint not yet wired" and returns an empty
result, so the UI renders gracefully against a partial backend.
