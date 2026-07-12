"""Phase 13: Autopilot orchestrator.

`run_campaign_tick()` runs the full outbound machine as one scheduled tick:
resurface -> replenish -> enrich -> auto-sequence -> send -> inbox check -> digest.

Every stage is isolated — a stage's failure is caught, logged, and recorded in
its result dict; it never prevents later stages from running. Automation
creates and sends here; it never approves — sequences created by stage (d)
have their steps left `draft` (create_sequence_for_lead's existing behaviour),
same as a human running `sequence create` by hand.
"""

import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from loguru import logger

from ..storage.db import (
    DB_PATH,
    count_draft_steps,
    count_unsequenced_leads,
    get_kv,
    get_snoozed_leads_due,
    list_leads,
    list_replies_since,
    set_kv,
    set_lead_outcome,
)
from .breaker import get_breaker_status
from .inbox import check_inbox
from .leads import enrich_lead
from .sender import send_tick
from .sequences import create_sequence_for_lead
from .sourcing import resolve_profile_for_lead, source_leads
from .stats import get_pipeline_stats

_ANGLE_COUNTER_KEY = "campaign_angle_counter"

DEFAULT_PIPELINE_FLOOR = 10
DEFAULT_SOURCE_BATCH = 5
DEFAULT_ENRICH_BATCH = 5
DEFAULT_CAMPAIGN_PROFILES = "job-tracker"

_REQUIRED_DIGEST_SMTP_VARS = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_FROM_EMAIL")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a valid integer — using default {default}")
        return default
    if value < 0:
        logger.warning(f"{name}={raw!r} must be >= 0 — using default {default}")
        return default
    return value


def _next_angle() -> int:
    """kv-backed 1->2->3->1... counter, shared across all auto-created sequences,
    for built-in A/B rotation data (Phase 13)."""
    raw = get_kv(_ANGLE_COUNTER_KEY)
    idx = int(raw) if raw else 0
    angle = (idx % 3) + 1
    set_kv(_ANGLE_COUNTER_KEY, str(idx + 1))
    return angle


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _stage_resurface() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    due = get_snoozed_leads_due(today)
    cleared = []
    for lead in due:
        set_lead_outcome(lead["id"], None)
        cleared.append({"lead_id": lead["id"], "company_name": lead["company_name"]})
        logger.info(f"[campaign tick] resurfaced lead #{lead['id']} ({lead['company_name']})")
    return {"count": len(cleared), "leads": cleared, "errors": []}


async def _stage_replenish() -> dict:
    floor = _int_env("PIPELINE_FLOOR", DEFAULT_PIPELINE_FLOOR)
    batch = _int_env("SOURCE_BATCH", DEFAULT_SOURCE_BATCH)
    profiles = [
        p.strip()
        for p in (os.getenv("CAMPAIGN_PROFILES") or DEFAULT_CAMPAIGN_PROFILES).split(",")
        if p.strip()
    ]

    unsequenced = count_unsequenced_leads()
    result = {
        "unsequenced_before": unsequenced,
        "floor": floor,
        "ran": False,
        "profiles": [],
        "errors": [],
    }
    if unsequenced >= floor:
        result["notice"] = f"{unsequenced} unsequenced lead(s) already >= floor ({floor}) — skipped sourcing."
        return result

    result["ran"] = True
    for profile in profiles:
        try:
            report = await source_leads(profile, limit=batch)
            result["profiles"].append(
                {
                    "profile": profile,
                    "discovered": report.discovered,
                    "filtered": report.filtered,
                    "inserted": report.inserted,
                    "skipped_duplicate": report.skipped_duplicate,
                    "no_email_found": report.no_email_found,
                    "notices": list(report.notices),
                }
            )
        except Exception as exc:
            logger.exception(f"[campaign tick] replenish failed for profile {profile!r}: {exc}")
            result["errors"].append(f"replenish/{profile}: {exc}")
    return result


async def _stage_enrich() -> dict:
    cap = _int_env("ENRICH_BATCH", DEFAULT_ENRICH_BATCH)
    new_leads = list_leads(status="new")

    attempted = 0
    ok = 0
    skipped_no_profile = 0
    errors: list[str] = []

    for lead in new_leads:
        if attempted >= cap:
            break
        try:
            resolve_profile_for_lead(lead["source"])
        except ValueError:
            skipped_no_profile += 1
            continue

        attempted += 1
        try:
            await enrich_lead(lead["id"])
            ok += 1
        except Exception as exc:
            # GLM (or any provider) can return malformed JSON, hit a validation error,
            # etc. — never let one bad lead crash the tick; record and move on.
            logger.exception(f"[campaign tick] enrich failed for lead #{lead['id']}: {exc}")
            errors.append(f"enrich/lead#{lead['id']}: {exc}")

    return {
        "cap": cap,
        "attempted": attempted,
        "ok": ok,
        "skipped_no_profile": skipped_no_profile,
        "errors": errors,
    }


