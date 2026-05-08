#!/usr/bin/env bash
set -euo pipefail

# One-shot VM update:
# 1) Pull latest main
# 2) Restore DB from committed dump (optional)
# 3) Rebuild and restart docker compose stack
#
# Run from anywhere:
#   bash deploy/update-vm.sh
#
# Optional env:
#   SKIP_DB_RESTORE=1   # skip pg_restore step
#   CLEAN_TARGET=1      # passed to restore script (default: 1)
#   DUMP_FILE=...       # custom dump path (default: backend/db_dumps/restaurant_assistant_no_session_logs.dump)
#   ENV_FILE=...        # custom .env path for restore script
#   TARGET_DATABASE_URL=postgresql://...  # override DB from .env

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

COMPOSE_FILE="docker-compose.prod.yml"
DUMP_FILE="${DUMP_FILE:-backend/db_dumps/restaurant_assistant_no_session_logs.dump}"
SKIP_DB_RESTORE="${SKIP_DB_RESTORE:-0}"
export CLEAN_TARGET="${CLEAN_TARGET:-1}"

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git not found."
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found."
  exit 1
fi

echo "==> Updating repository (main)"
git fetch origin
git checkout main
git pull --ff-only origin main

if [[ "${SKIP_DB_RESTORE}" != "1" ]]; then
  if [[ ! -f "${DUMP_FILE}" ]]; then
    echo "ERROR: dump file not found: ${DUMP_FILE}"
    exit 1
  fi
  echo "==> Restoring database from dump: ${DUMP_FILE}"
  DUMP_FILE="${DUMP_FILE}" bash backend/scripts/transfer_db_without_session_logs.sh restore
else
  echo "==> SKIP_DB_RESTORE=1, database restore skipped"
fi

echo "==> Rebuilding and restarting docker stack"
docker compose -f "${COMPOSE_FILE}" down
docker compose -f "${COMPOSE_FILE}" build --no-cache
docker compose -f "${COMPOSE_FILE}" up -d

echo "==> Runtime status"
docker compose -f "${COMPOSE_FILE}" ps

echo "==> Recent backend logs"
docker compose -f "${COMPOSE_FILE}" logs --tail=100 backend || true

echo "==> Recent frontend logs"
docker compose -f "${COMPOSE_FILE}" logs --tail=100 frontend || true

echo "Done."
