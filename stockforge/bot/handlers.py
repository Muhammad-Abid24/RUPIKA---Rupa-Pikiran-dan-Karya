"""Telegram command handlers and menu keyboards for RUPIKA (SF-003)."""
from __future__ import annotations

import logging
from pathlib import Path
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from stockforge.bot.auth import require_admin
from stockforge.config import get_settings
from stockforge.db.session import check_sqlite_integrity, create_db_engine

logger = logging.getLogger("stockforge.bot.handlers")


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Build standardized reply keyboard for admin interactions."""
    keyboard = [
        [KeyboardButton("/research"), KeyboardButton("/jobs")],
        [KeyboardButton("/health"), KeyboardButton("/help")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


@require_admin
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command for authorized admins."""
    user = update.effective_user
    user_name = user.first_name if user else "Admin"

    text = (
        f"👋 Halo, **{user_name}**!\n\n"
        "Selamat datang di **RUPIKA** (*Rupa, Pikiran, dan Karya*).\n"
        "Pipeline produksi aset fotografi stok komersial berbasis AI 24/7.\n\n"
        "Gunakan menu di bawah atau ketik `/help` untuk panduan perintah lengkap."
    )

    if update.effective_message:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(),
        )


@require_admin
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command providing safe operational command documentation."""
    text = (
        "📖 **Panduan Perintah RUPIKA**\n\n"
        "• `/start` — Menampilkan salam pembuka dan menu interaktif.\n"
        "• `/help` — Menampilkan dokumentasi perintah operasional ini.\n"
        "• `/health` — Diagnostik konektivitas basis data dan integritas storage.\n"
        "• `/research` — Riset niche pasar komersial dan konsep aset.\n"
        "• `/jobs` — Status antrean dan kemajuan pemrosesan batch.\n\n"
        "ℹ️ *Catatan Keamanan*: RUPIKA menerapkan invarian persetujuan manusia "
        "(`REQUIRE_HUMAN_APPROVAL=True`) dan tidak mengizinkan pengunggahan otomatis "
        "ke portal Adobe Stock."
    )

    if update.effective_message:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(),
        )


@require_admin
async def health_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /health command running diagnostics on database and file storage."""
    settings = get_settings()

    # 1. Database integrity
    db_status = "OK"
    try:
        engine = create_db_engine(settings.database_url)
        is_ok, msg = check_sqlite_integrity(engine)
        if not is_ok:
            db_status = f"Peringatan: {msg}"
    except Exception as e:
        db_status = f"Gagal: {e}"

    # 2. Storage write access
    storage_status = "OK"
    data_path = Path(settings.data_dir).resolve()
    try:
        data_path.mkdir(parents=True, exist_ok=True)
        test_file = data_path / ".health_test"
        test_file.write_text("ok")
        test_file.unlink()
    except Exception as e:
        storage_status = f"Tidak dapat menulis: {e}"

    text = (
        "🩺 **Status Kesehatan Sistem RUPIKA**\n\n"
        f"• **Lingkungan**: `{settings.app_env}`\n"
        f"• **Zona Waktu**: `{settings.tz}`\n"
        f"• **Basis Data SQLite**: `{db_status}`\n"
        f"• **Penyimpanan Data**: `{storage_status}`\n"
        f"• **Batas Harian Gambar**: `{settings.max_images_per_day}` item\n"
        f"• **Anggaran Harian**: `${settings.daily_budget_usd:.2f}` USD\n"
        f"• **Persetujuan Manusia**: `{'Aktif (Wajib)' if settings.require_human_approval else 'Nonaktif'}`"
    )

    if update.effective_message:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(),
        )

