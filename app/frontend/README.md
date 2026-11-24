# Frontend

The web interface for Pythia, built with Next.js 16, React 19, TanStack Query and Tailwind CSS 4.

## What's here

| Route | Purpose |
|---|---|
| `/answer` | Ask a legal question; see the drafted answer with clickable citations to the exact provisions |
| `/retrieve` | Inspect raw retrieval: which provisions were found and through which graph relations |
| `/sources/[provision_id]` | Read a single provision with its version history and connected law |
| `/profiles`, `/projects` | Company profiles and matters that give questions their context |
| `/digest/[profile_id]` | A per-profile digest of the regulation that applies |
| `/control/*` | Operator console: system health, ingestion, the Pythia engine, and a 3D view of the legal graph |

Source layout: `src/app/` (routes), `src/components/` (UI by feature), `src/lib/` (API client and helpers).

## Run it

The UI talks to the backend API (`app/src/regsuper`), which it expects at `http://localhost:8002`.
Set `NEXT_PUBLIC_API_URL` to point it somewhere else.

```bash
npm install
npm run dev
```

Then open http://localhost:3000.

Optional feature switches: `NEXT_PUBLIC_ANSWER_VIEW_ENABLED=1` and `NEXT_PUBLIC_DIGEST_UI_ENABLED=1`.

## Checks

```bash
npm run lint
npx tsc --noEmit
npm test          # vitest
npm run build
```
