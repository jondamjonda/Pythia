# Pythia engine

The reasoning core of Pythia: it builds and maintains the legal knowledge graph, retrieves the provisions that govern a question, and drafts and verifies cited answers. It runs as an HTTP service that the web application calls. See the [project README](../README.md) for the big picture.

## Package layout

| Module | Responsibility |
|---|---|
| `pythia/contract.py` | The public API models: `PythiaQuery` in, `PythiaAnswer` out |
| `pythia/service.py` | FastAPI service (`/v1/retrieve`, `/v1/answer`, `/v1/browse`, `/v1/act/{id}`, `/health`, `/metrics`, …) |
| `pythia/engine.py` | `PythiaEngine`: seed retrieval, graph expansion, rank fusion and trust decoration |
| `pythia/ports.py` | Adapters for the database, embeddings, language models and telemetry, and `NexusPort`, the single entry point into the graph |
| `pythia/config.py` | All settings, read from `PYTHIA_*` environment variables |
| `pythia/flag_registry.py` | Generates [`docs/FLAG_GATES.md`](docs/FLAG_GATES.md) and keeps it in sync with the settings |
| `pythia/ingest/` | Source connectors (e-Sbírka, EUR-Lex, court rulings, ČNB bulletin, regional law, regulator guidance), versioning, enrichment, integrity checks, scheduler |
| `pythia/graph/` | Relation registry, graph materialisation, recursive-CTE and PageRank expansion, rank fusion, trust resolution, browsing |
| `pythia/gnn/` | The graph neural network and checkpoint loading |
| `pythia/reasoning/` | Question gate, intake, answer drafting, claim extraction, verifiers, repair and the claim ledger |
| `pythia/eval/`, `eval/` | Evaluation harnesses and gold question sets |
| `train/` | GNN training (stage 1 self-supervised, stage 2 supervised) and the checkpoint manifest |
| `scripts/` | Operational scripts: corpus rebuild, reindex, manifest generation, Czech full-text setup |
| `corpus/` | The committed inventory of every ingestable act (`corpus_manifest.json`) |

## Run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'          # add ',gnn' for PyTorch + sentence-transformers
.venv/bin/uvicorn pythia.service:app --port 8099
```

Configuration comes from environment variables, for example `PYTHIA_DATABASE_URL` (defaults to the local development database). The corpus lives in Postgres. [`docs/REBUILD.md`](docs/REBUILD.md) explains how to restore or rebuild it.

Useful entry points:

```bash
.venv/bin/python -m pythia.engine "query" cte          # retrieval only (cte | ppr | gnn)
.venv/bin/python -m pythia.reasoning.answer "query"    # full drafted and verified answer
.venv/bin/python -m pythia.flag_registry --write        # regenerate docs/FLAG_GATES.md
```

## Test

```bash
.venv/bin/pytest -m "not live and not flags_on"   # default suite (expects the local corpus database)
.venv/bin/pytest -m flags_on                      # the verification loop in enforce mode
.venv/bin/ruff check pythia train tests scripts eval
```
