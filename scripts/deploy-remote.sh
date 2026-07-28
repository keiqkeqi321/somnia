#!/usr/bin/env bash
# One-click deploy of the Somnia remote stack (Relay + Web) to a server.
#
# Usage:
#   scripts/deploy-remote.sh              # build web + wheel, push, install, restart relay
#   scripts/deploy-remote.sh --setup      # additionally install systemd unit + nginx config (first time)
#
# Configuration via environment variables:
#   DEPLOY_USER   SSH user                 (default: root)
#   DEPLOY_HOST   Server                   (default: 47.109.202.44)
#   WEB_DIR       Web root on server       (default: /var/www/somnia)
#   DATA_DIR      Relay data dir on server (default: /var/lib/somnia)
#   SERVER_NAME   Public origin, no scheme (default: $DEPLOY_HOST)
#   SERVER_PYTHON Python on the server used for pip install (default: python3; must be >=3.11)
#   TLS_CERT / TLS_KEY                     (default: /etc/nginx/certs/somnia.crt|.key, setup only)
#   SOMNIA_ADMIN_USERNAME / SOMNIA_ADMIN_PASSWORD
#                 Bootstrap admin account  (setup only; prompted if unset)
#
# Server prerequisites: python3.11+ with pip, nginx (for --setup), TLS cert in place.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Optional .env in the repo root (not checked in): SOMNIA_ADMIN_USERNAME,
# SOMNIA_ADMIN_PASSWORD, DEPLOY_USER, DEPLOY_HOST, SERVER_PYTHON, ...
if [ -f "$REPO/.env" ]; then
    set -a
    . "$REPO/.env"
    set +a
fi

DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_HOST="${DEPLOY_HOST:-47.109.202.44}"
WEB_DIR="${WEB_DIR:-/var/www/somnia}"
DATA_DIR="${DATA_DIR:-/var/lib/somnia}"
SERVER_NAME="${SERVER_NAME:-$DEPLOY_HOST}"
SERVER_PYTHON="${SERVER_PYTHON:-python3}"
TLS_CERT="${TLS_CERT:-/etc/nginx/certs/somnia.crt}"
TLS_KEY="${TLS_KEY:-/etc/nginx/certs/somnia.key}"
SERVICE_NAME="somnia-relay"
SETUP=0

for arg in "$@"; do
    case "$arg" in
        --setup) SETUP=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"

echo "==> Building Web client..."
(cd "$REPO/desktop/ui" && npm run build)

echo "==> Building Python wheel..."
rm -rf "$REPO/.tmp-deploy" && mkdir -p "$REPO/.tmp-deploy"
python -m pip wheel --no-deps --no-build-isolation -w "$REPO/.tmp-deploy" "$REPO" >/dev/null
WHEEL="$(ls -t "$REPO"/.tmp-deploy/somnia-*.whl | head -1)"
echo "    wheel: $(basename "$WHEEL")"

echo "==> Pushing Web build to $TARGET:$WEB_DIR ..."
ssh "$TARGET" "mkdir -p '$WEB_DIR' && find '$WEB_DIR' -mindepth 1 -delete"
tar czf - -C "$REPO/desktop/ui/dist" . | ssh "$TARGET" "tar xzf - -C '$WEB_DIR'"

echo "==> Installing wheel and restarting $SERVICE_NAME ..."
ssh "$TARGET" "$SERVER_PYTHON -c 'import sys; assert sys.version_info >= (3, 11), sys.version'" || {
    echo "ERROR: server Python ($SERVER_PYTHON) must be >= 3.11. Install python3.11+ or set SERVER_PYTHON." >&2
    exit 1
}
scp -q "$WHEEL" "$TARGET:/tmp/"
ssh "$TARGET" "$SERVER_PYTHON -m pip install --upgrade '/tmp/$(basename "$WHEEL")' && rm -f '/tmp/$(basename "$WHEEL")'"

if [ "$SETUP" -eq 1 ]; then
    echo "==> Running first-time setup on $TARGET ..."
    ADMIN_USERNAME="${SOMNIA_ADMIN_USERNAME:-admin}"
    if [ -z "${SOMNIA_ADMIN_PASSWORD:-}" ]; then
        read -r -s -p "Relay administrator password (bootstrap only): " SOMNIA_ADMIN_PASSWORD
        echo
    fi
    ssh "$TARGET" "mkdir -p '$DATA_DIR' '$(dirname "$TLS_CERT")' /etc/nginx/conf.d"

    sed -e "s|@DATA_DIR@|$DATA_DIR|g" \
        -e "s|@ADMIN_USERNAME@|$ADMIN_USERNAME|g" \
        -e "s|@ADMIN_PASSWORD@|$SOMNIA_ADMIN_PASSWORD|g" \
        -e "s|@SERVER_NAME@|$SERVER_NAME|g" \
        "$REPO/scripts/deploy/somnia-relay.service" | ssh "$TARGET" "cat > '/etc/systemd/system/$SERVICE_NAME.service'"

    sed -e "s|@SERVER_NAME@|$SERVER_NAME|g" \
        -e "s|@WEB_DIR@|$WEB_DIR|g" \
        -e "s|@TLS_CERT@|$TLS_CERT|g" \
        -e "s|@TLS_KEY@|$TLS_KEY|g" \
        "$REPO/scripts/deploy/nginx-somnia.conf" | ssh "$TARGET" "cat > /etc/nginx/conf.d/somnia.conf"

    ssh "$TARGET" "systemctl daemon-reload && systemctl enable --now '$SERVICE_NAME' && nginx -t && systemctl reload nginx"
    echo "==> Setup complete. Relay runs under systemd, nginx serves https://$SERVER_NAME"
else
    ssh "$TARGET" "systemctl restart '$SERVICE_NAME'"
fi

rm -rf "$REPO/.tmp-deploy"
echo "==> Deploy finished: https://$SERVER_NAME/?remote=1"
