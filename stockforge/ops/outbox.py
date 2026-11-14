"""Telegram outbox event dispatcher with retry and anti-storm throttling (SF-013)."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.db.models import TelegramOutbox, utc_now

logger = logging.getLogger("stockforge.ops.outbox")

MAX_OUTBOX_ATTEMPTS = 5
DEDUPLICATION_WINDOW_SECONDS = 300  # 5 minutes suppression for duplicate alerts


@dataclass
class OutboxDispatchReport:
    """Outcome report for an outbox processing run."""
    total_processed: int
    sent_count: int
    failed_count: int
    suppressed_count: int


class OutboxService:
    """Manages transactional outbox storage and safe, throttled Telegram delivery."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        suppression_window_seconds: int = DEDUPLICATION_WINDOW_SECONDS,
        max_attempts: int = MAX_OUTBOX_ATTEMPTS,
    ) -> None:
        self.session_factory = session_factory
        self.suppression_window_seconds = suppression_window_seconds
        self.max_attempts = max_attempts
        # In-memory tracking of recent alert dispatches: alert_category -> timestamp
        self._recent_alerts: Dict[str, float] = {}

    def enqueue_event(
        self,
        chat_id: int,
        event_key: str,
        payload: Dict[str, Any],
    ) -> Tuple[TelegramOutbox, bool]:
        """Enqueue an event into telegram_outbox table idempotently.
        
        Returns:
            (outbox_record, is_newly_created)
        """
        with self.session_factory() as session:
            stmt = select(TelegramOutbox).where(TelegramOutbox.event_key == event_key)
            existing = session.scalars(stmt).first()
            if existing:
                return existing, False

            outbox = TelegramOutbox(
                chat_id=chat_id,
                event_key=event_key,
                payload_json=json.dumps(payload),
                attempts=0,
                sent_at=None,
                created_at=utc_now(),
            )
            session.add(outbox)
            session.commit()
            session.refresh(outbox)
            logger.info("Outbox event '%s' berhasil di-enqueue untuk chat_id=%d", event_key, chat_id)
            return outbox, True

    async def process_outbox(
        self,
        send_message_fn: Callable[[int, str], Any],
        max_batch: int = 10,
    ) -> OutboxDispatchReport:
        """Process pending outbox events with exponential backoff and anti-storm throttling.
        
        Guarantees:
        - Exponential backoff with jitter on repeated delivery failures.
        - Suppresses notification storms if identical alert category is repeated within window.
        - Updates sent_at timestamp and attempts count.
        """
        sent = 0
        failed = 0
        suppressed = 0

        with self.session_factory() as session:
            now = utc_now()
            # Select unsent events where attempts < max_attempts
            stmt = (
                select(TelegramOutbox)
                .where(
                    TelegramOutbox.sent_at == None,
                    TelegramOutbox.attempts < self.max_attempts,
                )
                .order_by(TelegramOutbox.created_at.asc())
                .limit(max_batch)
            )
            records = list(session.scalars(stmt).all())

            for record in records:
                try:
                    payload = json.loads(record.payload_json)
                except Exception:
                    payload = {"text": record.payload_json}

                text = str(payload.get("text", record.payload_json))
                alert_category = str(payload.get("category", record.event_key.split(":")[0]))

                # Anti-storm throttling: suppress rapid duplicate notifications
                now_ts = time.time()
                last_sent = self._recent_alerts.get(alert_category, 0.0)
                if alert_category != "general" and (now_ts - last_sent) < self.suppression_window_seconds:
                    logger.warning(
                        "Anti-storm throttling: notifikasi kategori '%s' disupresi (dikirim %.1f dtk yang lalu)",
                        alert_category,
                        now_ts - last_sent,
                    )
                    # Mark as delivered/suppressed to prevent queue blockage
                    record.sent_at = now
                    suppressed += 1
                    continue

                # Backoff calculation for retries: 2^attempts with jitter
                if record.attempts > 0:
                    delay = min(60.0, (2 ** record.attempts) + random.uniform(0.1, 1.0))
                    # Check if enough time elapsed since creation
                    age_seconds = (now - record.created_at).total_seconds()
                    if age_seconds < delay:
                        continue  # Not ready for retry yet

                # Attempt delivery
                try:
                    res = send_message_fn(record.chat_id, text)
                    if asyncio.iscoroutine(res):
                        await res

                    record.sent_at = utc_now()
                    record.attempts += 1
                    self._recent_alerts[alert_category] = time.time()
                    sent += 1
                    logger.info("Outbox event '%s' berhasil dikirim ke %d", record.event_key, record.chat_id)
                except Exception as e:
                    record.attempts += 1
                    failed += 1
                    logger.error(
                        "Gagal mengirim outbox event '%s' (upaya #%d): %s",
                        record.event_key,
                        record.attempts,
                        e,
                    )

            session.commit()

        return OutboxDispatchReport(
            total_processed=len(records),
            sent_count=sent,
            failed_count=failed,
            suppressed_count=suppressed,
        )

