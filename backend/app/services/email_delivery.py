"""Encrypted transactional-email outbox with bounded SMTP retries.

The outbox stores recipients and routing metadata in clear text but encrypts the
full message payload (including one-time links) with TOKEN_ENCRYPTION_KEY. SMTP
credentials and raw one-time tokens are never logged or included in status APIs.
"""
from __future__ import annotations

import json
import logging
import smtplib
import socket
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import decrypt_secret, encrypt_secret
from app.models.email_outbox import EmailOutbox

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def queue_transactional_email(
    db: Session,
    *,
    kind: str,
    recipient: str,
    subject: str,
    text_body: str,
    workspace_id: uuid.UUID | None = None,
) -> EmailOutbox:
    """Queue mail without committing, so callers can commit token + message atomically."""
    settings = get_settings()
    if not settings.smtp_configured:
        raise RuntimeError("smtp_not_configured")
    payload = json.dumps(
        {"subject": subject[:200], "text_body": text_body[:20000]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    encrypted = encrypt_secret(payload, settings)
    if not encrypted:
        raise RuntimeError("email_payload_encryption_unavailable")
    row = EmailOutbox(
        workspace_id=workspace_id,
        kind=kind[:60],
        recipient=recipient.strip().lower()[:320],
        encrypted_payload=encrypted,
        status="pending",
        attempts=0,
        next_attempt_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def _send_smtp(recipient: str, subject: str, text_body: str) -> None:
    settings = get_settings()
    if not settings.smtp_configured:
        raise RuntimeError("smtp_not_configured")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{settings.smtp_from_name.strip() or 'Socialdesk'} <{settings.smtp_from_email.strip()}>"
    message["To"] = recipient
    message.set_content(text_body)

    context = ssl.create_default_context()
    if settings.smtp_use_ssl:
        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=min(60.0, max(2.0, settings.smtp_timeout_seconds)),
            context=context,
        ) as client:
            if settings.smtp_username.strip():
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=min(60.0, max(2.0, settings.smtp_timeout_seconds))) as client:
        client.ehlo()
        if settings.smtp_starttls:
            client.starttls(context=context)
            client.ehlo()
        if settings.smtp_username.strip():
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


def process_one_email_outbox(db: Session) -> bool:
    """Deliver at most one due message. Returns whether any record was processed."""
    settings = get_settings()
    if not settings.smtp_configured:
        return False
    now = utcnow()
    stale_before = now - timedelta(minutes=10)

    # Recover records left locked by a worker that died during SMTP handling.
    stale = db.scalars(select(EmailOutbox).where(
        EmailOutbox.status == "sending",
        EmailOutbox.locked_at.is_not(None),
        EmailOutbox.locked_at < stale_before,
    ).limit(10)).all()
    for item in stale:
        item.status = "pending"
        item.locked_at = None
        item.last_error_code = "worker_recovered"
    if stale:
        db.commit()

    statement = (
        select(EmailOutbox)
        .where(EmailOutbox.status == "pending", EmailOutbox.next_attempt_at <= now)
        .order_by(EmailOutbox.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    row = db.scalar(statement)
    if row is None:
        return False

    row.status = "sending"
    row.locked_at = now
    row.attempts = int(row.attempts or 0) + 1
    db.commit()
    db.refresh(row)

    try:
        raw_payload = decrypt_secret(row.encrypted_payload, settings)
        if not raw_payload:
            raise RuntimeError("email_payload_unavailable")
        payload: Any = json.loads(raw_payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("subject"), str) or not isinstance(payload.get("text_body"), str):
            raise RuntimeError("email_payload_invalid")
        _send_smtp(row.recipient, payload["subject"], payload["text_body"])
    except Exception as exc:
        db.refresh(row)
        row.locked_at = None
        row.last_error_code = _error_code(exc)
        if row.attempts >= max(1, settings.email_outbox_max_attempts):
            row.status = "failed"
        else:
            row.status = "pending"
            # Exponential delay capped at one hour: 10s, 20s, 40s, ...
            delay = min(3600, 10 * (2 ** max(0, row.attempts - 1)))
            row.next_attempt_at = utcnow() + timedelta(seconds=delay)
        db.commit()
        logger.warning("transactional_email_failed id=%s kind=%s attempt=%d error_code=%s", row.id, row.kind, row.attempts, row.last_error_code)
        return True

    db.refresh(row)
    row.status = "sent"
    row.sent_at = utcnow()
    row.locked_at = None
    row.last_error_code = None
    db.commit()
    logger.info("transactional_email_sent id=%s kind=%s", row.id, row.kind)
    return True


def _error_code(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "smtp_timeout"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "smtp_authentication_failed"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "smtp_recipient_refused"
    if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
        return "email_payload_invalid"
    if isinstance(exc, RuntimeError) and str(exc) in {
        "email_payload_unavailable", "email_payload_invalid", "smtp_not_configured", "email_payload_encryption_unavailable"
    }:
        return str(exc)
    return "smtp_delivery_failed"
