# Postgres 16 + pgvector + PostGIS + ltree + pg_trgm.
# Base image already ships pgvector. ltree and pg_trgm are in postgres-contrib (in the base).
# We add PostGIS from the official Debian repository for the matching PG major.
FROM pgvector/pgvector:pg16

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-16-postgis-3 \
        postgresql-16-postgis-3-scripts \
    && rm -rf /var/lib/apt/lists/*
