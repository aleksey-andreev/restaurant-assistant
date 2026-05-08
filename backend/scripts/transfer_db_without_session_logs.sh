#!/usr/bin/env bash

set -euo pipefail

# Two-step PostgreSQL transfer flow for isolated environments:
# 1) dump   - run where source DB is available
# 2) restore - run where target DB is available
#
# Excludes only user session logs data:
# - public.sessions
# - public.graph_state
# - public.pipeline_events
#
# Usage examples:
#   SOURCE_DATABASE_URL='postgresql://u:p@src:5432/db' \
#   DUMP_FILE='backend/db_dumps/restaurant_assistant_no_session_logs.dump' \
#   bash backend/scripts/transfer_db_without_session_logs.sh dump
#
#   TARGET_DATABASE_URL='postgresql://u:p@target:5432/db' \
#   DUMP_FILE='backend/db_dumps/restaurant_assistant_no_session_logs.dump' \
#   CLEAN_TARGET=1 \
#   bash backend/scripts/transfer_db_without_session_logs.sh restore

MODE="${1:-}"
DUMP_FILE="${DUMP_FILE:-backend/db_dumps/restaurant_assistant_no_session_logs.dump}"
CLEAN_TARGET="${CLEAN_TARGET:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

# Preserve explicitly provided values before loading .env.
CLI_SOURCE_DATABASE_URL="${SOURCE_DATABASE_URL:-}"
CLI_TARGET_DATABASE_URL="${TARGET_DATABASE_URL:-}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

SOURCE_DATABASE_URL="${CLI_SOURCE_DATABASE_URL:-${SOURCE_DATABASE_URL:-${DATABASE_URL:-}}}"
TARGET_DATABASE_URL="${CLI_TARGET_DATABASE_URL:-${TARGET_DATABASE_URL:-${DATABASE_URL:-}}}"

normalize_pg_url() {
  local url="$1"
  # SQLAlchemy URLs are not accepted by pg_dump/pg_restore.
  url="${url/postgresql+psycopg:\/\//postgresql:\/\/}"
  url="${url/postgresql+asyncpg:\/\//postgresql:\/\/}"
  echo "${url}"
}

SOURCE_DATABASE_URL="$(normalize_pg_url "${SOURCE_DATABASE_URL}")"
TARGET_DATABASE_URL="$(normalize_pg_url "${TARGET_DATABASE_URL}")"

if [[ "${MODE}" != "dump" && "${MODE}" != "restore" ]]; then
  echo "Usage: $0 dump|restore" >&2
  exit 1
fi

if [[ "${MODE}" == "dump" ]]; then
  if [[ -z "${SOURCE_DATABASE_URL}" ]]; then
    echo "ERROR: Set SOURCE_DATABASE_URL or DATABASE_URL (.env) for dump mode." >&2
    exit 1
  fi
  mkdir -p "$(dirname "${DUMP_FILE}")"
  echo "Creating dump: ${DUMP_FILE}"
  pg_dump "${SOURCE_DATABASE_URL}" \
    --format=custom \
    --no-owner \
    --no-privileges \
    --exclude-table-data=public.sessions \
    --exclude-table-data=public.graph_state \
    --exclude-table-data=public.pipeline_events \
    --file "${DUMP_FILE}"
  echo "Dump created: ${DUMP_FILE}"
  exit 0
fi

if [[ -z "${TARGET_DATABASE_URL}" ]]; then
  echo "ERROR: Set TARGET_DATABASE_URL or DATABASE_URL (.env) for restore mode." >&2
  exit 1
fi
if [[ ! -f "${DUMP_FILE}" ]]; then
  echo "ERROR: Dump file not found: ${DUMP_FILE}" >&2
  exit 1
fi

RESTORE_FLAGS=(--no-owner --no-privileges --dbname "${TARGET_DATABASE_URL}")
if [[ "${CLEAN_TARGET}" == "1" ]]; then
  RESTORE_FLAGS+=(--clean --if-exists)
fi

echo "Restoring dump to target database: ${DUMP_FILE}"
pg_restore "${RESTORE_FLAGS[@]}" "${DUMP_FILE}"
echo "Restore completed."
