"""Worker process claiming jobs and processing pipeline items safely (SF-004)."""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Callable, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.db.models import Job, JobItem, utc_now
from stockforge.providers.resilience import AmbiguousTimeoutError
from stockforge.queue.service import QueueService
from stockforge.queue.tracker import ItemProgressTracker

logger = logging.getLogger("stockforge.queue.worker")


class Worker:
    """Sequential job worker executing pipeline stages outside long DB transactions.
    
    Invariants:
    - Never holds a database lock during network API calls.
    - Performs periodic lease heartbeats.
    - Honors cooperative cancellation: stops when job status is 'cancelled'.
    - Separate from Telegram bot process.
    """

    def __init__(
        self,
        queue_service: QueueService,
        worker_id: Optional[str] = None,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int = 60,
    ) -> None:
        self.queue_service = queue_service
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.poll_interval = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self.is_running = False
        self.current_job_id: Optional[int] = None

    def check_is_cancelled(self, job_id: int) -> bool:
        """Check if job has received cooperative cancellation."""
        with Session(self.queue_service.engine) as session:
            status = session.scalar(select(Job.status).where(Job.id == job_id))
            return status == "cancelled"

    def _default_pipeline_item_processor(self, job_id: int, item_index: int) -> None:
        """Execute default image pipeline: generation -> QC -> metadata -> review."""
        from stockforge.config import get_settings
        from stockforge.storage.service import StorageService
        from stockforge.images.service import ImageGenerationService
        from stockforge.providers.registry import ProviderRegistry
        from stockforge.providers.router import ProviderRouter
        from stockforge.db.models import ProductionPlan
        from pathlib import Path
        import json
        import asyncio

        settings = get_settings()
        data_dir = Path(settings.data_dir).resolve()
        storage = StorageService(data_dir)

        # Load router
        router = None
        for cfg_path in [Path("config/models.yaml"), Path("/app/config/models.yaml")]:
            if cfg_path.exists():
                reg = ProviderRegistry()
                r = ProviderRouter(reg)
                try:
                    r.load_from_yaml(cfg_path)
                    router = r
                    break
                except Exception:
                    pass

        # Resolve prompt from JobEvent (e.g. regeneration_request), Concept, or ProductionPlan
        prompt = f"Commercial stock photo concept #{item_index}"
        with Session(self.queue_service.engine) as session:
            # 1. Check JobEvent for explicit prompt (e.g. regeneration request)
            from stockforge.db.models import JobEvent, Concept
            regen_event = session.scalar(
                select(JobEvent)
                .where(
                    JobEvent.job_id == job_id,
                    JobEvent.event_type.in_(["regeneration_request", "prompt_override"]),
                )
                .order_by(JobEvent.id.desc())
            )
            if regen_event and regen_event.payload_json:
                try:
                    pdata = json.loads(regen_event.payload_json)
                    if pdata.get("prompt"):
                        prompt = pdata["prompt"]
                except Exception as ex:
                    logger.warning("Gagal mem-parse prompt dari JobEvent: %s", ex)

            # 2. Check Concept linked to Job
            if prompt.startswith("Commercial stock photo concept #"):
                job_rec = session.get(Job, job_id)
                if job_rec and job_rec.concept_id:
                    concept = session.get(Concept, job_rec.concept_id)
                    if concept and concept.prompt:
                        prompt = concept.prompt

            # 3. Check ProductionPlan
            if prompt.startswith("Commercial stock photo concept #"):
                plan = session.scalar(select(ProductionPlan).where(ProductionPlan.consumed_job_id == job_id))
                if plan and plan.concepts_json:
                    try:
                        concepts = json.loads(plan.concepts_json)
                        idx = item_index - 1
                        if 0 <= idx < len(concepts):
                            prompt = concepts[idx].get("prompt") or concepts[idx].get("title") or prompt
                    except Exception as ex:
                        logger.warning("Gagal mem-parse concepts_json dari plan #%s: %s", plan.id, ex)

        image_service = ImageGenerationService(
            session_factory=lambda: Session(self.queue_service.engine),
            storage_service=storage,
            router=router,
            license_evidence="adobe_stock_commercial_sublicense_v1",
        )

        # Check if an asset was already generated for this item (e.g. resumption after worker crash during QC or metadata)
        from stockforge.db.models import Asset, JobItem
        created_asset_id: Optional[int] = None
        with Session(self.queue_service.engine) as session:
            existing_item = session.scalar(
                select(JobItem).where(JobItem.job_id == job_id, JobItem.item_index == item_index)
            )
            if existing_item and existing_item.asset_id:
                asset_obj = session.get(Asset, existing_item.asset_id)
                if asset_obj:
                    created_asset_id = asset_obj.id
                    logger.info(
                        "Melanjutkan pemrosesan item #%d pada Job #%d dengan aset yang sudah ada (Asset #%d)",
                        item_index,
                        job_id,
                        created_asset_id,
                    )

        if not created_asset_id:
            # Generate single image
            batch_res = asyncio.run(image_service.generate_batch(prompts=[prompt], max_batch_size=1, job_id=job_id))
            if batch_res.failure_count > 0:
                err_msg = batch_res.failed_items[0].get("error", "Generasi gambar gagal")
                raise RuntimeError(err_msg)
            if batch_res.successful_assets:
                created_asset_id = batch_res.successful_assets[0].id

        if created_asset_id:
            tracker = ItemProgressTracker(lambda: Session(self.queue_service.engine))

            # 1. Update stage to QC and publish live progress
            tracker.update_item_stage(job_id, item_index, stage="qc", status="processing", asset_id=created_asset_id)
            self._publish_progress(job_id, force=True)

            # Run deterministic QC automatically
            try:
                from stockforge.qc.deterministic import DeterministicQcService
                qc_svc = DeterministicQcService(session_factory=lambda: Session(self.queue_service.engine))
                qc_svc.inspect_asset(created_asset_id)
            except Exception as qc_ex:
                logger.warning("Deterministic QC gagal untuk asset #%s: %s", created_asset_id, qc_ex)

            # 2. Update stage to metadata and publish live progress if router is configured
            if router:
                tracker.update_item_stage(job_id, item_index, stage="metadata", status="processing", asset_id=created_asset_id)
                self._publish_progress(job_id, force=True)
                try:
                    from stockforge.qc.vision import VisionQcService
                    vision_svc = VisionQcService(
                        session_factory=lambda: Session(self.queue_service.engine),
                        storage_service=storage,
                        router_or_provider=router,
                    )
                    v_res = asyncio.run(vision_svc.inspect_image_vision(created_asset_id))
                    asyncio.run(vision_svc.generate_metadata(created_asset_id))
                    if not v_res.passed:
                        logger.warning("Fail-closed: Asset #%s ditolak oleh Vision QC: %s", created_asset_id, v_res.details_json)
                except Exception as vis_ex:
                    logger.warning("Vision QC / Metadata gagal untuk asset #%s: %s. Menetapkan status qc_failed (Fail-Closed).", created_asset_id, vis_ex)
                    with Session(self.queue_service.engine) as fail_sess:
                        fa = fail_sess.get(Asset, created_asset_id)
                        if fa:
                            fa.status = "qc_failed"
                            fail_sess.commit()

            return created_asset_id
        return None

    def _post_telegram(
        self,
        token: str,
        method: str,
        payload: dict,
        timeout_secs: float = 25.0,
        max_retries: int = 2,
    ) -> Optional[Any]:
        """Post to Telegram API with connection reuse, timeouts, and transient retry."""
        import ssl
        import time
        import httpx

        url = f"https://api.telegram.org/bot{token}/{method}"
        timeout = httpx.Timeout(connect=20.0, read=timeout_secs, write=20.0, pool=20.0)
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=timeout, http2=False) as client:
                    resp = client.post(url, json=payload)
                    return resp
            except (httpx.TimeoutException, httpx.NetworkError, ssl.SSLError) as ex:
                if attempt < max_retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                logger.warning("Gagal memanggil Telegram %s (attempt %d/%d): %s", method, attempt + 1, max_retries + 1, ex)
                return None
            except Exception as ex:
                err_str = str(ex).lower()
                if ("remoteprotocolerror" in err_str or "server disconnected" in err_str or "eof" in err_str) and attempt < max_retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                logger.warning("Ekspepsi tak terduga memanggil Telegram %s: %s", method, ex)
                return None
        return None

    def _send_telegram_failure_alert(
        self,
        job_id: int,
        item_index: int,
        stage: str,
        error_msg: str,
    ) -> None:
        """Send immediate failure alert to Telegram admin(s)."""
        import html
        from stockforge.config import get_settings

        try:
            settings = get_settings()
            token = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else None
            admin_ids = settings.admin_ids_list
            if not token or not admin_ids:
                return

            alert_text = (
                f"❌ <b>Kegagalan Produksi Gambar (Job #{job_id})</b>\n\n"
                f"• <b>Item:</b> Gambar #{item_index}\n"
                f"• <b>Tahap:</b> <code>{html.escape(stage.upper())}</code>\n"
                f"• <b>Penyebab Error:</b>\n<code>{html.escape(error_msg[:500])}</code>\n\n"
                f"💡 <i>Gunakan perintah <code>/status {job_id}</code> untuk status batch atau <code>/failed {job_id}</code> untuk laporan lengkap.</i>"
            )

            for admin_id in admin_ids:
                try:
                    resp = self._post_telegram(
                        token=token,
                        method="sendMessage",
                        payload={
                            "chat_id": admin_id,
                            "text": alert_text,
                            "parse_mode": "HTML",
                        },
                    )
                    if resp is not None and resp.status_code == 200:
                        logger.info("Notifikasi error Telegram berhasil dikirim ke admin #%s untuk job #%s", admin_id, job_id)
                    elif resp is not None:
                        logger.warning("Telegram API mengembalikan status %s saat kirim alert: %s", resp.status_code, resp.text)
                except Exception as ex:
                    logger.warning("Gagal mengirim notifikasi error ke Telegram admin #%s: %s", admin_id, ex)
        except Exception as top_ex:
            logger.warning("Ekspepsi saat menyiapkan alert Telegram: %s", top_ex)

    def _publish_progress(
        self,
        job_id: int,
        force: bool = False,
        notify_completion: bool = False,
    ) -> None:
        """Publish or update live progress message in Telegram in real-time."""
        from datetime import timezone
        from stockforge.config import get_settings
        from stockforge.bot.progress import compute_render_hash, format_progress_message
        from stockforge.db.models import TelegramProgressMessage, Job, utc_now
        from stockforge.queue.tracker import ItemProgressTracker

        try:
            settings = get_settings()
            token = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else None
            admin_ids = settings.admin_ids_list
            if not token or not admin_ids:
                return

            tracker = ItemProgressTracker(lambda: Session(self.queue_service.engine))
            summary = tracker.get_batch_progress(job_id)

            with Session(self.queue_service.engine) as session:
                job = session.get(Job, job_id)
                job_type = job.type if job else "image_batch"
                cost_cents = job.reserved_usd_cents if job else 0
                record = session.get(TelegramProgressMessage, job_id)

                text = format_progress_message(
                    summary=summary,
                    job_type=job_type,
                    estimated_cost_cents=cost_cents,
                )
                current_hash = compute_render_hash(text)
                now = utc_now()

                if record:
                    # Skip if text unchanged and not force
                    if record.last_render_hash == current_hash and not force:
                        pass
                    else:
                        rec_time = record.last_updated_at
                        if rec_time.tzinfo is None:
                            rec_time = rec_time.replace(tzinfo=timezone.utc)
                        elapsed = (now - rec_time).total_seconds()

                        # Edit message if force, terminal, or past throttle interval
                        if force or summary.is_terminal or elapsed >= 2.0:
                            try:
                                resp = self._post_telegram(
                                    token=token,
                                    method="editMessageText",
                                    payload={
                                        "chat_id": record.chat_id,
                                        "message_id": record.message_id,
                                        "text": text,
                                        "parse_mode": "HTML",
                                    },
                                )
                                if resp is not None and resp.status_code == 200:
                                    record.last_render_hash = current_hash
                                    record.last_updated_at = now
                                    session.commit()
                                elif resp is not None:
                                    logger.warning("Telegram editMessageText returned %s: %s", resp.status_code, resp.text)
                            except Exception as ex:
                                logger.warning("Gagal edit live progress Telegram: %s", ex)
                else:
                    # Send initial message to primary admin chat
                    target_chat = admin_ids[0]
                    try:
                        resp = self._post_telegram(
                            token=token,
                            method="sendMessage",
                            payload={
                                "chat_id": target_chat,
                                "text": text,
                                "parse_mode": "HTML",
                            },
                        )
                        if resp is not None and resp.status_code == 200:
                            data = resp.json()
                            msg_id = data.get("result", {}).get("message_id")
                            if msg_id:
                                new_rec = TelegramProgressMessage(
                                    job_id=job_id,
                                    chat_id=target_chat,
                                    message_id=msg_id,
                                    last_render_hash=current_hash,
                                    last_updated_at=now,
                                )
                                session.add(new_rec)
                                session.commit()
                        elif resp is not None:
                            logger.warning("Telegram sendMessage returned %s: %s", resp.status_code, resp.text)
                    except Exception as ex:
                        logger.warning("Gagal kirim live progress awal Telegram: %s", ex)

                # Send completion notice when batch finishes completely (strictly idempotent: at most once per job)
                if summary.is_terminal and notify_completion:
                    import json
                    from stockforge.db.models import JobEvent
                    already_notified = session.scalar(
                        select(JobEvent).where(
                            JobEvent.job_id == job_id,
                            JobEvent.event_type == "completion_notification_sent",
                        )
                    )
                    if not already_notified:
                        target_chat = record.chat_id if record else admin_ids[0]
                        try:
                            if summary.completed_count > 0:
                                completion_text = (
                                    f"🎉 <b>Batch #{job_id} Selesai Diproduksi!</b>\n\n"
                                    f"• <b>Total Gambar:</b> {summary.total_items}\n"
                                    f"• <b>Berhasil:</b> {summary.completed_count}\n"
                                    f"• <b>Status:</b> Siap untuk ditinjau visual\n\n"
                                    f"👉 Ketik <code>/review {job_id}</code> untuk memulai review aset stok."
                                )
                            else:
                                completion_text = (
                                    f"⚠️ <b>Batch #{job_id} Telah Berakhir</b>\n\n"
                                    f"• <b>Gagal:</b> {summary.failed_count} dari {summary.total_items} gambar\n"
                                    f"Gunakan <code>/failed {job_id}</code> untuk rincian error."
                                )
                            resp = self._post_telegram(
                                token=token,
                                method="sendMessage",
                                payload={
                                    "chat_id": target_chat,
                                    "text": completion_text,
                                    "parse_mode": "HTML",
                                },
                            )
                            if resp is not None and resp.status_code == 200:
                                ev = JobEvent(
                                    job_id=job_id,
                                    event_type="completion_notification_sent",
                                    payload_json=json.dumps({"chat_id": target_chat, "at": now.isoformat()}),
                                    created_at=now,
                                )
                                session.add(ev)
                                session.commit()
                        except Exception as ex:
                            logger.warning("Gagal kirim notifikasi batch selesai: %s", ex)
        except Exception as top_ex:
            logger.warning("Ekspepsi saat publish progress Telegram: %s", top_ex)

    def execute_job_items(
        self,
        job_id: int,
        item_processor: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """Process items belonging to a claimed job with durable stage tracking.
        
        Guarantees:
        - Accurately tracks per-item stages before and after external calls.
        - Preserves progress across crashes; never reruns completed images.
        - Returns final status: 'completed', 'completed_with_errors', or 'cancelled'.
        """
        tracker = ItemProgressTracker(lambda: Session(self.queue_service.engine))

        # Check existing items or prepare resumption plan
        with Session(self.queue_service.engine) as session:
            job = session.get(Job, job_id)
            if not job:
                return "failed"
            has_items = len(job.items) > 0

        # If job had no items created yet, initialize item 1
        if not has_items:
            tracker.initialize_job_items(job_id, total_items=1, initial_stage="generating")

        # Plan which items need execution (excluding completed ones!)
        indices_to_run = tracker.prepare_resumption_plan(job_id)

        any_failure = False
        for idx in indices_to_run:
            # Check cancellation before starting next item
            if self.check_is_cancelled(job_id):
                logger.info("Pekerjaan %s dibatalkan secara kooperatif oleh operator", job_id)
                tracker.update_item_stage(job_id, idx, stage="generating", status="cancelled")
                return "cancelled"

            # Heartbeat to extend lease
            self.queue_service.heartbeat(job_id, self.worker_id, extend_seconds=self.lease_seconds)

            # Persist stage update BEFORE external execution
            tracker.update_item_stage(job_id, idx, stage="generating", status="processing")
            self._publish_progress(job_id, force=True)

            try:
                # Network / processing happens outside database transaction!
                asset_id = None
                if item_processor:
                    asset_id = item_processor(job_id, idx)
                else:
                    asset_id = self._default_pipeline_item_processor(job_id, idx)

                # Persist stage update AFTER successful execution
                tracker.update_item_stage(job_id, idx, stage="generating", status="completed", asset_id=asset_id)
                self._publish_progress(job_id, force=True)
            except AmbiguousTimeoutError as ate:
                logger.warning("Item %s pada job %s mengalami ambiguous timeout: %s", idx, job_id, ate)
                tracker.update_item_stage(
                    job_id,
                    idx,
                    stage="generating",
                    status="needs_reconciliation",
                    last_error=str(ate),
                )
                self._send_telegram_failure_alert(job_id, idx, "generating", str(ate))
            except Exception as e:
                logger.error("Gagal memproses item %s pada job %s: %s", idx, job_id, e)
                tracker.update_item_stage(
                    job_id,
                    idx,
                    stage="generating",
                    status="failed",
                    last_error=str(e),
                )
                self._send_telegram_failure_alert(job_id, idx, "generating", str(e))

        progress = tracker.get_batch_progress(job_id)
        self._publish_progress(job_id, force=True, notify_completion=False)
        if progress.reconciliation_count > 0:
            return "needs_reconciliation"
        if progress.completed_count == 0 and progress.failed_count > 0:
            return "failed"
        if progress.failed_count > 0:
            return "completed_with_errors"
        return "completed"


    def run_once(
        self,
        item_processor: Optional[Callable[[int, int], None]] = None,
    ) -> Optional[int]:
        """Attempt to claim and process exactly one job.
        
        Returns job_id if processed, None if queue was empty.
        """
        job = self.queue_service.claim_job(self.worker_id, lease_seconds=self.lease_seconds)
        if not job:
            return None

        job_id = job.id
        self.current_job_id = job_id
        logger.info("Worker %s memulai pemrosesan job %s (%s)", self.worker_id, job_id, job.type)

        final_status = "failed"
        try:
            final_status = self.execute_job_items(job_id, item_processor=item_processor)
        except Exception as e:
            logger.error("Ekspepsi fatal saat eksekusi job %s: %s", job_id, e, exc_info=True)
            final_status = "failed"
        finally:
            if final_status != "cancelled":
                self.queue_service.complete_job(job_id, self.worker_id, status=final_status)
                self._publish_progress(job_id, force=True, notify_completion=True)
            self.current_job_id = None

        logger.info("Worker %s menyelesaikan job %s dengan status: %s", self.worker_id, job_id, final_status)
        return job_id

    def run(self) -> None:
        """Start worker loop polling the queue until stopped."""
        self.is_running = True
        logger.info("Worker %s aktif dan memantau antrean...", self.worker_id)
        while self.is_running:
            try:
                claimed = self.run_once()
                if not claimed:
                    time.sleep(self.poll_interval)
            except Exception as e:
                logger.error("Galat pada loop worker %s: %s", self.worker_id, e)
                time.sleep(self.poll_interval)

    def stop(self) -> None:
        """Signal worker to stop after current job finishes."""
        self.is_running = False
        logger.info("Worker %s menerima sinyal penghentian", self.worker_id)

