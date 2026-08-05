#!/usr/bin/env bash
# One-click deploy of the Somnia product homepage (homepage/) to a server.
#
# Usage:
#   scripts/deploy-homepage.sh              # push homepage files to the server
#   scripts/deploy-homepage.sh --setup      # additionally install the nginx config (first time / after template changes)
#
# Configuration via environment variables:
#   DEPLOY_USER   SSH user                 (default: root)
#   DEPLOY_HOST   Server                   (default: 47.109.202.44)
#   HOME_DIR      Homepage dir on server   (default: /var/www/somnia-home)
#   WEB_DIR       Web app root on server   (default: /var/www/somnia, setup only)
#   SERVER_NAME   Public origin, no scheme (default: somnia.top)
#   TLS_CERT / TLS_KEY                     (default: /etc/nginx/certs/somnia.crt|.key, setup only)
#
# The nginx config routes https://$SERVER_NAME/ to the homepage and serves the
# Remote web client under /app/ (see scripts/deploy/nginx-somnia.conf).
# Server prerequisites: nginx (for --setup), TLS cert in place, and the remote
# stack already deployed at WEB_DIR (scripts/deploy-remote.sh).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Optional .env in the repo root (not checked in): DEPLOY_USER, DEPLOY_HOST,
# HOME_DIR, WEB_DIR, SERVER_NAME, TLS_CERT, TLS_KEY, ...
if [ -f "$REPO/.env" ]; then
    set -a
    . "$REPO/.env"
    set +a
fi

DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_HOST="${DEPLOY_HOST:-47.109.202.44}"
HOME_DIR="${HOME_DIR:-/var/www/somnia-home}"
WEB_DIR="${WEB_DIR:-/var/www/somnia}"
SERVER_NAME="${SERVER_NAME:-somnia.top}"
TLS_CERT="${TLS_CERT:-/etc/nginx/certs/somnia.crt}"
TLS_KEY="${TLS_KEY:-/etc/nginx/certs/somnia.key}"
SETUP=0

for arg in "$@"; do
    case "$arg" in
        --setup) SETUP=1 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"

echo "==> Pushing homepage to $TARGET:$HOME_DIR ..."
ssh "$TARGET" "mkdir -p '$HOME_DIR' && find '$HOME_DIR' -mindepth 1 -delete"
tar czf - -C "$REPO/homepage" . | ssh "$TARGET" "tar xzf - -C '$HOME_DIR'"

if [ "$SETUP" -eq 1 ]; then
    echo "==> Installing nginx config on $TARGET ..."
    ssh "$TARGET" "mkdir -p /etc/nginx/conf.d"
    sed -e "s|@SERVER_NAME@|$SERVER_NAME|g" \
        -e "s|@WEB_DIR@|$WEB_DIR|g" \
        -e "s|@HOME_DIR@|$HOME_DIR|g" \
        -e "s|@TLS_CERT@|$TLS_CERT|g" \
        -e "s|@TLS_KEY@|$TLS_KEY|g" \
        "$REPO/scripts/deploy/nginx-somnia.conf" | ssh "$TARGET" "cat > /etc/nginx/conf.d/somnia.conf"
    ssh "$TARGET" "nginx -t && systemctl reload nginx"
    echo "==> Setup complete. https://$SERVER_NAME/ serves the homepage; the web app lives at https://$SERVER_NAME/app/."
fi

echo "==> Deploy finished: https://$SERVER_NAME/"
