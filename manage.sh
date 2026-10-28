#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
dc() { docker compose --env-file install.env "$@"; }
case "${1:-help}" in
  start)
    dc run --rm --no-deps bot check-license
    dc up -d --wait db
    dc run --rm init
    dc up -d bot web
    ;;
  stop) dc stop bot web ;;
  restart) dc up -d --force-recreate bot web ;;
  status) dc ps ;;
  logs) dc logs --tail 100 -f bot web ;;
  license) dc run --rm --no-deps bot check-license ;;
  backup)
    mkdir -p backups
    backup_dir="backups/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir "$backup_dir"
    dc exec -T db pg_dump -U wireout -d wireout -Fc > "$backup_dir/database.dump"
    cp app.env install.env compose.yaml "$backup_dir/"
    chmod 600 "$backup_dir/"*
    echo "Backup saved to $backup_dir. It contains secrets; store it securely."
    ;;
  *) echo 'Usage: sudo bash manage.sh {start|stop|restart|status|logs|license|backup}' ;;
esac
