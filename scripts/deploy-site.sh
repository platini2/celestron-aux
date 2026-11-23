#!/usr/bin/env bash
# celestron-aux · Copyright (C) 2021-2026 Patricio Latini · SPDX-License-Identifier: GPL-3.0-or-later
#
# Publish the project's web page (site/) to its server.
#   scripts/deploy-site.sh            publish site/ as committed in HEAD
#   scripts/deploy-site.sh --setup    first time only: also install the nginx site and get its certificate
#
# What it does:
#   1. Takes site/ from the commit (uncommitted edits are never published).
#   2. Backs up the live page to $BACKUP_DIR/<time>/ on the server.
#   3. Uploads it to $DEPLOY_ROOT, which holds nothing else, and sets the owner.
#   4. Checks the public page matches.
# It only writes to $DEPLOY_ROOT (and, with --setup, this site's own nginx file and certificate).
#
# Settings: deploy.env (not in git), a shell file that sets DEPLOY_HOST
# (user@server), DEPLOY_NAME (the site's host name) and DEPLOY_ROOT (its own
# folder under /var/www), and optionally DEPLOY_PORT (22), DEPLOY_OWNER
# (www-data:www-data) and BACKUP_DIR (/var/backups/celestron-aux).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
say() { echo "deploy-site: $*"; }
die() { echo "deploy-site: $*" >&2; exit 1; }

[[ -f deploy.env ]] || die "deploy.env not found: create it with DEPLOY_HOST, DEPLOY_NAME and DEPLOY_ROOT (see the top of this script)."
# shellcheck disable=SC1091
source deploy.env
: "${DEPLOY_HOST:?set DEPLOY_HOST in deploy.env}"
: "${DEPLOY_ROOT:?set DEPLOY_ROOT in deploy.env}"
: "${DEPLOY_NAME:?set DEPLOY_NAME in deploy.env}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
DEPLOY_OWNER="${DEPLOY_OWNER:-www-data:www-data}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/celestron-aux}"
DEPLOY_URL="${DEPLOY_URL:-https://$DEPLOY_NAME}"
SSH=(ssh -p "$DEPLOY_PORT" -o BatchMode=yes -o ConnectTimeout=15 "$DEPLOY_HOST")

case "$DEPLOY_ROOT" in /var/www/?*) ;; *) die "DEPLOY_ROOT must be a folder under /var/www/" ;; esac

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git archive HEAD site | tar -x -C "$TMP"
[[ -f "$TMP/site/index.html" ]] || die "site/index.html is not committed"
SHORT="$(git rev-parse --short HEAD)"

if [[ "${1:-}" == "--setup" ]]; then
  CONF="deploy/$DEPLOY_NAME.conf"
  [[ -f "$CONF" ]] || die "$CONF not found"
  say "setting up $DEPLOY_NAME on the server"
  "${SSH[@]}" "test ! -e /etc/nginx/sites-available/$DEPLOY_NAME.conf" \
    || die "/etc/nginx/sites-available/$DEPLOY_NAME.conf already exists; setup was done before"
  "${SSH[@]}" "mkdir -p '$DEPLOY_ROOT' && chown $DEPLOY_OWNER '$DEPLOY_ROOT'"
  AVAIL="/etc/nginx/sites-available/$DEPLOY_NAME.conf"
  ENABLED="/etc/nginx/sites-enabled/$DEPLOY_NAME.conf"
  # First a plain HTTP site, so certbot can prove the name; then the full HTTPS one.
  # Whenever nginx rejects a step, that step is undone, so the server's other sites
  # (and their certificate renewals, which reload nginx) are never left with a broken config.
  sed -n '/^# ---------- HTTP/,/^# ---------- HTTPS/p' "$CONF" | sed '$d' \
    | "${SSH[@]}" "cat > '$AVAIL' && ln -s '$AVAIL' '$ENABLED' && if nginx -t; then systemctl reload nginx; else rm -f '$ENABLED' '$AVAIL'; exit 1; fi" \
    || die "nginx rejected the HTTP site; removed it again"
  "${SSH[@]}" "certbot certonly --webroot -w '$DEPLOY_ROOT' -d '$DEPLOY_NAME' --non-interactive --keep-until-expiring" \
    || die "certbot could not get a certificate; the HTTP-only site is left in place"
  "${SSH[@]}" "cp '$AVAIL' '$AVAIL.http-only' && cat > '$AVAIL' && if nginx -t; then systemctl reload nginx && rm -f '$AVAIL.http-only'; else mv '$AVAIL.http-only' '$AVAIL'; exit 1; fi" < "$CONF" \
    || die "nginx rejected the HTTPS site; put the HTTP-only one back"
  say "nginx site and certificate ready"
fi

say "publishing site/ from $SHORT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
"${SSH[@]}" "mkdir -p '$BACKUP_DIR/$STAMP' && cp -a '$DEPLOY_ROOT/.' '$BACKUP_DIR/$STAMP/' 2>/dev/null || true"
rsync -rlt --delete --exclude '.well-known/' -e "ssh -p $DEPLOY_PORT -o BatchMode=yes -o ConnectTimeout=15" \
  "$TMP/site/" "$DEPLOY_HOST:$DEPLOY_ROOT/"
"${SSH[@]}" "chown -R $DEPLOY_OWNER '$DEPLOY_ROOT'"

want="$(shasum -a 256 "$TMP/site/index.html" | cut -d' ' -f1)"
got="$(curl -fsS "$DEPLOY_URL/" | shasum -a 256 | cut -d' ' -f1)" || die "could not fetch $DEPLOY_URL"
[[ "$want" == "$got" ]] || die "$DEPLOY_URL does not match site/index.html from $SHORT"
say "live at $DEPLOY_URL (site/ from $SHORT; backup in $BACKUP_DIR/$STAMP)"
