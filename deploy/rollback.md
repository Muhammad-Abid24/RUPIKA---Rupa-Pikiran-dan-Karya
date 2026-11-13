# Panduan Operasional Rollback RUPIKA (SF-014C)

Dokumen ini menjelaskan prosedur resmi untuk melakukan rollback versi aplikasi dan pemulihan data pada environment localhost macOS (Docker Desktop) maupun deployment server VPS.

---

## ⚠️ PERINGATAN KESELAMATAN DATA (CRITICAL INVARIANT)

> [!CAUTION]
> **JANGAN PERNAH MENJALANKAN PERINTAH `docker compose down -v`**  
> Flag `-v` atau `--volumes` akan **MENGHAPUS SEMUA DATA PERSISTENT SECARA PERMANEN**, termasuk database SQLite `/data/stockforge.db`, file aset foto mentah di `/data/raw/`, dan riwayat transaksi ledger anggaran.
> Untuk menghentikan container, **HANYA GUNAKAN `docker compose down` TANPA FLAG `-v`**.

---

## 1. Skenario Rollback Image Aplikasi (Code / Container Rollback)

Gunakan prosedur ini apabila versi rilis baru mengalami error aplikasi atau kegagalan runtime, namun skema basis data masih kompatibel dengan versi sebelumnya.

### Langkah-langkah:

1. **Hentikan Layanan Container yang Sedang Berjalan (Tanpa Flag `-v`):**
   ```bash
   docker compose down
   ```

2. **Buat Snapshot Cadangan Darurat (Opsional tapi Direkomendasikan):**
   ```bash
   docker compose run --rm --entrypoint /bin/sh bot /app/deploy/backup.sh
   ```

3. **Revert Tag Image atau Commit Git ke Versi Sebelumnya:**
   - **Metode Git Revert / Checkout Commit Stabil:**
     ```bash
     git checkout <stable-commit-hash>
     ```
   - **Metode Tag Image di `compose.yaml`:**
     Ubah `image: rupika:latest` menjadi `image: rupika:<previous-stable-tag>`.

4. **Rebuild atau Pull Image Versi Sebelumnya:**
   ```bash
   docker compose build
   ```

5. **Nyalakan Kembali Layanan:**
   ```bash
   docker compose up -d bot worker
   ```

6. **Verifikasi Status Kesehatan:**
   ```bash
   docker compose run --rm bot doctor
   docker compose logs --tail=50 bot worker
   ```

---

## 2. Skenario Rollback Basis Data (Database Rollback)

Gunakan prosedur ini apabila terjadi migrasi skema yang bermasalah atau korupsi data.

### Skenario A: Rollback Skema Kompatibel via Alembic
Jika migrasi mendukung downgrade tanpa kehilangan data kritis:
```bash
docker compose run --rm bot alembic downgrade -1
```

### Skenario B: Pemulihan Penuh dari Snapshot Backup
Jika terjadi korupsi data atau migrasi irreversible:

1. **Hentikan Container Worker dan Bot:**
   ```bash
   docker compose down
   ```

2. **Pilih File Backup yang Sehat:**
   Periksa daftar backup yang tersedia di named volume:
   ```bash
   docker compose run --rm --entrypoint ls bot -la /data/backups/
   ```

3. **Jalankan Prosedur Restore Aman:**
   ```bash
   docker compose run --rm --entrypoint /bin/sh bot /app/deploy/restore.sh /data/backups/stockforge_backup_<TIMESTAMP>.db
   ```
   *Script `deploy/restore.sh` secara otomatis memvalidasi integritas backup, membuat snapshot darurat, membersihkan berkas jurnal WAL lama, dan memverifikasi integritas hasil restore.*

4. **Jalankan Kembali Layanan dan Verifikasi:**
   ```bash
   docker compose up -d bot worker
   docker compose run --rm bot doctor
   ```

---

## 3. Checklist Verifikasi Pasca-Rollback

- [ ] Perintah `docker compose run --rm bot doctor` memberikan output **semua OK** dengan kode keluar `0`.
- [ ] Database SQLite berada dalam mode WAL:
  ```bash
  docker compose run --rm --entrypoint sqlite3 bot /data/stockforge.db "PRAGMA journal_mode;"
  # Hasil yang diharapkan: wal
  ```
- [ ] Integritas SQLite valid dan bebas korupsi:
  ```bash
  docker compose run --rm --entrypoint sqlite3 bot /data/stockforge.db "PRAGMA integrity_check;"
  # Hasil yang diharapkan: ok
  ```
- [ ] File-file aset di `/data/raw/` dapat diakses dan sesuai dengan SHA-256 yang tercatat di tabel `assets`.
- [ ] Service `bot` dan `worker` berjalan stabil tanpa crash loop atau error HTTP 409 pada polling Telegram.
