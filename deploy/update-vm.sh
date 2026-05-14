#!/usr/bin/env bash
set -euo pipefail

# One-shot VM update:
# 1) Pull latest main
# 2) Optionally restore DB from dump (--restore-db)
# 3) Rebuild and restart docker compose stack
#
# Run from anywhere:
#   bash deploy/update-vm.sh              # git + docker only (no DB restore)
#   bash deploy/update-vm.sh --restore-db # also pg_restore from dump
#
# Optional env (restore step only):
#   CLEAN_TARGET=1      # passed to restore script (default: 1)
#   DUMP_FILE=...       # custom dump path (default: backend/db_dumps/restaurant_assistant_no_session_logs.dump)
#   ENV_FILE=...        # custom .env path for restore script
#   TARGET_DATABASE_URL=postgresql://...  # override DB from .env

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

COMPOSE_FILE="docker-compose.prod.yml"
DUMP_FILE="${DUMP_FILE:-backend/db_dumps/restaurant_assistant_no_session_logs.dump}"
RESTORE_DB=0
export CLEAN_TARGET="${CLEAN_TARGET:-1}"

usage() {
  cat <<'EOF'
Usage: bash deploy/update-vm.sh [options]

  Default: pull main from origin, rebuild and restart docker compose (no DB dump).

Options:
  --restore-db     Restore PostgreSQL from DUMP_FILE (see env below).
  --dump-file PATH Override dump path for this run (same as DUMP_FILE=...).
  -h, --help       Show this help.

Environment (restore only):
  DUMP_FILE, CLEAN_TARGET, ENV_FILE, TARGET_DATABASE_URL
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restore-db)
      RESTORE_DB=1
      shift
      ;;
    --dump-file)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --dump-file requires a path." >&2
        exit 1
      fi
      DUMP_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

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

if [[ "${RESTORE_DB}" -eq 1 ]]; then
  if [[ ! -f "${DUMP_FILE}" ]]; then
    echo "ERROR: dump file not found: ${DUMP_FILE}"
    exit 1
  fi
  echo "==> Restoring database from dump: ${DUMP_FILE}"
  DUMP_FILE="${DUMP_FILE}" bash backend/scripts/transfer_db_without_session_logs.sh restore
else
  echo "==> Database restore skipped (pass --restore-db to restore from dump)"
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
