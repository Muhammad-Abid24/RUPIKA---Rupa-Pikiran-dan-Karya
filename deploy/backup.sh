#!/bin/sh
# deploy/backup.sh — Online SQLite backup script for RUPIKA / StockForge (SF-014C)
# Guarantees: Hot online backup using SQLite's .backup API without downtime or read locks.

set -eu

DB_PATH="${1:-${DATABASE_URL:-/data/stockforge.db}}"
# Strip sqlite://// or sqlite:/// if present in URL
DB_PATH="$(echo "$DB_PATH" | sed -E 's|^sqlite:///*|/|')"

BACKUP_DIR="${2:-/data/backups}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/stockforge_backup_${TIMESTAMP}.db"
CHECKSUM_FILE="${BACKUP_FILE}.sha256"

echo "=== [RUPIKA] Memulai Hot Online Backup Database SQLite ==="
echo "Sumber database : ${DB_PATH}"
echo "Target backup   : ${BACKUP_FILE}"

if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: File database sumber tidak ditemukan di: ${DB_PATH}" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

# 1. Jalankan SQLite online backup API (.backup)
echo "Menjalankan online backup..."
sqlite3 "$DB_PATH" ".backup '${BACKUP_FILE}'"

# 2. Verifikasi integritas file backup
echo "Memverifikasi integritas file backup..."
INTEGRITY="$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;")"
if [ "$INTEGRITY" != "ok" ]; then
    echo "ERROR: Integritas backup gagal! Hasil: ${INTEGRITY}" >&2
    rm -f "$BACKUP_FILE"
    exit 2
fi

# 3. Hitung SHA-256 checksum untuk verifikasi tamper-proof
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$BACKUP_FILE" > "$CHECKSUM_FILE"
elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$BACKUP_FILE" > "$CHECKSUM_FILE"
fi

BACKUP_SIZE="$(wc -c < "$BACKUP_FILE" | tr -d ' ')"
echo "✓ Backup berhasil dibuat (${BACKUP_SIZE} bytes)."
echo "✓ File backup: ${BACKUP_FILE}"
if [ -f "$CHECKSUM_FILE" ]; then
    echo "✓ Checksum SHA-256: $(cat "$CHECKSUM_FILE")"
fi
echo "=== [RUPIKA] Backup Selesai dengan Sukses ==="
exit 0
