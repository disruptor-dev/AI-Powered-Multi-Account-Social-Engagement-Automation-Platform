"""Redis-backed worker for idempotent social comment synchronization jobs."""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
import uuid
from typing import Any

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.redis_client import get_redis_client
from app.models.comment_sync_run import CommentSyncRun
from app.models.ai_reply_draft import AIReplyDraft
from app.models.comment_automation_rule import CommentAutomationRun
from app.services.comment_publish_jobs import enqueue_due_comment_publish_batches
from app.services.ai_drafting import process_ai_reply_draft, enqueue_due_ai_drafts
from app.services.comment_automation import process_comment_automation_run, enqueue_due_automation_rule_runs
from app.services.comment_publishing import process_comment_publish_batch
from app.services.comment_jobs import DEAD_LETTER_QUEUE, QUEUE_NAME, enqueue_dead_letter, enqueue_due_account_syncs
from app.services.comment_sync import run_comment_sync
from app.services.email_delivery import process_one_email_outbox
from app.models.analytics_sync_run import AnalyticsSyncRun
from app.services.analytics import enqueue_due_analytics_syncs, process_analytics_sync_run
from app.services.youtube_comment_queue import enqueue_due_youtube_comment_generations, process_due_scheduled_youtube_comments, process_youtube_comment_generation

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("social_platform.worker")
shutdown_requested = False


def request_shutdown(signum: int, frame: Any) -> None:
    global shutdown_requested
    shutdown_requested = True
    logger.info("Shutdown signal received: %s", signum)


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _dispatch(payload: object, client: Redis) -> None:
    if not isinstance(payload, dict):
        enqueue_dead_letter(client, payload, "job_payload_not_object")
        return
    job_type = payload.get("type")
    if job_type == "ping":
        logger.info("Worker ping job received: id=%s", payload.get("job_id", "n/a"))
        return
    if job_type == "youtube_comment_generate":
        queue_item_id = _uuid(payload.get("queue_item_id"))
        workspace_id = _uuid(payload.get("workspace_id"))
        if queue_item_id is None or workspace_id is None:
            enqueue_dead_letter(client, payload, "invalid_youtube_comment_queue_ids")
            return
        db: Session = SessionLocal()
        try:
            process_youtube_comment_generation(db, queue_item_id, workspace_id)
        except Exception:
            logger.exception("Worker could not generate YouTube top-level comment draft id=%s", queue_item_id)
            db.rollback()
        finally:
            db.close()
        return

    if job_type == "comment_ai_draft":
        draft_id = _uuid(payload.get("draft_id"))
        workspace_id = _uuid(payload.get("workspace_id"))
        if draft_id is None or workspace_id is None:
            enqueue_dead_letter(client, payload, "invalid_ai_draft_ids")
            return
        db: Session = SessionLocal()
        try:
            process_ai_reply_draft(db, draft_id, workspace_id)
        except Exception:
            logger.exception("Worker could not dispatch AI reply draft id=%s", draft_id)
            db.rollback()
        finally:
            db.close()
        return

    if job_type == "analytics_sync":
        run_id = _uuid(payload.get("run_id"))
        account_id = _uuid(payload.get("account_id"))
        workspace_id = _uuid(payload.get("workspace_id"))
        if run_id is None or account_id is None or workspace_id is None:
            enqueue_dead_letter(client, payload, "invalid_analytics_sync_ids")
            return
        db: Session = SessionLocal()
        try:
            run = db.get(AnalyticsSyncRun, run_id)
            if run is None or run.workspace_id != workspace_id:
                logger.warning("Discarded analytics job without matching run: id=%s", run_id)
                return
            if run.status not in {"queued", "running"}:
                logger.info("Ignored already-finished analytics job: id=%s status=%s", run_id, run.status)
                return
            asyncio.run(process_analytics_sync_run(db, run_id, account_id))
        except Exception:
            logger.exception("Worker could not dispatch analytics sync run id=%s", run_id)
            db.rollback()
        finally:
            db.close()
        return

    if job_type == "comment_automation_run":
        run_id = _uuid(payload.get("run_id"))
        workspace_id = _uuid(payload.get("workspace_id"))
        if run_id is None or workspace_id is None:
            enqueue_dead_letter(client, payload, "invalid_automation_run_ids")
            return
        db: Session = SessionLocal()
        try:
            process_comment_automation_run(db, run_id, workspace_id)
        except Exception:
            logger.exception("Worker could not dispatch automation run id=%s", run_id)
            db.rollback()
        finally:
            db.close()
        return

    if job_type == "comment_publish_batch":
        batch_id = _uuid(payload.get("batch_id"))
        workspace_id = _uuid(payload.get("workspace_id"))
        if batch_id is None or workspace_id is None:
            enqueue_dead_letter(client, payload, "invalid_publish_batch_ids")
            return
        db: Session = SessionLocal()
        try:
            process_comment_publish_batch(db, batch_id, workspace_id)
        except Exception:
            logger.exception("Worker could not dispatch comment publish batch id=%s", batch_id)
            db.rollback()
        finally:
            db.close()
        return

    if job_type != "comment_sync":
        enqueue_dead_letter(client, payload, "unsupported_job_type")
        logger.warning("Moved unsupported job type to dead-letter queue: type=%r", job_type)
        return

    run_id = _uuid(payload.get("run_id"))
    account_id = _uuid(payload.get("account_id"))
    if run_id is None or account_id is None:
        enqueue_dead_letter(client, payload, "invalid_sync_job_ids")
        return

    db: Session = SessionLocal()
    try:
        run = db.get(CommentSyncRun, run_id)
        if run is None:
            logger.warning("Discarded queued sync job without a run record: id=%s", run_id)
            return
        if run.status not in {"queued", "running"}:
            logger.info("Ignored already-finished sync job: id=%s status=%s", run_id, run.status)
            return
        asyncio.run(run_comment_sync(db, run_id, account_id))
    except Exception:
        # A dispatch failure must not kill the worker. The run is marked failed if the
        # sync service had started; stale queued records are also recovered by the scheduler.
        logger.exception("Worker could not dispatch comment sync run id=%s", run_id)
        try:
            run = db.get(CommentSyncRun, run_id)
            if run is not None and run.status in {"queued", "running"}:
                run.status = "error"
                run.error_code = "worker_dispatch_error"
                from datetime import datetime, timezone
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


