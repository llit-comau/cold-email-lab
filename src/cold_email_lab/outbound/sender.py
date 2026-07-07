import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from loguru import logger

from ..storage.db import (
    approve_sequence_steps,
    approve_step as db_approve_step,
    count_steps_sent_between,
    get_sequence,
    get_sequence_step,
    get_kv,
    get_snoozed_leads_due,
    is_suppressed,
    list_draft_steps,
    list_due_steps,
    set_kv,
    update_step_content,
    update_step_status,
)

DEFAULT_UNSUBSCRIBE_TEXT = (
    "If you'd rather not hear from me, just reply 'unsubscribe' and I won't email again."
)

_REQUIRED_SMTP_VARS = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_FROM_EMAIL")
_FIRST_LIVE_SEND_KEY = "send_first_live_date"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _parse_positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _parse_warmup_schedule() -> list[int] | None:
    raw = os.getenv("SEND_WARMUP_SCHEDULE")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("SEND_WARMUP_SCHEDULE must contain at least one positive integer")
    return [_parse_positive_int(part, "SEND_WARMUP_SCHEDULE") for part in parts]


def get_send_cap_status() -> dict:
    """Current UTC-day send cap, including optional warm-up schedule."""
    today_start = _today_utc()
    tomorrow_start = today_start + timedelta(days=1)
    sent_today = count_steps_sent_between(today_start.isoformat(), tomorrow_start.isoformat())

    schedule = _parse_warmup_schedule()
    first_live_date = get_kv(_FIRST_LIVE_SEND_KEY)
    if schedule:
        anchor = datetime.fromisoformat(first_live_date).date() if first_live_date else today_start.date()
        week_index = max(0, (today_start.date() - anchor).days // 7)
        cap = schedule[min(week_index, len(schedule) - 1)]
        source = "warm-up"
    else:
        cap = _parse_positive_int(os.getenv("SEND_DAILY_CAP", "10"), "SEND_DAILY_CAP")
        source = "daily"

    return {
        "cap": cap,
        "sent_today": sent_today,
        "remaining": max(0, cap - sent_today),
        "source": source,
        "first_live_date": first_live_date,
        "today": today_start.date().isoformat(),
    }


def review_steps(sequence_id: int | None = None) -> list:
    """Return draft steps (optionally filtered to one sequence), joined with lead/sequence info."""
    return list_draft_steps(sequence_id=sequence_id)


def approve_step(step_id: int) -> bool:
    """Approve a single draft step. Returns True if it was approved (False if not found/not draft)."""
    step = get_sequence_step(step_id)
    if step is None:
        raise ValueError(f"Step #{step_id} not found")
    if step["status"] != "draft":
        raise ValueError(f"Step #{step_id} is '{step['status']}', not 'draft' — nothing to approve")
    return db_approve_step(step_id)


def approve_sequence(sequence_id: int) -> int:
    """Approve all draft steps in a sequence. Returns count approved."""
    sequence = get_sequence(sequence_id)
    if sequence is None:
        raise ValueError(f"Sequence #{sequence_id} not found")
    return approve_sequence_steps(sequence_id)


def edit_step(step_id: int, subject: str | None = None, body: str | None = None) -> None:
    step = get_sequence_step(step_id)
    if step is None:
        raise ValueError(f"Step #{step_id} not found")
    if step["status"] != "draft":
        raise ValueError(f"Step #{step_id} is '{step['status']}' — only draft steps can be edited")
    if subject is None and body is None:
        raise ValueError("Provide --subject and/or --body")
    update_step_content(step_id, subject=subject, body=body)


def _compose_body(body: str) -> str:
    """Append signature + unsubscribe footer per compliance rules."""
    signature = os.getenv("OUTBOUND_SIGNATURE") or os.getenv("SMTP_FROM_NAME") or ""
    unsubscribe = os.getenv("UNSUBSCRIBE_TEXT") or DEFAULT_UNSUBSCRIBE_TEXT

    parts = [body.rstrip()]
    if signature:
        parts.append(signature.strip())
    parts.append(unsubscribe.strip())
    return "\n\n".join(parts)


def get_due_sendable_steps() -> list:
    """Approved, due steps in active sequences, excluding suppressed recipients."""
    due = list_due_steps(_now_iso())
    return [step for step in due if not is_suppressed(step["contact_email"])]


def _missing_smtp_vars() -> list[str]:
    return [v for v in _REQUIRED_SMTP_VARS if not os.getenv(v)]


def send_tick(live: bool = False) -> list[dict]:
    """Find due, approved steps and either print them (dry-run) or send them (--live).

    Dry-run never touches the database or a network socket. --live refuses up front
    with a clear error if required SMTP env vars are missing — nothing is sent or
    marked in that case. In --live mode, a failure sending one message is logged and
    skipped; it never aborts the rest of the batch.
    """
    cap = get_send_cap_status()
    due_resurface = get_snoozed_leads_due(cap["today"])
    due_steps = get_due_sendable_steps()
    results: list[dict] = []

    if due_resurface:
        results.append(
            {
                "kind": "resurface_notice",
                "status": "resurface-due",
                "count": len(due_resurface),
                "leads": [
                    {
                        "lead_id": lead["id"],
                        "company_name": lead["company_name"],
                        "contact_email": lead["contact_email"],
                        "snooze_until": lead["snooze_until"],
                    }
                    for lead in due_resurface
                ],
            }
        )

    if not due_steps:
        return results

    remaining = cap["remaining"]
    sendable_steps = due_steps[:remaining]
    deferred_steps = due_steps[remaining:]

    if live and sendable_steps:
        missing = _missing_smtp_vars()
        if missing:
            raise ValueError(
                "Cannot send live — missing required SMTP env var(s): "
                f"{', '.join(missing)}. Set them in .env, or omit --live for a dry-run."
            )

    should_anchor_warmup = live and cap["source"] == "warm-up" and cap["first_live_date"] is None
    warmup_anchored = False
    smtp_conn = None
    if live and sendable_steps:
        host = os.environ["SMTP_HOST"]
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.environ["SMTP_USER"]
        password = os.environ["SMTP_PASS"]
        try:
            smtp_conn = smtplib.SMTP(host, port, timeout=30)
            smtp_conn.starttls()
            smtp_conn.login(user, password)
        except Exception as exc:
            logger.exception(f"Failed to connect/authenticate to SMTP server: {exc}")
            raise ValueError(f"Could not connect to SMTP server: {exc}") from exc

    try:
        for step in sendable_steps:
            full_body = _compose_body(step["body"])
            entry = {
                "kind": "step",
                "step_id": step["id"],
                "sequence_id": step["sequence_id"],
                "company_name": step["company_name"],
                "to": step["contact_email"],
                "subject": step["subject"],
                "body": full_body,
                "status": "dry-run",
                "cap": cap,
            }

            if not live:
                results.append(entry)
                continue

            from_name = os.getenv("SMTP_FROM_NAME", "")
            from_email = os.environ["SMTP_FROM_EMAIL"]
            try:
                msg = EmailMessage()
                msg["Subject"] = step["subject"]
                msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
                msg["To"] = step["contact_email"]
                message_id = make_msgid()
                msg["Message-ID"] = message_id
                msg.set_content(full_body)

                smtp_conn.send_message(msg)

                update_step_status(
                    step["id"], "sent", sent_at=_now_iso(), smtp_message_id=message_id
                )
                if should_anchor_warmup and not warmup_anchored:
                    set_kv(_FIRST_LIVE_SEND_KEY, cap["today"])
                    warmup_anchored = True
                entry["status"] = "sent"
                entry["smtp_message_id"] = message_id
                logger.info(f"Sent step #{step['id']} to {step['contact_email']} ({message_id})")
            except Exception as exc:
                logger.exception(f"Failed to send step #{step['id']}: {exc}")
                entry["status"] = "failed"
                entry["error"] = str(exc)

            results.append(entry)

        for step in deferred_steps:
            results.append(
                {
                    "kind": "step",
                    "step_id": step["id"],
                    "sequence_id": step["sequence_id"],
                    "company_name": step["company_name"],
                    "to": step["contact_email"],
                    "subject": step["subject"],
                    "body": _compose_body(step["body"]) if not live else "",
                    "status": "deferred",
                    "reason": "daily cap",
                    "cap": cap,
                }
            )
    finally:
        if smtp_conn is not None:
            try:
                smtp_conn.quit()
            except Exception:
                pass

    return results
