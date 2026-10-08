#!/usr/bin/env bash
# Consistent backup of the VisionOps database and snapshot directory.
#
#   VISIONOPS_DATABASE_URL  sqlite:///... or postgresql+psycopg://...
#   VISIONOPS_DATA_DIR      runtime data root (default ./data)
#   VISIONOPS_SNAPSHOT_DIR  snapshot directory (default $DATA_DIR/snapshots)
#   VISIONOPS_BACKUP_DIR    output directory (default /var/backups/visionops)
#
# SQLite uses the online `.backup` command (safe while the API runs). Postgres
# uses a compressed `pg_dump`. Retains 7 daily backups plus weekly (Sunday)
# copies for 4 weeks. See docs/DEPLOYMENT.md §5.5.

set -euo pipefail

DATA_DIR="${VISIONOPS_DATA_DIR:-./data}"
SNAPSHOT_DIR="${VISIONOPS_SNAPSHOT_DIR:-${DATA_DIR}/snapshots}"
BACKUP_DIR="${VISIONOPS_BACKUP_DIR:-/var/backups/visionops}"
DATABASE_URL="${VISIONOPS_DATABASE_URL:-sqlite:///./data/visionops.db}"

STAMP="$(date +%F)"
WEEKDAY="$(date +%u)" # 1=Mon .. 7=Sun
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

if [[ "${DATABASE_URL}" == sqlite* ]]; then
    # Strip the scheme (sqlite:// or sqlite:///) to get a filesystem path.
    DB_PATH="${DATABASE_URL#sqlite://}"
    DB_PATH="${DB_PATH#/}"
    if [[ "${DATABASE_URL}" == sqlite:////* ]]; then
        DB_PATH="/${DB_PATH}"
    fi
    if [[ ! -f "${DB_PATH}" ]]; then
        echo "backup: database not found: ${DB_PATH}" >&2
        exit 1
    fi
    OUT="${BACKUP_DIR}/visionops-${STAMP}.db"
    sqlite3 "${DB_PATH}" ".backup '${OUT}'"
    echo "backup: sqlite -> ${OUT}"
elif [[ "${DATABASE_URL}" == postgresql* ]]; then
    OUT="${BACKUP_DIR}/visionops-${STAMP}.dump"
    pg_dump -Fc --no-owner --dbname="${DATABASE_URL}" -f "${OUT}"
    echo "backup: postgres -> ${OUT}"
else
    echo "backup: unsupported VISIONOPS_DATABASE_URL scheme" >&2
    exit 1
fi

if [[ -d "${SNAPSHOT_DIR}" ]]; then
    ARCHIVE="${BACKUP_DIR}/snapshots-${STAMP}.tgz"
    tar -C "$(dirname "${SNAPSHOT_DIR}")" -czf "${ARCHIVE}" "$(basename "${SNAPSHOT_DIR}")"
    echo "backup: snapshots -> ${ARCHIVE}"
fi

# Weekly copy on Sundays, kept for 28 days.
if [[ "${WEEKDAY}" == "7" ]]; then
    for f in "${BACKUP_DIR}"/*-"${STAMP}".*; do
        [[ -e "${f}" ]] || continue
        cp -p "${f}" "${f%.*}-weekly.${f##*.}"
    done
fi

# Retention: 7 daily + 4 weekly.
find "${BACKUP_DIR}" -maxdepth 1 -type f -name '*-weekly.*' -mtime +28 -delete
find "${BACKUP_DIR}" -maxdepth 1 -type f ! -name '*-weekly.*' -mtime +7 -delete
echo "backup: done"
