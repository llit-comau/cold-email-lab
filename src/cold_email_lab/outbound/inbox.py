import email
import imaplib
import os
from datetime import datetime, timezone
from email.utils import parseaddr

from loguru import logger

from ..storage.db import (
    add_suppression,
    cancel_pending_steps,
    get_active_sequence_by_lead_email,
    get_kv,
    get_lead,
    get_step_by_message_id,
    log_event,
    save_reply,
    set_kv,
    update_lead_status,
    update_sequence_status,
)

_UNSUB_KEYWORDS = ("unsubscribe", "stop emailing", "remove me")
_BOUNCE_SENDERS = ("mailer-daemon", "postmaster")
_WATERMARK_KEY = "inbox_last_checked"

_REQUIRED_IMAP_VARS = ("IMAP_HOST", "IMAP_USER", "IMAP_PASS")


def _is_bounce(from_addr: str) -> bool:
    addr = (from_addr or "").lower()
    return any(marker in addr for marker in _BOUNCE_SENDERS)


def _has_unsubscribe_intent(body: str) -> bool:
    text = (body or "").lower()
    return any(kw in text for kw in _UNSUB_KEYWORDS)


def _extract_message_ids(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return header_value.split()


def process_incoming_message(
    from_addr: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    subject: str | None = None,
    body: str = "",
) -> str:
    """Core reply/bounce/unsubscribe handling logic, decoupled from IMAP transport.

    Matches an incoming message to a sequence via In-Reply-To/References against
    stored Message-IDs, falling back to a from-address match against active
    sequences. Returns a short outcome string: 'bounce', 'reply', 'reply+unsubscribe',
    'bounce_no_match', or 'no_match'.
    """
    from_email = parseaddr(from_addr)[1].lower()

    if _is_bounce(from_addr):
        candidate_ids = _extract_message_ids(in_reply_to) + _extract_message_ids(references)
        step = None
        for mid in candidate_ids:
            step = get_step_by_message_id(mid)
            if step is not None:
                break
        if step is None:
            logger.warning(f"Bounce from {from_addr} did not match any known sequence — ignoring")
            return "bounce_no_match"

        lead = get_lead(step["lead_id"])
        update_lead_status(step["lead_id"], "bounced")
        if lead is not None:
            add_suppression(lead["contact_email"], "bounce")
        save_reply(
            lead_id=step["lead_id"],
            sequence_id=step["sequence_id"],
            from_addr=from_addr,
            subject=subject,
            body_text=body,
            kind="bounce",
        )
        log_event("bounced", step["lead_id"], step["sequence_id"])
        logger.info(f"Lead #{step['lead_id']} marked bounced (via sequence #{step['sequence_id']})")
        return "bounce"

    candidate_ids = _extract_message_ids(in_reply_to) + _extract_message_ids(references)
    sequence_id = None
    lead_id = None
    for mid in candidate_ids:
        step = get_step_by_message_id(mid)
        if step is not None:
            sequence_id = step["sequence_id"]
            lead_id = step["lead_id"]
            break

    if sequence_id is None:
        sequence = get_active_sequence_by_lead_email(from_email)
        if sequence is not None:
            sequence_id = sequence["id"]
            lead_id = sequence["lead_id"]

    if sequence_id is None:
        logger.warning(f"Reply from {from_addr} did not match any known sequence — ignoring")
        return "no_match"

    update_sequence_status(sequence_id, "replied")
    cancel_pending_steps(sequence_id)
    update_lead_status(lead_id, "replied")
    log_event("replied", lead_id, sequence_id)
    logger.info(f"Sequence #{sequence_id} → replied (lead #{lead_id}); remaining steps cancelled")

    if _has_unsubscribe_intent(body):
        lead = get_lead(lead_id)
        if lead is not None:
            add_suppression(lead["contact_email"], "unsubscribe")
        update_lead_status(lead_id, "unsubscribed")
        save_reply(
            lead_id=lead_id,
            sequence_id=sequence_id,
            from_addr=from_addr,
            subject=subject,
            body_text=body,
            kind="unsubscribe",
        )
        log_event("unsubscribed", lead_id, sequence_id)
        logger.info(f"Lead #{lead_id} → unsubscribed (reply contained unsubscribe intent)")
        return "reply+unsubscribe"

    save_reply(
        lead_id=lead_id,
        sequence_id=sequence_id,
        from_addr=from_addr,
        subject=subject,
        body_text=body,
        kind="reply",
    )
    return "reply"


def _missing_imap_vars() -> list[str]:
    return [v for v in _REQUIRED_IMAP_VARS if not os.getenv(v)]


def check_inbox() -> dict:
    """Connect to IMAP, scan for new messages since the last watermark, and process each.

    Raises ValueError with a clear message if required IMAP env vars are missing.
    """
    missing = _missing_imap_vars()
    if missing:
        raise ValueError(
            "Cannot check inbox — missing required IMAP env var(s): "
            f"{', '.join(missing)}. Set them in .env to run `inbox check`."
        )

    host = os.environ["IMAP_HOST"]
    user = os.environ["IMAP_USER"]
    password = os.environ["IMAP_PASS"]

    watermark = get_kv(_WATERMARK_KEY) or "01-Jan-2000"

    stats = {"scanned": 0, "reply": 0, "unsubscribe": 0, "bounce": 0, "no_match": 0}

    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        conn.select("INBOX")
        typ, data = conn.search(None, f'(SINCE "{watermark}")')
        if typ != "OK":
            raise ValueError(f"IMAP search failed: {typ}")

        message_nums = data[0].split() if data and data[0] else []
        for num in message_nums:
            try:
                typ, msg_data = conn.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                from_addr = msg.get("From", "")
                subject = msg.get("Subject")
                in_reply_to = msg.get("In-Reply-To")
                references = msg.get("References")
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="replace")
                            break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode(errors="replace")

                outcome = process_incoming_message(
                    from_addr=from_addr,
                    in_reply_to=in_reply_to,
                    references=references,
                    subject=subject,
                    body=body,
                )
                stats["scanned"] += 1
                if outcome == "reply":
                    stats["reply"] += 1
                elif outcome == "reply+unsubscribe":
                    stats["reply"] += 1
                    stats["unsubscribe"] += 1
                elif outcome == "bounce":
                    stats["bounce"] += 1
                else:
                    stats["no_match"] += 1
            except Exception as exc:
                logger.exception(f"Failed to process message {num}: {exc}")

        today = datetime.now(timezone.utc).strftime("%d-%b-%Y")
        set_kv(_WATERMARK_KEY, today)
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return stats
