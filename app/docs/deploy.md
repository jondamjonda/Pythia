# Production deployment runbook

End-to-end steps for putting the Regulatory Super App on the internet. The
scaffold lives in `deploy/`; this doc tells you the exact buttons to push
in the right order.

**Cost:** ~€30/mo VPS + ~€1/mo object storage + ~$30 OpenAI budget = ~€60/mo total.

**Prerequisites:**
- Hetzner Cloud account (https://accounts.hetzner.com)
- Vercel account (https://vercel.com)
- Domain you own (optional; you can serve on the Hetzner IP at MVP)
- OpenAI + Cohere keys with credit

---

## 1 · Hetzner Object Storage (5 min)

Object storage holds raw ČNB PDFs (deferred), embedding parquet snapshots,
and daily pg_dump backups. Bucket cost is ~€1/mo at MVP scale.

1. Hetzner console → **Project › Object Storage**.
2. Create a bucket named `regsuper-mvp` in region **FSN1** (Frankfurt).
3. Generate an **access key + secret**; keep both.
4. Lifecycle rule: under "Lifecycle rules", add:
   - prefix `backups/pgdump/`, expiration **30 days**

Endpoint URL format: `https://fsn1.your-objectstorage.com`

---

## 2 · Hetzner VPS — CX42 (10 min)

1. Hetzner console → **Cloud › Servers › New Server**.
2. Location: **FSN1** (Frankfurt). Image: **Ubuntu 24.04**. Type: **CX42**
   (8 vCPU / 16 GB RAM / 160 GB SSD, ~€30/mo).
3. SSH key: upload your public key (you'll need passwordless login).
4. **Firewall**: allow inbound 22, 80, 443. Block everything else.
5. After provisioning note the public IP.

SSH in:

```bash
ssh root@<HETZNER_IP>
```

---

## 3 · DNS (optional, skip for IP-only MVP)

If you have a domain:

| Record | Type | Target |
|---|---|---|
| `api.your-domain.example` | A | `<HETZNER_IP>` |
| `app.your-domain.example` | CNAME | `cname.vercel-dns.com` *(set after Vercel deploy)* |

If you skip DNS, serve the API on the raw IP — replace `api.your-domain.example`
in the Caddyfile with `:443` and Caddy will use a self-signed cert. Less
nice for the lawyer but functional.

---

## 4 · Install on the VPS (15 min)

Either clone the repo or copy via `scp` / `rsync`. Then:

```bash
# As root on the VPS
cd /tmp && git clone <your-repo-url> regsuper
cd regsuper
sudo bash deploy/scripts/install.sh
```

The script:
1. Installs apt deps (Caddy, Docker, awscli, Python 3.12, build tools)
2. Creates the `regsuper` system user and `/opt/regsuper`,
   `/etc/regsuper`, `/var/lib/regsuper`, `/var/log/regsuper`
3. Sets up the Python venv and installs the package (`pip install -e .`)
4. Installs systemd units and the Caddyfile
5. Starts Postgres via `deploy/docker-compose.prod.yml`
6. Reloads Caddy

It will **not** start the API yet — it waits for you to drop in secrets.

---

## 5 · Secrets (5 min)

```bash
sudo cp /opt/regsuper/secrets.prod.env.example /etc/regsuper/secrets.env
sudo chmod 600 /etc/regsuper/secrets.env
sudo chown root:regsuper /etc/regsuper/secrets.env
sudo nano /etc/regsuper/secrets.env
```

Fill in every `<placeholder>`:

| Key | What |
|---|---|
| `<db-password>` | Same value you set as `POSTGRES_PASSWORD` for the compose service. |
| `OPENAI_API_KEY` | Production key from https://platform.openai.com/api-keys |
| `COHERE_API_KEY` | Production rerank key (the trial cap of 10 reqs/min will rate-limit real traffic) |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | From step 1 |
| `ADMIN_BASIC_AUTH_PASS` | Long random string. Save in 1Password. |
| `FRONTEND_ORIGINS` | After Vercel deploy, paste the Vercel URL + your custom domain |
| `REFUSAL_THRESHOLD` | Leave at `0.6` until Day-13 lawyer labeling is done |

Then start the API and the backup timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now regsuper-api regsuper-backup.timer
sudo systemctl status regsuper-api    # should show "active (running)"
```

---

## 6 · Run database migrations + bootstrap data (one-time)

```bash
# Apply migrations
sudo -u regsuper /opt/regsuper/.venv/bin/python -c "
import asyncio
from sqlalchemy import text
from regsuper.storage.database import get_engine

async def main():
    eng = get_engine()
    async with eng.connect() as conn:
        conn = await conn.execution_options(isolation_level='AUTOCOMMIT')
        for f in ['migrations/001_initial_schema.sql',
                  'migrations/002_api_call.sql',
                  'migrations/003_eval_query_group.sql']:
            sql = open(f).read()
            # split on ; and skip BEGIN/COMMIT (asyncpg can't do multi-stmt)
            for stmt in [s.strip() for s in sql.split(';')
                         if s.strip() and s.strip().upper() not in ('BEGIN','COMMIT')]:
                try:
                    await conn.execute(text(stmt))
                except Exception as e:
                    print('skip:', str(e)[:80])
asyncio.run(main())
"

# Bring in regulatory documents — see the corresponding ingest scripts:
sudo -u regsuper /opt/regsuper/.venv/bin/python -m regsuper.ingest.eurlex     # Day 3
sudo -u regsuper /opt/regsuper/.venv/bin/python -m regsuper.ingest.esbirka    # Day 4
sudo -u regsuper /opt/regsuper/.venv/bin/python -m regsuper.ingest.sentences  # Day 5
sudo -u regsuper /opt/regsuper/.venv/bin/python -m regsuper.ingest.definitions
sudo -u regsuper /opt/regsuper/.venv/bin/python -m regsuper.ingest.embed --all  # Day 6, ~$0.50

# This whole sequence takes ~45 minutes. Watch progress:
journalctl -u regsuper-api -f
```

---

## 7 · Smoke-test the API (1 min)

```bash
# From your laptop, hitting the VPS
curl -s https://api.your-domain.example/health
# → {"status":"ok","db":"connected",...}

curl -s -X POST https://api.your-domain.example/internal/retrieve \
    -H "Content-Type: application/json" \
    -d '{"query":"AIFMD marketing notification CZ","jurisdiction":["EU","CZ"]}' \
  | jq .results[0]
# → expect AIFMD Article 31 at top
```

---

## 8 · Frontend on Vercel (10 min)

```bash
# From your laptop
cd frontend
npm install -g vercel
vercel login
vercel link            # answer the prompts; pick a project name
vercel env add regsuper_api_url
# When prompted, enter: https://api.your-domain.example
vercel --prod
```

Vercel hands you back a URL like `https://regsuper-abc123.vercel.app`.
**Go back to step 5** and add that URL to `FRONTEND_ORIGINS`, then:

```bash
sudo systemctl restart regsuper-api
```

If you have a custom domain, point `app.your-domain.example` as a CNAME
to `cname.vercel-dns.com` and configure it in the Vercel project settings.

---

## 9 · Final acceptance (plan §13.5 Day 14)

| Gate | How to check |
|---|---|
| Submit project → see findings → click citation → read source | Open the Vercel URL, click **New project**, run analysis, watch findings appear, click a citation pill, verify the source viewer opens |
| Refusal works on out-of-scope queries | `/retrieve` → "How long is a Tesla Model 3 warranty?" → amber refusal banner |
| Every finding has audit_event row | `psql -d regsuper -c "SELECT count(*) FROM audit_event WHERE action='l3.obligations.extracted'"` should match the obligation finding count |
| Daily backup runs | Wait 24h, then `aws s3 ls s3://regsuper-mvp/backups/pgdump/ --endpoint-url=https://fsn1.your-objectstorage.com` |

---

## Common operational commands

```bash
# Tail the structured log
sudo tail -f /var/log/regsuper/app.log | jq -r '.code + " " + .friendly_message'

# Restart everything
sudo systemctl restart regsuper-api

# Trigger a backup right now
sudo systemctl start regsuper-backup.service

# See spend + kill-switch state
curl -s -u admin:<pass> https://api.your-domain.example/admin/api/system | jq .

# Engage the kill switch (refuses every new LLM call)
curl -s -X POST -u admin:<pass> https://api.your-domain.example/admin/api/killswitch/engage \
  -H "Content-Type: application/json" -d '{"reason":"deploying"}'
```

---

## Rollback

The audit_event table is append-only (DB trigger). To roll back a botched
analysis, mark findings `review_state='superseded'` rather than deleting:

```sql
UPDATE finding
   SET review_state = 'superseded'
 WHERE project_id = '<id>' AND analysis_layer = 'L3' AND created_at > now() - interval '1h';
```

If the API service itself is wedged, the kill switch (above) buys you
time to redeploy without billing more OpenAI / Cohere calls. The daily
S3 backup is your full database restore path:

```bash
aws s3 cp --endpoint-url=$S3_ENDPOINT_URL s3://regsuper-mvp/backups/pgdump/<date>.sql.gz - \
  | gunzip | psql $DATABASE_URL_SYNC
```
