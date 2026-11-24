#!/usr/bin/env bash
# N1/B3 — (re)create the Czech full-text stack in the Postgres container. Idempotent.
# Run this ONCE per fresh container, THEN: .venv/bin/python -m pythia.ingest.czech_fts
#
# It fetches the open-source cs_CZ hunspell dictionary (LibreOffice, UTF-8), copies it into the
# container's tsearch_data dir, and creates: unaccent · the czech_hunspell ispell dictionary · the
# `czech` text-search config (hunspell stemming → simple fallback). The dict FILES live inside the
# container, so this must be re-run if the container is recreated.
set -euo pipefail

CONTAINER="${PG_CONTAINER:-regsuper-postgres}"
DB="${PG_DB:-pythia}"
USER="${PG_USER:-regsuper}"

echo "→ fetching cs_CZ hunspell dictionary (LibreOffice)…"
mkdir -p /tmp/csdict
curl -fsSL -o /tmp/csdict/cs_CZ.dic https://raw.githubusercontent.com/LibreOffice/dictionaries/master/cs_CZ/cs_CZ.dic
curl -fsSL -o /tmp/csdict/cs_CZ.aff https://raw.githubusercontent.com/LibreOffice/dictionaries/master/cs_CZ/cs_CZ.aff

SHARE="$(docker exec "$CONTAINER" pg_config --sharedir)"
echo "→ copying dict into $CONTAINER:$SHARE/tsearch_data/ …"
docker cp /tmp/csdict/cs_CZ.dic "$CONTAINER:$SHARE/tsearch_data/czech.dict"
docker cp /tmp/csdict/cs_CZ.aff "$CONTAINER:$SHARE/tsearch_data/czech.affix"

echo "→ creating unaccent + czech_hunspell dictionary + 'czech' text-search config …"
# NOTE: run this BEFORE building provision.tsv on 'czech'. The DROPs are non-CASCADE on purpose, so
# if the live tsv column already depends on 'czech' the script stops safely rather than dropping it.
# To re-run after tsv is built: first `python -m pythia.ingest.czech_fts --simple`, then this, then
# `python -m pythia.ingest.czech_fts`.
docker exec -i "$CONTAINER" psql -U "$USER" -d "$DB" -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS unaccent;
DROP TEXT SEARCH CONFIGURATION IF EXISTS czech;
DROP TEXT SEARCH DICTIONARY IF EXISTS czech_hunspell;
CREATE TEXT SEARCH DICTIONARY czech_hunspell (TEMPLATE = ispell, DictFile = czech, AffFile = czech);
CREATE TEXT SEARCH CONFIGURATION czech (COPY = simple);
ALTER TEXT SEARCH CONFIGURATION czech
  ALTER MAPPING FOR asciiword, asciihword, hword_asciipart, word, hword, hword_part
  WITH czech_hunspell, simple;
SQL

echo "→ verifying stemming (banka/banky/bankou should share the 'banka' lexeme):"
docker exec -i "$CONTAINER" psql -U "$USER" -d "$DB" -tAc \
  "SELECT to_tsvector('czech','banka banky bance bankou');"
echo "✓ czech FTS ready. Next: .venv/bin/python -m pythia.ingest.czech_fts"