def main() -> None:
    global shutdown_requested
    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    client = get_redis_client()
    logger.info("Worker started; listening on queue '%s' and dead-letter queue '%s'", QUEUE_NAME, DEAD_LETTER_QUEUE)
    interval = max(60, settings.comment_sync_interval_seconds)
    scheduler_poll_seconds = max(10, min(60, interval // 4))
    next_schedule_at = 0.0
    next_email_poll_at = 0.0

    while not shutdown_requested:
        now = time.monotonic()
        if now >= next_schedule_at:
            try:
                queued = enqueue_due_account_syncs()
                if queued:
                    logger.info("Scheduled due account comment syncs: count=%d", queued)
                legacy_retired = enqueue_due_comment_publish_batches()
                if legacy_retired:
                    logger.warning("Retired legacy multi-channel comment batches without posting: count=%d", legacy_retired)
                ai_queued = enqueue_due_ai_drafts()
                if ai_queued:
                    logger.info("Recovered queued AI reply drafts: count=%d", ai_queued)
                automation_queued = enqueue_due_automation_rule_runs()
                if automation_queued:
                    logger.info("Queued/recovered comment automation runs: count=%d", automation_queued)
                analytics_queued = enqueue_due_analytics_syncs()
                if analytics_queued:
                    logger.info("Queued scheduled analytics sync runs: count=%d", analytics_queued)
                video_drafts_queued = enqueue_due_youtube_comment_generations()
                if video_drafts_queued:
                    logger.info("Recovered YouTube video-comment draft jobs: count=%d", video_drafts_queued)
                with SessionLocal() as youtube_queue_db:
                    due_published = process_due_scheduled_youtube_comments(youtube_queue_db, limit=1)
                if due_published:
                    logger.info("Processed scheduled, human-approved YouTube comments: count=%d", due_published)
            except Exception:
                logger.exception("Could not schedule due comment syncs")
            next_schedule_at = now + scheduler_poll_seconds

        if now >= next_email_poll_at:
            try:
                with SessionLocal() as email_db:
                    email_processed = process_one_email_outbox(email_db)
                if email_processed:
                    logger.debug("Processed one transactional email outbox record")
            except Exception:
                logger.exception("Could not process transactional email outbox")
            next_email_poll_at = now + max(2, min(30, int(settings.email_outbox_poll_seconds)))

        try:
            item = client.brpop(QUEUE_NAME, timeout=3)
            if item is None:
                continue
            _, raw_payload = item
            try:
                payload = json.loads(raw_payload)
            except (json.JSONDecodeError, TypeError):
                enqueue_dead_letter(client, str(raw_payload)[:5000], "malformed_job_json")
                logger.warning("Moved malformed job JSON to dead-letter queue")
                continue
            _dispatch(payload, client)
        except RedisError:
            logger.exception("Redis unavailable; retrying after a short delay")
            time.sleep(2)
        except Exception:
            logger.exception("Unexpected worker loop error")
            time.sleep(1)

    try:
        client.close()
    except Exception:
        pass
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
