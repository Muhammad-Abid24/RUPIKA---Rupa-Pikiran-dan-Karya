#!/bin/sh
# deploy/restore.sh — Safe SQLite database restore script for RUPIKA / StockForge (SF-014C)
# Guarantees: Validates backup integrity before restore, takes pre-restore snapshot, removes stale WAL journals.

set -eu

if [ $# -lt 1 ]; then
    echo "Usage: $0 <path-to-backup-file> [target-db-path]" >&2
    echo "Contoh: $0 /data/backups/stockforge_backup_20261113_052500.db" >&2
    exit 1
fi

BACKUP_FILE="$1"
TARGET_DB="${2:-${DATABASE_URL:-/data/stockforge.db}}"
TARGET_DB="$(echo "$TARGET_DB" | sed -E 's|^sqlite:///*|/|')"
TARGET_DIR="$(dirname "$TARGET_DB")"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

echo "=== [RUPIKA] Memulai Prosedur Restore Database SQLite ==="
echo "File Backup    : ${BACKUP_FILE}"
echo "Target Restore : ${TARGET_DB}"

# 1. Validasi keberadaan file backup
if [ ! -f "$BACKUP_FILE" ]; then
    echo "ERROR: File backup tidak ditemukan di: ${BACKUP_FILE}" >&2
    exit 1
fi

# 2. Verifikasi integritas file backup sebelum menyentuh database aktif
echo "1. Memverifikasi integritas file backup sumber..."
if ! BACKUP_INTEGRITY="$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" 2>&1)"; then
    echo "ERROR: File backup korup atau tidak valid! Detail: ${BACKUP_INTEGRITY}" >&2
    echo "Proses restore dibatalkan demi keamanan data." >&2
    exit 2
fi

if [ "$BACKUP_INTEGRITY" != "ok" ]; then
    echo "ERROR: File backup korup atau tidak valid! Hasil: ${BACKUP_INTEGRITY}" >&2
    echo "Proses restore dibatalkan demi keamanan data." >&2
    exit 2
fi
echo "   ✓ File backup valid dan sehat."

# 3. Snapshot darurat (pre-restore backup) dari database aktif yang ada
if [ -f "$TARGET_DB" ]; then
    PRE_RESTORE_SNAP="${TARGET_DB}.pre_restore_${TIMESTAMP}.bak"
    echo "2. Membuat snapshot darurat database aktif saat ini di: ${PRE_RESTORE_SNAP}"
    cp "$TARGET_DB" "$PRE_RESTORE_SNAP"
    if [ -f "${TARGET_DB}-wal" ]; then
        cp "${TARGET_DB}-wal" "${PRE_RESTORE_SNAP}-wal" || true
    fi
    echo "   ✓ Snapshot database aktif berhasil disimpan."
fi

# 4. Melakukan restore database secara atomik
echo "3. Melakukan pemulihan file database..."
TEMP_RESTORE="${TARGET_DIR}/.stockforge_restore_${TIMESTAMP}.tmp"
cp "$BACKUP_FILE" "$TEMP_RESTORE"

# Bersihkan journal WAL usang agar tidak replay transaksi mismatched
rm -f "${TARGET_DB}-wal" "${TARGET_DB}-shm"

# Pindahkan file restore ke target aktif secara atomik
mv "$TEMP_RESTORE" "$TARGET_DB"
echo "   ✓ File database berhasil dipulihkan."

# 5. Verifikasi integritas database setelah restore
echo "4. Memverifikasi integritas database hasil restore..."
RESTORED_INTEGRITY="$(sqlite3 "$TARGET_DB" "PRAGMA integrity_check;")"
if [ "$RESTORED_INTEGRITY" != "ok" ]; then
    echo "FATAL: Database hasil restore gagal verifikasi integritas! Hasil: ${RESTORED_INTEGRITY}" >&2
    exit 3
fi

# Pastikan WAL mode aktif kembali
sqlite3 "$TARGET_DB" "PRAGMA journal_mode = WAL;" >/dev/null

echo "   ✓ Integritas database target: OK."
echo "=== [RUPIKA] Restore Selesai dengan Sukses ==="
exit 0
