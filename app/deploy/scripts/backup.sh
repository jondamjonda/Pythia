#!/usr/bin/env bash
# Daily pg_dump → gzip → Hetzner Object Storage. Plan §15.4.
#
# Reads from /etc/regsuper/secrets.env via the systemd EnvironmentFile.
# Required env: DATABASE_URL_SYNC, S3_ENDPOINT_URL, S3_BUCKET, plus AWS-style
# creds (S3_ACCESS_KEY/S3_SECRET_KEY mapped to AWS_ACCESS_KEY_ID/...).
#
# The S3 lifecycle rule on the bucket (configured once in the Hetzner
# console) deletes objects under backups/pgdump/ older than 30 days, so we
# don't manage retention here.

set -euo pipefail

if [[ -z "${DATABASE_URL_SYNC:-}" ]]; then
    echo "DATABASE_URL_SYNC unset; aborting" >&2
    exit 2
fi
if [[ -z "${S3_ENDPOINT_URL:-}" || -z "${S3_BUCKET:-}" ]]; then
    echo "S3_ENDPOINT_URL or S3_BUCKET unset; aborting" >&2
    exit 2
fi

export AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY:?S3_ACCESS_KEY unset}"
export AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY:?S3_SECRET_KEY unset}"
export AWS_DEFAULT_REGION="${S3_REGION:-fsn1}"
export AWS_EC2_METADATA_DISABLED="true"

DATE=$(date -u +%F)
KEY="backups/pgdump/${DATE}.sql.gz"
TMP=$(mktemp -t regsuper-pgdump.XXXXXX.sql.gz)
trap 'rm -f "$TMP"' EXIT

echo "[$(date -u +%FT%TZ)] dumping → $TMP"
pg_dump --no-owner --no-privileges "$DATABASE_URL_SYNC" | gzip -9 >"$TMP"

SIZE=$(stat -c%s "$TMP")
echo "[$(date -u +%FT%TZ)] dump size $SIZE bytes; uploading s3://${S3_BUCKET}/${KEY}"

aws s3 cp \
    --endpoint-url "${S3_ENDPOINT_URL}" \
    --no-progress \
    --content-type application/gzip \
    "$TMP" "s3://${S3_BUCKET}/${KEY}"

echo "[$(date -u +%FT%TZ)] backup complete: s3://${S3_BUCKET}/${KEY}"
