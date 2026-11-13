"""Telegram visual review and human approval workflow (SF-011)."""
from __future__ import annotations

import html
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
from stockforge.db.models import Asset, AssetMetadata, Job, QcResult, Review, TelegramUser, utc_now
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

    def get_pending_asset(
        self,
        asset_id: Optional[int] = None,
        job_id: Optional[int] = None,
    ) -> Optional[Asset]:
        """Fetch asset pending review by asset ID, job ID, or retrieve the next eligible asset."""
        with self.session_factory() as session:
            if asset_id is not None:
                stmt = select(Asset).where(Asset.id == asset_id)
                asset = session.scalars(stmt).first()
                if asset:
                    _ = asset.metadata_entry
                    _ = list(asset.qc_results)
                    session.expunge_all()
                    return asset

            if job_id is not None:
                stmt = (
                    select(Asset)
                    .where(
                        Asset.job_id == job_id,
                        Asset.status.in_(["qc_passed", "review_pending", "generated"]),
                    )
                    .order_by(Asset.id.desc())
                    .limit(1)
                )
                asset = session.scalars(stmt).first()
                if asset:
                    _ = asset.metadata_entry
                    _ = list(asset.qc_results)
                    session.expunge_all()
                    return asset

            if asset_id is None and job_id is None:
                stmt = (
                    select(Asset)
                    .where(Asset.status.in_(["qc_passed", "review_pending", "generated"]))
                    .order_by(Asset.id.desc())
                    .limit(1)
                )
                asset = session.scalars(stmt).first()
                if asset:
                    _ = asset.metadata_entry
                    _ = list(asset.qc_results)
                    session.expunge_all()
                    return asset

            return None

    def get_active_generation_job(self, job_id: Optional[int] = None) -> Optional[Job]:
        """Fetch active image generation job currently in progress, if any."""
        with self.session_factory() as session:
            stmt = select(Job).where(
                Job.type == "image_batch",
                Job.status.in_(["pending", "reserved", "running"]),
            )
            if job_id is not None:
                stmt = stmt.where(Job.id == job_id)
            else:
                stmt = stmt.order_by(Job.id.desc())
            job = session.scalars(stmt).first()
            if job:
                session.expunge(job)
            return job

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
        for_caption: bool = True,
    ) -> Tuple[str, InlineKeyboardMarkup]:
        """Construct formatted Telegram review summary with inline action keyboard.
        
        Guarantees caption length does not exceed Telegram 1024 character limit.
        """
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

        # Cap vision issues length to prevent Telegram 1024 caption limit rejection
        max_issues = 180 if for_caption else 800
        if len(vision_issues_str) > max_issues:
            vision_issues_str = vision_issues_str[:max_issues - 3] + "..."

        mp = (asset.width * asset.height) / 1_000_000.0
        score_display = f"{vision_score:.2f}" if vision_score is not None else "0.00"

        short_disclaimer = "Automated inspection only. Human review remains mandatory prior to agency submission."
        disclaimer_text = short_disclaimer if for_caption else VISION_QC_DISCLAIMER

        text = (
            f"🖼️ <b>Review Aset Stok #{asset.id}</b>\n\n"
            f"• <b>File</b>: <code>{html.escape(asset.filename or '')}</code>\n"
            f"• <b>Dimensi</b>: {asset.width}x{asset.height} px ({mp:.1f} MP)\n"
            f"• <b>Status</b>: <code>{html.escape(asset.status)}</code>\n"
            f"• <b>Judul</b>: {html.escape(title)}\n"
            f"• <b>Kategori</b>: {cat_id} ({html.escape(cat_name)})\n"
            f"• <b>Keywords ({keywords_count})</b>: <i>{html.escape(top_keywords_str)}</i>\n\n"
            f"📋 <b>Hasil Quality Control (QC)</b>:\n"
            f"• Det. Checks: {html.escape(qc_det_summary)}\n"
            f"• Vision QC: {'✅ Lolos' if vision_passed else '❌ Flagged'} "
            f"(Score: {score_display})\n"
            f"• Isu Visual: {html.escape(vision_issues_str)}\n\n"
            f"⚠️ <i>{html.escape(disclaimer_text)}</i>"
        )

        # Extra safety check: if caption still exceeds 980 chars, trim further
        if for_caption and len(text) > 980:
            excess = len(text) - 980
            if len(vision_issues_str) > excess + 10:
                vision_issues_str = vision_issues_str[:-excess - 10] + "..."
                text = (
                    f"🖼️ <b>Review Aset Stok #{asset.id}</b>\n\n"
                    f"• <b>File</b>: <code>{html.escape(asset.filename or '')}</code>\n"
                    f"• <b>Dimensi</b>: {asset.width}x{asset.height} px ({mp:.1f} MP)\n"
                    f"• <b>Status</b>: <code>{html.escape(asset.status)}</code>\n"
                    f"• <b>Judul</b>: {html.escape(title)}\n"
                    f"• <b>Kategori</b>: {cat_id} ({html.escape(cat_name)})\n"
                    f"• <b>Keywords ({keywords_count})</b>: <i>{html.escape(top_keywords_str)}</i>\n\n"
                    f"📋 <b>Hasil Quality Control (QC)</b>:\n"
                    f"• Det. Checks: {html.escape(qc_det_summary)}\n"
                    f"• Vision QC: {'✅ Lolos' if vision_passed else '❌ Flagged'} "
                    f"(Score: {score_display})\n"
                    f"• Isu Visual: {html.escape(vision_issues_str)}\n\n"
                    f"⚠️ <i>{html.escape(disclaimer_text)}</i>"
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
        from stockforge.db.session import create_db_engine, create_session_factory
        settings = get_settings()
        engine = create_db_engine(settings.database_url)
        _default_review_service = ReviewService(create_session_factory(engine))
    return _default_review_service


@require_admin
async def review_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /review command providing interactive visual review wizard."""
    service = get_review_service()

    target_id: Optional[int] = None
    if context.args and len(context.args) > 0 and context.args[0].isdigit():
        target_id = int(context.args[0])

    asset = service.get_pending_asset(asset_id=target_id)
    if not asset and target_id is not None:
        asset = service.get_pending_asset(job_id=target_id)

    if not asset:
        active_job = service.get_active_generation_job(job_id=target_id if target_id else None)
        if not active_job and target_id is not None:
            active_job = service.get_active_generation_job()

        if active_job:
            msg = (
                f"⏳ <b>Proses generate gambar belum selesai</b> (Batch #{active_job.id}).\n\n"
                f"Silakan periksa kemajuan dengan perintah <code>/status {active_job.id}</code>."
            )
        elif target_id:
            msg = f"Tidak ada aset #{target_id} yang dapat direview."
        else:
            msg = "Tidak ada aset yang menunggu review saat ini."

        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    token = service.generate_token(asset.id)
    summary_text, markup = service.build_review_message_and_keyboard(asset, token, for_caption=True)

    image_file = Path(asset.raw_path) if asset.raw_path else None
    if image_file and image_file.is_file():
        try:
            with open(image_file, "rb") as f:
                await update.effective_message.reply_photo(
                    photo=f,
                    caption=summary_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
            return
        except Exception as e:
            logger.warning("Gagal mengirim preview foto dengan caption penuh: %s. Mencoba foto dengan caption pendek...", e)
            try:
                short_caption = f"🖼️ <b>Review Aset Stok #{asset.id}</b> (<code>{html.escape(asset.filename or '')}</code>)"
                with open(image_file, "rb") as f:
                    await update.effective_message.reply_photo(
                        photo=f,
                        caption=short_caption,
                        parse_mode=ParseMode.HTML,
                    )
                if update.effective_message:
                    await update.effective_message.reply_text(
                        summary_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                return
            except Exception as e2:
                logger.error("Gagal mengirim preview foto fallback: %s. Mengirim teks biasa.", e2)

    # Fallback jika file fisik tidak ada atau terjadi kendala media total
    if update.effective_message:
        try:
            await update.effective_message.reply_text(
                summary_text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except Exception as e:
            logger.error("Gagal mengirim pesan review dengan HTML: %s. Mengirim teks polos.", e)
            await update.effective_message.reply_text(
                summary_text,
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

    user_label = html.escape(user_name or "Admin")
    summary_note = f"\n\n{emoji} <b>Keputusan</b>: <code>{html.escape(res.decision.upper())}</code> oleh {user_label} ({time_str} UTC)"

    try:
        # If it's a photo caption
        if query.message.caption:
            caption_html = getattr(query.message, "caption_html", None)
            base_caption = caption_html if isinstance(caption_html, str) else html.escape(query.message.caption or "")
            await query.edit_message_caption(
                caption=base_caption + summary_note,
                parse_mode=ParseMode.HTML,
                reply_markup=None,  # Disables further clicks
            )
        else:
            # If it's a text message
            text_html = getattr(query.message, "text_html", None)
            base_text = text_html if isinstance(text_html, str) else html.escape(query.message.text or "")
            await query.edit_message_text(
                text=base_text + summary_note,
                parse_mode=ParseMode.HTML,
                reply_markup=None,  # Disables further clicks
            )
    except Exception as e:
        logger.warning("Gagal mengedit pesan review: %s", e)

    await query.answer(f"Aset #{asset_id} berhasil di-{res.decision}!", show_alert=False)
