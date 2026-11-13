"""Telegram visual review and human approval workflow (SF-011)."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.bot.auth import require_admin
from stockforge.db.models import Asset, AssetMetadata, QcResult, Review, TelegramUser, utc_now
from stockforge.qc.vision import ADOBE_CATEGORIES, VISION_QC_DISCLAIMER

logger = logging.getLogger("stockforge.bot.review")

CALLBACK_TTL_SECONDS = 86400  # 24 hours callback TTL


@dataclass
class ReviewResult:
    """Outcome of attempting a review action on an asset."""
    success: bool
    asset_id: int
    decision: str
    new_status: Optional[str] = None
    already_completed: bool = False
    message: str = ""


class ReviewService:
    """Coordinates asset retrieval, review presentation, and idempotent state transitions."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def get_pending_asset(self, asset_id: Optional[int] = None) -> Optional[Asset]:
        """Fetch asset pending review by ID or retrieve the next eligible asset."""
        with self.session_factory() as session:
            if asset_id is not None:
                stmt = select(Asset).where(Asset.id == asset_id)
            else:
                stmt = (
                    select(Asset)
                    .where(Asset.status.in_(["qc_passed", "review_pending", "generated"]))
                    .order_by(Asset.id.asc())
                    .limit(1)
                )
            asset = session.scalars(stmt).first()
            if asset:
                # Eagerly load relationships before leaving session
                _ = asset.metadata_entry
                _ = list(asset.qc_results)
                session.expunge_all()
            return asset

    @staticmethod
    def generate_token(asset_id: int) -> str:
        """Generate a timestamped token for callback TTL verification."""
        ts = int(time.time())
        return f"{asset_id}_{ts}"

    @staticmethod
    def validate_token(token: str, max_age_seconds: int = CALLBACK_TTL_SECONDS) -> bool:
        """Validate whether the token has expired."""
        try:
            parts = token.split("_")
            if len(parts) < 2:
                return False
            ts = int(parts[1])
            now = int(time.time())
            if now < ts:  # Future timestamp anomaly
                return False
            return (now - ts) <= max_age_seconds
        except Exception:
            return False

    def build_review_message_and_keyboard(
        self,
        asset: Asset,
        token: str,
    ) -> Tuple[str, InlineKeyboardMarkup]:
        """Construct formatted Telegram review summary with inline action keyboard."""
        meta = asset.metadata_entry
        qc_list = asset.qc_results or []

        # 1. Metadata info
        title = meta.title if meta else (asset.filename or f"Asset #{asset.id}")
        cat_id = meta.category_id if meta else None
        cat_name = ADOBE_CATEGORIES.get(cat_id, "Belum ditentukan") if cat_id else "Belum ditentukan"

        keywords_count = 0
        top_keywords_str = "-"
        if meta and meta.keywords_json:
            try:
                kw_list = json.loads(meta.keywords_json)
                keywords_count = len(kw_list)
                top_keywords_str = ", ".join(kw_list[:6])
                if len(kw_list) > 6:
                    top_keywords_str += f" (+{len(kw_list) - 6} lainnya)"
            except Exception:
                pass

        # 2. QC Findings
        det_passed = True
        det_checks: List[str] = []
        vision_passed = True
        vision_score: Optional[float] = None
        vision_issues: List[str] = []

        for qc in qc_list:
            if qc.check_name == "vision_qc":
                vision_passed = qc.passed
                vision_score = qc.score
                try:
                    det = json.loads(qc.details_json)
                    vision_issues = det.get("issues", [])
                except Exception:
                    pass
            else:
                if not qc.passed:
                    det_passed = False
                det_checks.append(f"{qc.check_name}: {'✅' if qc.passed else '❌'}")

        qc_det_summary = ", ".join(det_checks) if det_checks else "Belum diperiksa"
        vision_issues_str = ", ".join(vision_issues) if vision_issues else "Tidak ada"

        mp = (asset.width * asset.height) / 1_000_000.0

        score_display = f"{vision_score:.2f}" if vision_score is not None else "0.00"

        text = (
            f"🖼️ **Review Aset Stok #{asset.id}**\n\n"
            f"• **File**: `{asset.filename}`\n"
            f"• **Dimensi**: {asset.width}x{asset.height} px ({mp:.1f} MP)\n"
            f"• **Status**: `{asset.status}`\n"
            f"• **Judul**: {title}\n"
            f"• **Kategori**: {cat_id} ({cat_name})\n"
            f"• **Keywords ({keywords_count})**: _{top_keywords_str}_\n\n"
            f"📋 **Hasil Quality Control (QC)**:\n"
            f"• Det. Checks: {qc_det_summary}\n"
            f"• Vision QC: {'✅ Lolos' if vision_passed else '❌ Flagged'} "
            f"(Score: {score_display})\n"
            f"• Isu Visual: {vision_issues_str}\n\n"
            f"⚠️ *{VISION_QC_DISCLAIMER}*"
        )


        keyboard = [
            [
                InlineKeyboardButton("✅ Setujui (Approve)", callback_data=f"rev:app:{asset.id}:{token}"),
                InlineKeyboardButton("❌ Tolak (Reject)", callback_data=f"rev:rej:{asset.id}:{token}"),
            ],
            [
                InlineKeyboardButton("🔄 Regenerasi", callback_data=f"rev:reg:{asset.id}:{token}"),
                InlineKeyboardButton("🔍 Minta Gambar Full-Res", callback_data=f"rev:res:{asset.id}:{token}"),
            ],
        ]
        return text, InlineKeyboardMarkup(keyboard)

    def process_review_action(
        self,
        asset_id: int,
        action: str,
        telegram_user_id: int,
        user_name: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> ReviewResult:
        """Process review action idempotently preventing duplicate state transitions."""
        valid_actions = {"app": "approved", "rej": "rejected", "reg": "regenerate"}
        if action not in valid_actions:
            return ReviewResult(
                success=False,
                asset_id=asset_id,
                decision=action,
                message=f"Aksi '{action}' tidak dikenal",
            )

        target_decision = valid_actions[action]

        with self.session_factory() as session:
            # 1. Ensure telegram_user exists for FK constraints
            stmt = select(TelegramUser).where(TelegramUser.telegram_id == telegram_user_id)
            user_rec = session.scalars(stmt).first()
            if not user_rec:
                user_rec = TelegramUser(
                    telegram_id=telegram_user_id,
                    role="admin",
                    enabled=True,
                )
                session.add(user_rec)
                session.flush()

            # 2. Retrieve asset
            asset = session.get(Asset, asset_id)
            if not asset:
                return ReviewResult(
                    success=False,
                    asset_id=asset_id,
                    decision=target_decision,
                    message=f"Aset #{asset_id} tidak ditemukan",
                )

            # 3. State machine & idempotency check (CRITICAL: double-click prevention)
            if asset.status == "approved":
                return ReviewResult(
                    success=False,
                    asset_id=asset_id,
                    decision=target_decision,
                    new_status="approved",
                    already_completed=True,
                    message=f"Aset #{asset_id} sudah disetujui (APPROVED) sebelumnya.",
                )
            elif asset.status == "rejected":
                return ReviewResult(
                    success=False,
                    asset_id=asset_id,
                    decision=target_decision,
                    new_status="rejected",
                    already_completed=True,
                    message=f"Aset #{asset_id} sudah ditolak (REJECTED) sebelumnya.",
                )
            elif asset.status == "regenerating":
                return ReviewResult(
                    success=False,
                    asset_id=asset_id,
                    decision=target_decision,
                    new_status="regenerating",
                    already_completed=True,
                    message=f"Aset #{asset_id} sedang dalam proses regenerasi.",
                )

            # 4. Apply status transition
            now = utc_now()
            if action == "app":
                asset.status = "approved"
                if asset.metadata_entry:
                    asset.metadata_entry.metadata_reviewed_at = now
            elif action == "rej":
                asset.status = "rejected"
            elif action == "reg":
                asset.status = "regenerating"

            # 5. Persist review record
            review_entry = Review(
                asset_id=asset_id,
                telegram_user_id=user_rec.id,
                decision=target_decision,
                notes=notes or f"Review action '{target_decision}' via Telegram review wizard",
                created_at=now,
            )

            session.add(review_entry)
            session.commit()

            logger.info(
                "Aset #%d berhasil di-%s oleh Admin ID %d: status='%s'",
                asset_id,
                target_decision,
                telegram_user_id,
                asset.status,
            )
            return ReviewResult(
                success=True,
                asset_id=asset_id,
                decision=target_decision,
                new_status=asset.status,
                already_completed=False,
                message=f"Aset #{asset_id} berhasil di-{target_decision}!",
            )


# Default module-level service instance (initialized on demand or injected)
_default_review_service: Optional[ReviewService] = None


def get_review_service(session_factory: Optional[Callable[[], Session]] = None) -> ReviewService:
    global _default_review_service
    if session_factory is not None:
        _default_review_service = ReviewService(session_factory)
    elif _default_review_service is None:
        from stockforge.config import get_settings
        from stockforge.db.session import create_session_factory
        settings = get_settings()
        _default_review_service = ReviewService(create_session_factory(settings.database_url))
    return _default_review_service


@require_admin
async def review_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /review command providing interactive visual review wizard."""
    service = get_review_service()

    target_id: Optional[int] = None
    if context.args and len(context.args) > 0 and context.args[0].isdigit():
        target_id = int(context.args[0])

    asset = service.get_pending_asset(asset_id=target_id)
    if not asset:
        msg = (
            f"Tidak ada aset #{target_id} yang dapat direview."
            if target_id
            else "Tidak ada aset yang menunggu review saat ini."
        )
        if update.effective_message:
            await update.effective_message.reply_text(msg)
        return

    token = service.generate_token(asset.id)
    summary_text, markup = service.build_review_message_and_keyboard(asset, token)

    image_file = Path(asset.raw_path) if asset.raw_path else None
    if image_file and image_file.is_file():
        try:
            with open(image_file, "rb") as f:
                await update.effective_message.reply_photo(
                    photo=f,
                    caption=summary_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=markup,
                )
            return
        except Exception as e:
            logger.warning("Gagal mengirim preview foto: %s. Mengirim teks biasa.", e)

    # Fallback jika file fisik tidak ada atau terjadi kendala media
    if update.effective_message:
        await update.effective_message.reply_text(
            summary_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )


@require_admin
async def review_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks for review actions and full-res requests."""
    query = update.callback_query
    if not query or not query.data:
        return

    # Acknowledge callback immediately
    try:
        await query.answer()
    except Exception:
        pass

    raw_data = query.data
    # Format: rev:<action>:<asset_id>:<token>
    parts = raw_data.split(":")
    if len(parts) != 4 or parts[0] != "rev":
        return

    _, action, asset_id_str, token = parts
    if not asset_id_str.isdigit():
        return
    asset_id = int(asset_id_str)

    service = get_review_service()

    # 1. Validate callback token TTL
    if not service.validate_token(token):
        await query.answer(
            "⏳ Sesi review ini telah kedaluwarsa (TTL habis). Silakan ketik /review lagi.",
            show_alert=True,
        )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    user = update.effective_user
    user_id = user.id if user else 0
    user_name = user.first_name if user else "Admin"

    # 2. Full-resolution image request
    if action == "res":
        asset = service.get_pending_asset(asset_id=asset_id)
        if not asset or not asset.raw_path or not Path(asset.raw_path).is_file():
            await query.answer("File resolusi penuh tidak ditemukan di server.", show_alert=True)
            return

        await query.answer("Mengirimkan file gambar resolusi penuh...")
        try:
            with open(asset.raw_path, "rb") as f:
                await query.message.reply_document(
                    document=f,
                    filename=asset.filename,
                    caption=f"File resolusi penuh untuk Aset #{asset.id} ({asset.width}x{asset.height} px)",
                )
        except Exception as e:
            logger.error("Gagal mengirim dokumen full-res: %s", e)
            await query.message.reply_text(f"Gagal mengirim file full-res: {e}")
        return

    # 3. Process review decision (approve, reject, regenerate)
    res = service.process_review_action(
        asset_id=asset_id,
        action=action,
        telegram_user_id=user_id,
        user_name=user_name,
    )

    if not res.success:
        if res.already_completed:
            # Double click or repeated click
            await query.answer(
                f"⚠️ Aksi diabaikan: {res.message}",
                show_alert=True,
            )
            try:
                # Remove inline buttons to prevent further confusion
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        else:
            await query.answer(f"Gagal: {res.message}", show_alert=True)
        return

    # Successful state transition
    decision_emojis = {"approved": "✅", "rejected": "❌", "regenerate": "🔄"}
    emoji = decision_emojis.get(res.decision, "🏁")
    time_str = utc_now().strftime("%Y-%m-%d %H:%M:%S")

    summary_note = f"\n\n{emoji} **Keputusan**: `{res.decision.upper()}` oleh {user_name} ({time_str} UTC)"

    try:
        # If it's a photo caption
        if query.message.caption:
            await query.edit_message_caption(
                caption=query.message.caption + summary_note,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=None,  # Disables further clicks
            )
        else:
            # If it's a text message
            await query.edit_message_text(
                text=query.message.text + summary_note,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=None,  # Disables further clicks
            )
    except Exception as e:
        logger.warning("Gagal mengedit pesan review: %s", e)

    await query.answer(f"Aset #{asset_id} berhasil di-{res.decision}!", show_alert=False)