async def _stage_sequence() -> dict:
    """Auto-create sequences for enriched leads that don't have one yet, rotating
    angles 1->2->3 for built-in A/B data. Steps stay draft — automation never approves."""
    enriched_leads = list_leads(status="enriched")
    created: list[dict] = []
    errors: list[str] = []

    for lead in enriched_leads:
        angle = _next_angle()
        try:
            sequence_id = create_sequence_for_lead(lead["id"], angle)
            created.append(
                {
                    "lead_id": lead["id"],
                    "company_name": lead["company_name"],
                    "sequence_id": sequence_id,
                    "angle": angle,
                }
            )
        except Exception as exc:
            logger.exception(f"[campaign tick] sequence creation failed for lead #{lead['id']}: {exc}")
            errors.append(f"sequence/lead#{lead['id']}: {exc}")

    return {"created": created, "errors": errors}


def _stage_send(live: bool) -> dict:
    try:
        results = send_tick(live=live)
        return {"results": results, "error": None}
    except Exception as exc:
        logger.exception(f"[campaign tick] send tick failed: {exc}")
        return {"results": [], "error": str(exc)}


def _stage_inbox() -> dict:
    try:
        stats = check_inbox()
        return {"stats": stats, "skipped": False, "error": None}
    except ValueError as exc:
        # Missing IMAP_* env vars — documented as "skip cleanly", not an error.
        return {"stats": None, "skipped": True, "error": str(exc)}
    except Exception as exc:
        logger.exception(f"[campaign tick] inbox check failed: {exc}")
        return {"stats": None, "skipped": False, "error": str(exc)}


def _missing_digest_smtp_vars() -> list[str]:
    return [v for v in _REQUIRED_DIGEST_SMTP_VARS if not os.getenv(v)]


def _send_digest_email(subject: str, body: str) -> tuple[bool, str | None]:
    """Email the digest to DIGEST_EMAIL when both it and SMTP are configured.

    Exempt from the daily send cap and suppression list — this goes to us, not
    a prospect. Uses its own direct SMTP connection rather than send_tick's
    machinery (no cap bookkeeping, no unsubscribe footer, no suppression check)."""
    digest_email = os.getenv("DIGEST_EMAIL")
    if not digest_email:
        return False, None

    missing = _missing_digest_smtp_vars()
    if missing:
        return False, f"DIGEST_EMAIL is set but SMTP is not fully configured (missing {', '.join(missing)})"

    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    from_name = os.getenv("SMTP_FROM_NAME", "")
    from_email = os.environ["SMTP_FROM_EMAIL"]

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
        msg["To"] = digest_email
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=30) as conn:
            conn.starttls()
            conn.login(user, password)
            conn.send_message(msg)
        return True, None
    except Exception as exc:
        logger.exception(f"Failed to email digest to {digest_email}: {exc}")
        return False, str(exc)


