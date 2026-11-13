"""Telegram visual review and human approval workflow (SF-011)."""
from __future__ import annotations

import html
import io
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
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
    new_job_id: Optional[int] = None


class ReviewService:
    """Coordinates asset retrieval, review presentation, and idempotent state transitions."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        queue_service: Optional[Any] = None,
    ) -> None:
        self.session_factory = session_factory
        self.queue_service = queue_service

    def _get_queue_service(self, session: Session) -> Optional[Any]:
        """Get or lazily create QueueService from database session bind."""
        if self.queue_service is not None:
            return self.queue_service
        try:
            bind = session.get_bind()
            if bind:
                from stockforge.queue.service import QueueService
                self.queue_service = QueueService(engine=bind)
                return self.queue_service
        except Exception as ex:
            logger.warning("Tidak dapat menginisialisasi QueueService otomatis: %s", ex)
        return None

    def _resolve_prompt_for_asset(self, session: Session, asset: Asset) -> str:
        """Resolve concept prompt from previous JobEvent, ProductionPlan, Concept, or metadata."""
        from stockforge.db.models import Job, JobItem, JobEvent, Concept, ProductionPlan
        prompt: Optional[str] = None

        # 1. Check previous JobEvent (if this asset was already generated from a prompt override or regen)
        if asset.job_id:
            regen_ev = session.scalar(
                select(JobEvent)
                .where(
                    JobEvent.job_id == asset.job_id,
                    JobEvent.event_type.in_(["regeneration_request", "prompt_override"]),
                )
                .order_by(JobEvent.id.desc())
            )
            if regen_ev and regen_ev.payload_json:
                try:
                    pdata = json.loads(regen_ev.payload_json)
                    if pdata.get("prompt"):
                        prompt = pdata["prompt"]
                except Exception:
                    pass

        # 2. Check ProductionPlan via JobItem
        if not prompt and asset.job_id:
            job_item = session.scalar(
                select(JobItem).where(JobItem.job_id == asset.job_id, JobItem.asset_id == asset.id)
            )
            plan = session.scalar(select(ProductionPlan).where(ProductionPlan.consumed_job_id == asset.job_id))
            if plan and plan.concepts_json:
                try:
                    concepts = json.loads(plan.concepts_json)
                    idx = (job_item.item_index - 1) if job_item else 0
                    if 0 <= idx < len(concepts):
                        prompt = concepts[idx].get("prompt") or concepts[idx].get("title")
                except Exception:
                    pass

        # 3. Check Concept linked directly to Job
        if not prompt and asset.job_id:
            job_rec = session.get(Job, asset.job_id)
            if job_rec and job_rec.concept_id:
                conc = session.get(Concept, job_rec.concept_id)
                if conc and conc.prompt:
                    prompt = conc.prompt

        # 4. Fallback to AssetMetadata title or filename
        if not prompt:
            if asset.metadata_entry and asset.metadata_entry.title:
                prompt = f"Commercial stock photo: {asset.metadata_entry.title}"
            elif asset.filename:
                prompt = f"Commercial stock photo concept for {asset.filename}"
            else:
                prompt = f"Commercial stock photo concept #{asset.id}"

        return prompt

    def _improve_prompt_for_regeneration(self, session: Session, asset: Asset, base_prompt: str) -> str:
        """Improve concept prompt to target and rectify anatomical, quality, and IP defects."""
        qc_list = list(asset.qc_results) if asset.qc_results else []
        issues: List[str] = []
        notes = ""
        has_anatomy_issue = False
        has_hand_finger_issue = False
        has_face_issue = False
        has_interaction_issue = False
        has_blur_issue = False
        has_watermark_issue = False

        for qc in qc_list:
            if qc.details_json:
                try:
                    det = json.loads(qc.details_json)
                    iss = det.get("issues", [])
                    if isinstance(iss, list):
                        issues.extend(str(i) for i in iss)
                    notes += " " + str(det.get("notes", ""))
                    anat = det.get("anatomy", {})
                    if anat.get("hands_and_fingers_defect"):
                        has_hand_finger_issue = True
                        has_anatomy_issue = True
                    if anat.get("face_defect"):
                        has_face_issue = True
                        has_anatomy_issue = True
                    if anat.get("interaction_defect"):
                        has_interaction_issue = True
                        has_anatomy_issue = True
                except Exception:
                    pass

        combined_issues = (" ".join(issues) + " " + notes).lower()
        if any(w in combined_issues for w in ["finger", "hand", "digit", "polydactyly", "wrist"]):
            has_hand_finger_issue = True
            has_anatomy_issue = True
        if any(w in combined_issues for w in ["face", "eye", "pupil", "teeth", "mouth"]):
            has_face_issue = True
            has_anatomy_issue = True
        if any(w in combined_issues for w in ["interaction", "blend", "clipping", "overlap"]):
            has_interaction_issue = True
            has_anatomy_issue = True
        if any(w in combined_issues for w in ["blur", "soft", "resolution", "noise"]):
            has_blur_issue = True
        if any(w in combined_issues for w in ["logo", "brand", "trademark", "watermark", "text"]):
            has_watermark_issue = True

        enhancements: List[str] = []
        if has_hand_finger_issue:
            enhancements.append("perfect anatomically correct hands with exactly 5 distinct fingers, natural finger articulation and structure, no extra digits, no fused fingers")
        if has_face_issue:
            enhancements.append("realistic symmetrical facial features, coherent eye pupils and gaze, natural realistic skin texture")
        if has_interaction_issue:
            enhancements.append("clear crisp physical contact boundaries, natural physical interaction without clipping or melting overlap")
        elif has_anatomy_issue:
            enhancements.append("anatomically correct human anatomy, natural realistic proportions")

        if has_blur_issue:
            enhancements.append("razor-sharp focus, professional commercial photography, 8k resolution, crisp clean details")
        if has_watermark_issue:
            enhancements.append("clean background without any logos, trademarks, text overlays, or watermarks")

        if not enhancements:
            enhancements.append("perfect anatomically correct hands with 5 fingers, realistic facial features, sharp focus, professional commercial stock photography")

        improved = f"{base_prompt.rstrip('. ')}, {', '.join(enhancements)}"
        return improved

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
                        Asset.status.in_(["qc_passed", "review_pending", "generated", "qc_failed"]),
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
                    .where(Asset.status.in_(["qc_passed", "review_pending", "generated", "qc_failed"]))
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
        has_txt_export: bool = False,
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
        export_note = "📄 <i>Metadata lengkap & sisa keyword terlampir dalam file .txt</i>\n\n" if has_txt_export else ""

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
            f"{export_note}"
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
                    f"{export_note}"
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

    def build_metadata_export_txt(self, asset: Asset) -> Tuple[str, bool]:
        """Construct full metadata export content as formatted plain text and indicate if export is needed.

        Returns:
            Tuple[str, bool]: (txt_content, is_lengthy)
            where is_lengthy is True if keywords count > 6 or vision issues were truncated.
        """
        meta = asset.metadata_entry
        qc_list = asset.qc_results or []

        title = meta.title if meta else (asset.filename or f"Asset #{asset.id}")
        cat_id = meta.category_id if meta else None
        cat_name = ADOBE_CATEGORIES.get(cat_id, "Belum ditentukan") if cat_id else "Belum ditentukan"

        kw_list: List[str] = []
        if meta and meta.keywords_json:
            try:
                kw_list = json.loads(meta.keywords_json)
                if not isinstance(kw_list, list):
                    kw_list = [str(kw_list)]
            except Exception:
                kw_list = []

        # QC Findings
        det_checks_lines: List[str] = []
        vision_passed = True
        vision_score: Optional[float] = None
        vision_issues: List[str] = []
        vision_notes = "-"
        vision_model = "-"
        vision_checked_at = "-"
        anatomy_summary = "Normal / Tidak ada cacat anatomi terdeteksi"

        for qc in qc_list:
            if qc.check_name == "vision_qc":
                vision_passed = qc.passed
                vision_score = qc.score
                try:
                    det = json.loads(qc.details_json) if qc.details_json else {}
                    vision_issues = det.get("issues", [])
                    vision_notes = det.get("notes", "-")
                    vision_model = det.get("model", "-")
                    vision_checked_at = det.get("checked_at", str(qc.checked_at) if qc.checked_at else "-")
                    anat = det.get("anatomy", {})
                    anat_findings = []
                    if anat.get("hands_and_fingers_defect"):
                        anat_findings.append("Cacat Jari/Tangan")
                    if anat.get("face_defect"):
                        anat_findings.append("Cacat Wajah/Mata")
                    if anat.get("interaction_defect"):
                        anat_findings.append("Cacat Kontak/Interaksi")
                    if anat.get("details"):
                        anat_findings.append(anat.get("details"))
                    if anat_findings:
                        anatomy_summary = "; ".join(anat_findings)
                except Exception:
                    pass
            else:
                passed_str = "PASS" if qc.passed else "FAIL"
                det_checks_lines.append(f"  - {qc.check_name}: [{passed_str}] (Score: {qc.score:.2f})")

        mp = (asset.width * asset.height) / 1_000_000.0 if (asset.width and asset.height) else 0.0

        if kw_list:
            keywords_block = "\n".join(f"  {idx + 1}. {k}" for idx, k in enumerate(kw_list))
        else:
            keywords_block = "  (Belum ada keywords yang di-generate)"

        if vision_issues:
            issues_block = "\n".join(f"  - {iss}" for iss in vision_issues)
        else:
            issues_block = "  - Tidak ada isu teknis/hukum yang terdeteksi."

        det_block = "\n".join(det_checks_lines) if det_checks_lines else "  - Belum diperiksa"

        txt_lines = [
            "================================================================================",
            "RUPIKA / STOCKFORGE - METADATA & QUALITY CONTROL REPORT",
            f"Asset ID: #{asset.id}",
            "================================================================================",
            "",
            "[1. INFORMASI BERKAS & RESOLUSI]",
            f"Nama Berkas     : {asset.filename or '-'}",
            f"Dimensi         : {asset.width} x {asset.height} px ({mp:.2f} Megapixels)",
            f"MIME Type       : {asset.mime_type or 'image/jpeg'}",
            f"Status Aset     : {asset.status}",
            f"Waktu Dibuat    : {asset.created_at or '-'}",
            f"SHA-256 Hash    : {asset.sha256 or '-'}",
            f"Perceptual Hash : {asset.phash or '-'}",
            "",
            "[2. METADATA KOMERSIAL ADOBE STOCK]",
            f"Judul Aset      : {title}",
            f"Kategori        : {cat_id or '-'} ({cat_name})",
            f"Tipe Konten     : {meta.content_type if meta else 'photo'}",
            f"Total Keywords  : {len(kw_list)}",
            "",
            "Daftar Lengkap Keywords (Commercial Tags):",
            keywords_block,
            "",
            "[3. HASIL QUALITY CONTROL (DETERMINISTIC)]",
            det_block,
            "",
            "[4. HASIL QUALITY CONTROL (VISION AI)]",
            f"Status Vision QC: {'LOLOS' if vision_passed else 'FLAGGED / GAGAL (FAIL-CLOSED)'}",
            f"Model Vision QC : {vision_model}",
            f"Waktu Periksa   : {vision_checked_at}",
            f"Skor Kualitas   : {vision_score:.2f}" if vision_score is not None else "Skor Kualitas   : -",
            f"Evaluasi Anatomi: {anatomy_summary}",
            "Daftar Isu Visual / Legal:",
            issues_block,
            f"Catatan Analisis: {vision_notes}",
            "",
            "[5. LEGAL DISCLAIMER]",
            VISION_QC_DISCLAIMER,
            "================================================================================",
        ]

        txt_content = "\n".join(txt_lines)

        # Flagged as lengthy if keywords count exceeds 6 or vision issues exceed 1 or full text exceeds 1200 chars
        is_lengthy = len(kw_list) > 6 or len(vision_issues) > 1 or len(txt_content) > 1200
        return txt_content, is_lengthy

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
            new_job_id: Optional[int] = None

            if action == "app":
                asset.status = "approved"
                if asset.metadata_entry:
                    asset.metadata_entry.metadata_reviewed_at = now
            elif action == "rej":
                asset.status = "rejected"
            elif action == "reg":
                asset.status = "regenerating"
                # Atomically enqueue regeneration job within this same transaction
                base_prompt = self._resolve_prompt_for_asset(session, asset)
                improved_prompt = self._improve_prompt_for_regeneration(session, asset, base_prompt)
                op_id = f"regen_{asset.id}_{int(time.time())}"
                from stockforge.db.models import BudgetLedger, Job, JobEvent, JobItem
                from stockforge.budget.service import BudgetService, BudgetExceededError

                regen_cost_cents = 5  # Cost approval for regenerated asset
                budget_svc = BudgetService(session_factory=self.session_factory)
                daily_committed = budget_svc.get_daily_committed_cents(session)
                if daily_committed + regen_cost_cents > budget_svc.daily_limit_cents:
                    return ReviewResult(
                        success=False,
                        asset_id=asset_id,
                        decision=target_decision,
                        message=f"Batas anggaran harian terlampaui ({budget_svc.daily_limit_cents} sen)! Regenerasi dibatalkan.",
                    )

                new_job = Job(
                    type="image_batch",
                    operation_id=op_id,
                    created_by=user_rec.id,
                    status="pending",
                    reserved_usd_cents=regen_cost_cents,
                    attempts=0,
                    max_attempts=3,
                    created_at=now,
                    updated_at=now,
                )
                session.add(new_job)
                session.flush()
                new_job_id = new_job.id

                # Record BudgetLedger reservation
                ledger_entry = BudgetLedger(
                    job_id=new_job.id,
                    kind="reserve",
                    amount_cents=regen_cost_cents,
                    created_at=now,
                )
                session.add(ledger_entry)

                regen_event = JobEvent(
                    job_id=new_job.id,
                    event_type="regeneration_request",
                    payload_json=json.dumps({
                        "original_asset_id": asset.id,
                        "original_job_id": asset.job_id,
                        "prompt": improved_prompt,
                        "base_prompt": base_prompt,
                        "reserved_usd_cents": regen_cost_cents,
                        "notes": notes or "User requested regeneration from /review with improved prompt",
                    }),
                    created_at=now,
                )
                session.add(regen_event)

                item_rec = JobItem(
                    job_id=new_job.id,
                    item_index=1,
                    stage="generating",
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )
                session.add(item_rec)
                logger.info("Enqueued regeneration job #%d for Asset #%d with improved prompt: %s", new_job.id, asset.id, improved_prompt[:70])

            # 5. Persist review record
            review_entry = Review(
                asset_id=asset_id,
                telegram_user_id=user_rec.id,
                decision=target_decision,
                notes=notes or (
                    f"Review action '{target_decision}' via Telegram review wizard"
                    + (f" (Batch #{new_job_id})" if new_job_id else "")
                ),
                created_at=now,
            )

            session.add(review_entry)
            session.commit()

            logger.info(
                "Aset #%d berhasil di-%s oleh Admin ID %d: status='%s' (New Job: %s)",
                asset_id,
                target_decision,
                telegram_user_id,
                asset.status,
                new_job_id,
            )
            return ReviewResult(
                success=True,
                asset_id=asset_id,
                decision=target_decision,
                new_status=asset.status,
                already_completed=False,
                message=f"Aset #{asset_id} berhasil di-{target_decision}!"
                + (f" Batch regenerasi #{new_job_id} telah dibuat." if new_job_id else ""),
                new_job_id=new_job_id,
            )


# Default module-level service instance (initialized on demand or injected)
_default_review_service: Optional[ReviewService] = None


def get_review_service(
    session_factory: Optional[Callable[[], Session]] = None,
    queue_service: Optional[Any] = None,
) -> ReviewService:
    global _default_review_service
    if session_factory is not None or queue_service is not None:
        _default_review_service = ReviewService(session_factory=session_factory, queue_service=queue_service)
    elif _default_review_service is None:
        from stockforge.config import get_settings
        from stockforge.db.session import create_db_engine, create_session_factory
        from stockforge.queue.service import QueueService
        settings = get_settings()
        engine = create_db_engine(settings.database_url)
        sf = create_session_factory(engine)
        qs = QueueService(engine=engine)
        _default_review_service = ReviewService(session_factory=sf, queue_service=qs)
    return _default_review_service


@require_admin
async def review_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /review command providing interactive visual review wizard."""
    # 1. Tampilkan typing indicator ke user
    if update.effective_chat and context.bot:
        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING,
            )
        except Exception as act_err:
            logger.debug("Gagal mengirim typing chat action: %s", act_err)

    try:
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
        txt_content, is_lengthy = service.build_metadata_export_txt(asset)
        summary_text, markup = service.build_review_message_and_keyboard(
            asset, token, for_caption=True, has_txt_export=is_lengthy
        )

        photo_sent = False
        image_file = Path(asset.raw_path) if asset.raw_path else None
        if image_file and image_file.is_file():
            # Prepare an optimized preview buffer for Telegram reply_photo (max 1920px, ~200-400KB JPEG)
            # This ensures instant upload without timeouts or Telegram client render lag, while leaving the master 8 MP file intact on disk.
            preview_bio = None
            try:
                with Image.open(image_file) as img:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    orig_w, orig_h = img.size
                    max_preview = 1920
                    if max(orig_w, orig_h) > max_preview:
                        sc = max_preview / max(orig_w, orig_h)
                        nw = max(1, int(orig_w * sc))
                        nh = max(1, int(orig_h * sc))
                        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
                    preview_bio = io.BytesIO()
                    preview_bio.name = image_file.name
                    img.save(preview_bio, format="JPEG", quality=88, optimize=True)
                    preview_bio.seek(0)
            except Exception as prep_err:
                logger.warning("Gagal membuat preview buffer gambar untuk review: %s. Menggunakan berkas asli.", prep_err)
                preview_bio = None

            try:
                if preview_bio:
                    await update.effective_message.reply_photo(
                        photo=preview_bio,
                        caption=summary_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                else:
                    with open(image_file, "rb") as f:
                        await update.effective_message.reply_photo(
                            photo=f,
                            caption=summary_text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=markup,
                        )
                photo_sent = True
            except Exception as e:
                logger.warning("Gagal mengirim preview foto dengan caption penuh: %s. Mencoba foto dengan caption pendek...", e)
                try:
                    short_caption = f"🖼️ <b>Review Aset Stok #{asset.id}</b> (<code>{html.escape(asset.filename or '')}</code>)"
                    if preview_bio:
                        preview_bio.seek(0)
                        await update.effective_message.reply_photo(
                            photo=preview_bio,
                            caption=short_caption,
                            parse_mode=ParseMode.HTML,
                        )
                    else:
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
                    photo_sent = True
                except Exception as e2:
                    logger.error("Gagal mengirim preview foto fallback: %s. Mengirim teks biasa.", e2)

        # Fallback jika file fisik tidak ada atau terjadi kendala media total
        if not photo_sent and update.effective_message:
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

        # Jika metadata terlalu panjang, ekspor sisanya / lengkap dalam bentuk file .txt
        if is_lengthy and update.effective_message:
            try:
                bio = io.BytesIO(txt_content.encode("utf-8"))
                bio.name = f"metadata_asset_{asset.id}.txt"
                caption_doc = f"📄 Metadata lengkap & sisa keywords untuk Aset #{asset.id} (.txt)"
                await update.effective_message.reply_document(
                    document=bio,
                    filename=f"metadata_asset_{asset.id}.txt",
                    caption=caption_doc,
                )
            except Exception as doc_err:
                logger.error("Gagal mengirim lampiran dokumen metadata .txt: %s", doc_err)

    except Exception as err:
        logger.error("Terjadi galat saat memproses review aset: %s", err, exc_info=True)
        err_msg = (
            f"❌ <b>Terjadi galat saat memproses review aset</b>\n\n"
            f"• <b>Penyebab</b>: <code>{html.escape(str(err))}</code>\n\n"
            f"Silakan periksa log sistem atau coba kembali beberapa saat lagi."
        )
        if update.effective_message:
            try:
                await update.effective_message.reply_text(err_msg, parse_mode=ParseMode.HTML)
            except Exception:
                await update.effective_message.reply_text(f"❌ Terjadi kesalahan pada /review: {err}")


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
    job_suffix = f" (Batch Baru #{res.new_job_id})" if res.new_job_id else ""
    summary_note = f"\n\n{emoji} <b>Keputusan</b>: <code>{html.escape(res.decision.upper())}{job_suffix}</code> oleh {user_label} ({time_str} UTC)"

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

    # Kirim konfirmasi detail jika regenerasi berhasil di-enqueue
    if res.decision == "regenerate" and res.new_job_id and query.message:
        try:
            regen_text = (
                f"🔄 <b>Proses Regenerasi Dimulai untuk Aset #{asset_id}</b>\n\n"
                f"• <b>Job Baru:</b> Batch #{res.new_job_id}\n"
                f"• <b>Status:</b> Antrean produksi gambar baru\n\n"
                f"💡 <i>Gunakan perintah <code>/status {res.new_job_id}</code> untuk memantau kemajuan atau tunggu notifikasi saat gambar selesai dibuat.</i>"
            )
            await query.message.reply_text(regen_text, parse_mode=ParseMode.HTML)
        except Exception as rx:
            logger.warning("Gagal mengirim pesan konfirmasi regenerasi: %s", rx)

    ans_msg = (
        f"Regenerasi Aset #{asset_id} dimulai (Batch #{res.new_job_id})!"
        if (res.decision == "regenerate" and res.new_job_id)
        else f"Aset #{asset_id} berhasil di-{res.decision}!"
    )
    await query.answer(ans_msg, show_alert=False)
