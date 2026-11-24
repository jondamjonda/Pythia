# Pythia application

The product layer around the [Pythia engine](../engine): a FastAPI backend (Python package `regsuper`) and a Next.js [frontend](frontend). It manages companies, projects and users, runs the regulatory analysis for a given business, calls the engine for grounded answers, and keeps the audit trail that legal work requires. See the [project README](../README.md) for the big picture.

## Backend layout (`src/regsuper/`)

| Package | Responsibility |
|---|---|
| `api/` | HTTP endpoints: answers, retrieval, projects, company profiles, digests, evidence and export, trust, events (server-sent), admin |
| `analysis/` | The analysis cascade: intake, then L1 candidate retrieval, L2 applicability ("does this provision apply to this business?"), L3 obligation extraction and gap detection, with a compliance guard and per-role model routing |
| `retrieval/` | Hybrid search (keyword + vector, reciprocal rank fusion) with query rewriting and HyDE legs |
| `ingest/` | Parsers that turn EUR-Lex, e-Sbírka and ČNB documents into provision rows |
| `pythia/` | HTTP client for the Pythia engine (feature-flagged) |
| `entitlements/`, `billing/` | Plan tiers, scopes and metering; an append-only billing event outbox |
| `conduit/` | Metered external API and MCP surface with OAuth / API-key protection |
| `observability/` | Metrics, cost tracking, quotas, kill switches, environment manifests and the feature-flag registry |
| `storage/` | Database sessions and ORM models, the audit log, object storage (S3). Tenant isolation is enforced by Postgres row-level security (`migrations/006`–`008`) and the tenancy middleware |
| `calibration/` | Evaluation-set generation and threshold fitting (with a Streamlit labelling UI in `apps/`) |
| `integrations/` | File-format adapters for onboarding |

Other folders: `migrations/` (numbered SQL schema, applied by `scripts/apply_migrations.py` or the Docker init script), `tests/` (about 1,850 tests), `deploy/` and `docker/` (production scaffolding), `docs/` (deployment runbook, gates, flag registry), `scripts/` (smoke tests, backtests, local launchers in `scripts/local/`).

## Run

```bash
docker compose up -d                                   # Postgres 16 + pgvector + PostGIS
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp secrets.env.example secrets.env                     # fill in keys; set PYTHIA_URL to the engine
.venv/bin/uvicorn regsuper.api.main:app --port 8002
```

`scripts/local/run_everything.sh` starts the backend with the full local configuration, and `scripts/local/run_app_local.sh` starts the backend and frontend together.

## Test

```bash
.venv/bin/pytest                                       # database-backed tests skip when Postgres is down
.venv/bin/ruff check src tests apps scripts
```