def _render_digest(date_str: str, stages: dict) -> str:
    lines: list[str] = [f"# Cold Email Lab — Daily Digest — {date_str}", ""]

    breaker = stages.get("breaker", {})
    lines.append("## Circuit breaker")
    if breaker.get("tripped"):
        lines.append(f"**TRIPPED** — {breaker.get('reason')}")
        lines.append("Live sends are blocked until `campaign resume` is run.")
    else:
        lines.append("OK — not tripped.")
    lines.append("")

    lines.append("## Awaiting approval")
    lines.append(f"{count_draft_steps()} draft step(s) awaiting human approval.")
    lines.append("")

    resurface = stages.get("resurface", {})
    lines.append("## Resurfaced")
    if resurface.get("leads"):
        for lead in resurface["leads"]:
            lines.append(f"- Lead #{lead['lead_id']} — {lead['company_name']}")
    else:
        lines.append("None due today.")
    lines.append("")

    replenish = stages.get("replenish", {})
    lines.append("## Replenish (sourcing)")
    if replenish.get("ran"):
        for p in replenish.get("profiles", []):
            lines.append(
                f"- {p['profile']}: discovered {p['discovered']}, filtered {p['filtered']}, "
                f"inserted {p['inserted']}, skipped-dup {p['skipped_duplicate']}, "
                f"no-email {p['no_email_found']}"
            )
            for notice in p.get("notices", []):
                lines.append(f"  - Notice: {notice}")
    else:
        lines.append(replenish.get("notice", "Skipped — pipeline floor already met."))
    lines.append("")

    enrich = stages.get("enrich", {})
    lines.append("## Enrichment")
    lines.append(
        f"Attempted {enrich.get('attempted', 0)} (cap {enrich.get('cap', '?')}), "
        f"{enrich.get('ok', 0)} succeeded, {enrich.get('skipped_no_profile', 0)} skipped (no profile)."
    )
    lines.append("")

    sequence = stages.get("sequence", {})
    created = sequence.get("created", [])
    lines.append("## Sequences auto-created (draft — awaiting human approval)")
    if created:
        for c in created:
            lines.append(
                f"- Lead #{c['lead_id']} {c['company_name']} -> sequence #{c['sequence_id']} "
                f"(angle {c['angle']})"
            )
    else:
        lines.append("None.")
    lines.append("")

    send = stages.get("send", {})
    send_results = [r for r in send.get("results", []) if r.get("kind", "step") == "step"]
    sent_count = sum(1 for r in send_results if r["status"] == "sent")
    deferred_count = sum(1 for r in send_results if r["status"] == "deferred")
    failed_count = sum(1 for r in send_results if r["status"] == "failed")
    lines.append("## Send tick")
    if send.get("error"):
        lines.append(f"Error: {send['error']}")
    else:
        lines.append(
            f"{len(send_results)} due step(s) — sent {sent_count}, deferred {deferred_count}, "
            f"failed {failed_count}."
        )
    lines.append("")

    inbox = stages.get("inbox", {})
    lines.append("## Inbox check")
    if inbox.get("skipped"):
        lines.append(f"Skipped — {inbox.get('error')}")
    elif inbox.get("error"):
        lines.append(f"Error: {inbox['error']}")
    elif inbox.get("stats"):
        s = inbox["stats"]
        lines.append(
            f"Scanned {s['scanned']} — {s['reply']} reply(ies), {s['unsubscribe']} unsubscribe(s), "
            f"{s['bounce']} bounce(s), {s['no_match']} unmatched."
        )
    lines.append("")

    replies = stages.get("replies", [])
    lines.append("## Replies")
    if replies:
        for r in replies:
            snippet = (r.get("body_text") or "").strip().replace("\n", " ")[:200]
            lines.append(f"- {r['company_name']} <{r['contact_email']}> ({r['kind']}): {snippet}")
    else:
        lines.append("No replies received since the last tick.")
    lines.append("")

    errors: list[str] = []
    for stage_name in ("resurface", "replenish", "enrich", "sequence"):
        errors.extend(f"{stage_name}: {e}" for e in stages.get(stage_name, {}).get("errors", []))
    if send.get("error"):
        errors.append(f"send: {send['error']}")
    if inbox.get("error") and not inbox.get("skipped"):
        errors.append(f"inbox: {inbox['error']}")

    lines.append("## Errors")
    if errors:
        lines.extend(f"- {e}" for e in errors)
    else:
        lines.append("None.")
    lines.append("")

    pipeline_stats = stages.get("pipeline", {})
    lines.append("## Pipeline counts")
    funnel = pipeline_stats.get("funnel", {})
    for status, count in funnel.items():
        lines.append(f"- {status}: {count}")
    lines.append(f"- total leads: {pipeline_stats.get('total_leads', 0)}")
    lines.append(f"- active sequences: {pipeline_stats.get('active_sequences', 0)}")
    lines.append(f"- due to resurface (unresolved): {pipeline_stats.get('due_to_resurface', 0)}")
    lines.append(f"- reply rate: {pipeline_stats.get('reply_rate', 0) * 100:.1f}%")
    lines.append(f"- unsubscribe rate: {pipeline_stats.get('unsubscribe_rate', 0) * 100:.1f}%")
    lines.append(f"- suppressed: {pipeline_stats.get('suppression_count', 0)}")
    lines.append("")

    return "\n".join(lines)


async def run_campaign_tick(live: bool = False) -> dict:
    """Run one full autopilot tick. Every stage is isolated — a stage's exception
    is caught, logged, and recorded; later stages still run. Returns the full
    stages dict (also used to render/print the digest)."""
    tick_started_at = _now_iso()
    stages: dict = {}

    try:
        stages["resurface"] = await _stage_resurface()
    except Exception as exc:
        logger.exception(f"[campaign tick] resurface stage crashed: {exc}")
        stages["resurface"] = {"count": 0, "leads": [], "errors": [str(exc)]}

    try:
        stages["replenish"] = await _stage_replenish()
    except Exception as exc:
        logger.exception(f"[campaign tick] replenish stage crashed: {exc}")
        stages["replenish"] = {"ran": False, "profiles": [], "errors": [str(exc)]}

    try:
        stages["enrich"] = await _stage_enrich()
    except Exception as exc:
        logger.exception(f"[campaign tick] enrich stage crashed: {exc}")
        stages["enrich"] = {"attempted": 0, "ok": 0, "skipped_no_profile": 0, "errors": [str(exc)]}

    try:
        stages["sequence"] = await _stage_sequence()
    except Exception as exc:
        logger.exception(f"[campaign tick] sequence stage crashed: {exc}")
        stages["sequence"] = {"created": [], "errors": [str(exc)]}

    stages["send"] = _stage_send(live)
    stages["inbox"] = _stage_inbox()

    stages["breaker"] = get_breaker_status()
    stages["pipeline"] = get_pipeline_stats()
    stages["replies"] = [dict(r) for r in list_replies_since(tick_started_at)]

    date_str = datetime.now(timezone.utc).date().isoformat()
    digest_md = _render_digest(date_str, stages)

    digest_path = DB_PATH.parent / f"digest-{date_str}.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(digest_md, encoding="utf-8")

    emailed, email_error = _send_digest_email(f"Cold Email Lab digest — {date_str}", digest_md)

    stages["digest_path"] = str(digest_path)
    stages["digest_emailed"] = emailed
    stages["digest_email_error"] = email_error

    return stages
