#!/usr/bin/env bash
# One-shot installer for a fresh Ubuntu 24.04 Hetzner CX42 (plan §13.2).
#
# What this does, in order:
#   1. apt-update + install Caddy, docker, awscli, build tools, Python 3.12
#   2. create the `regsuper` system user + /opt/regsuper + /etc/regsuper +
#      /var/lib/regsuper + /var/log/regsuper directory layout
#   3. clone the repo (or copy from current dir) into /opt/regsuper
#   4. set up the Python venv and install dependencies
#   5. install systemd units + Caddyfile + backup script
#   6. enable the timers and Caddy
#
# Run as root on a freshly provisioned Hetzner instance:
#   curl -fsSL https://raw.githubusercontent.com/<you>/regsuper/main/deploy/scripts/install.sh | sudo bash
#
# Or copy the repo locally first and run `sudo bash deploy/scripts/install.sh`.
# Idempotent: re-running is safe.

set -euo pipefail

REGSUPER_USER="${REGSUPER_USER:-regsuper}"
REGSUPER_HOME="${REGSUPER_HOME:-/opt/regsuper}"
SECRETS_DIR="/etc/regsuper"
DATA_DIR="/var/lib/regsuper"
LOG_DIR="/var/log/regsuper"
REPO_URL="${REPO_URL:-}"

log() { echo "[$(date -u +%FT%TZ)] $*"; }

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

log "== 1/6 · apt packages =="
apt-get update -qq
apt-get install -y -qq \
    ca-certificates curl gnupg lsb-release \
    git build-essential \
    python3.12 python3.12-venv python3-pip \
    postgresql-client awscli \
    debian-keyring debian-archive-keyring apt-transport-https

# Docker — official repo
if ! command -v docker &>/dev/null; then
    log "  installing docker"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
fi

# Caddy — official repo
if ! command -v caddy &>/dev/null; then
    log "  installing caddy"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
fi

log "== 2/6 · user + directories =="
id -u "$REGSUPER_USER" &>/dev/null || useradd --system --create-home --home-dir "$REGSUPER_HOME" --shell /bin/bash "$REGSUPER_USER"
usermod -aG docker "$REGSUPER_USER" || true
install -d -m 0755 -o "$REGSUPER_USER" -g "$REGSUPER_USER" "$REGSUPER_HOME" "$DATA_DIR" "$DATA_DIR/pgdata" "$LOG_DIR"
install -d -m 0750 -o root           -g "$REGSUPER_USER" "$SECRETS_DIR"

log "== 3/6 · source code =="
if [[ -n "$REPO_URL" && ! -d "$REGSUPER_HOME/.git" ]]; then
    sudo -u "$REGSUPER_USER" git clone "$REPO_URL" "$REGSUPER_HOME"
elif [[ -d "$(pwd)/src/regsuper" ]]; then
    log "  copying from $(pwd)"
    rsync -a --delete --exclude=.venv --exclude=__pycache__ --exclude=.next --exclude=node_modules \
        "$(pwd)/" "$REGSUPER_HOME/"
    chown -R "$REGSUPER_USER:$REGSUPER_USER" "$REGSUPER_HOME"
else
    log "  no REPO_URL set and no local source — leaving $REGSUPER_HOME untouched"
fi

log "== 4/6 · Python venv + deps =="
sudo -u "$REGSUPER_USER" bash -c "
    cd '$REGSUPER_HOME'
    [[ -d .venv ]] || python3.12 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -e .
"

log "== 5/6 · systemd units + Caddy =="
install -m 0644 "$REGSUPER_HOME/deploy/systemd/regsuper-api.service"     /etc/systemd/system/
install -m 0644 "$REGSUPER_HOME/deploy/systemd/regsuper-backup.service"  /etc/systemd/system/
install -m 0644 "$REGSUPER_HOME/deploy/systemd/regsuper-backup.timer"    /etc/systemd/system/
install -m 0755 "$REGSUPER_HOME/deploy/scripts/backup.sh"                /opt/regsuper/deploy/scripts/backup.sh
install -m 0644 "$REGSUPER_HOME/deploy/caddy/Caddyfile"                  /etc/caddy/Caddyfile

systemctl daemon-reload

# Seed the secrets file from the template on first install so the operator has a
# concrete file to edit (LEDGER-M27). We copy it un-filled and chmod 600 — the API
# is NOT enabled until the operator has filled in the <placeholder> values.
if [[ ! -f "$SECRETS_DIR/secrets.env" && -f "$REGSUPER_HOME/secrets.prod.env.example" ]]; then
    log "  seeding $SECRETS_DIR/secrets.env from secrets.prod.env.example (fill in the <placeholders>)"
    install -m 0640 -o root -g "$REGSUPER_USER" \
        "$REGSUPER_HOME/secrets.prod.env.example" "$SECRETS_DIR/secrets.env"
fi

# Start Postgres FIRST, before enabling the API (LEDGER-M27). The API connects to
# the DB on boot; enabling it before Postgres is up races the dependency and the
# first start fails. POSTGRES_PASSWORD is read from the secrets file so it matches
# the DATABASE_URL the API uses — but ONLY once the file is real (no remaining
# <placeholder>), so we never source the un-filled template (its <...> values are
# shell-redirection metacharacters). On a not-yet-configured box we fall back to
# the previous ":-regsuper" default; the API stays disabled there anyway.
PG_PW="regsuper"
if [[ -f "$SECRETS_DIR/secrets.env" ]] && ! grep -q '<.*>' "$SECRETS_DIR/secrets.env"; then
    # grep a clean KEY=value line; tolerate it being absent (PG_PW keeps its default).
    _pw_line="$(grep -E '^POSTGRES_PASSWORD=' "$SECRETS_DIR/secrets.env" | tail -n1 || true)"
    [[ -n "$_pw_line" ]] && PG_PW="${_pw_line#POSTGRES_PASSWORD=}"
fi
log "  starting Postgres via docker compose"
sudo -u "$REGSUPER_USER" bash -c "cd '$REGSUPER_HOME/deploy' && \
    POSTGRES_PASSWORD='$PG_PW' docker compose -f docker-compose.prod.yml up -d"

# Enable the API only once a secrets file exists AND it has no remaining <placeholders>.
if [[ ! -f "$SECRETS_DIR/secrets.env" ]] || grep -q '<.*>' "$SECRETS_DIR/secrets.env"; then
    log "  $SECRETS_DIR/secrets.env has unfilled <placeholder> values — NOT enabling regsuper-api."
    log "  fill it in (incl. ENVIRONMENT=prod + the deliberate REQUIRE_AUTH decision), then:"
    log "    sudo systemctl enable --now regsuper-api"
    log "    sudo systemctl enable --now regsuper-backup.timer"
else
    log "  enabling regsuper-api + backup timer"
    systemctl enable --now regsuper-api
    systemctl enable --now regsuper-backup.timer
fi

log "  reloading caddy"
systemctl reload caddy || systemctl restart caddy

log "== 6/6 · done =="
log "  check: systemctl status regsuper-api"
log "  logs:  journalctl -u regsuper-api -f"
log "  caddy: systemctl status caddy"
