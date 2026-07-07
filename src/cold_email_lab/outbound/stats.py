"""Business logic for the pipeline dashboard (Phase 7). Pure queries + derived rates —
no printing, no HTML here; that lives in cli.py and dashboard_html.py respectively.
"""

from datetime import datetime, timedelta, timezone

from ..storage.db import (
    count_active_sequences,
    count_events_distinct_leads,
    count_leads_by_status,
    count_leads_with_sent_step,
    count_steps_due_today,
    count_steps_sent_since,
    count_suppression,
    get_angle_performance_rows,
    get_all_sequences_overview,
    get_daily_reply_counts,
    get_daily_sent_counts,
    get_sequence_steps,
    get_snoozed_leads_due,
)

LEAD_STATUSES = ("new", "enriched", "sequenced", "replied", "unsubscribed", "bounced")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_days_ago(days: int) -> str:
    return (_now() - timedelta(days=days)).isoformat()


def get_pipeline_stats() -> dict:
    """Aggregate funnel + rate metrics for the `pipeline` command and dashboard header."""
    by_status_raw = count_leads_by_status()
    funnel = {status: by_status_raw.get(status, 0) for status in LEAD_STATUSES}
    total_leads = sum(by_status_raw.values())

    active_sequences = count_active_sequences()
    due_today = count_steps_due_today()
    sent_this_week = count_steps_sent_since(_iso_days_ago(7))

    leads_with_sent_step = count_leads_with_sent_step()
    replied_leads = count_events_distinct_leads("replied")
    reply_rate = (replied_leads / leads_with_sent_step) if leads_with_sent_step else 0.0

    unsubscribed_leads = funnel["unsubscribed"]
    unsubscribe_rate = (unsubscribed_leads / total_leads) if total_leads else 0.0

    return {
        "funnel": funnel,
        "total_leads": total_leads,
        "active_sequences": active_sequences,
        "due_today": due_today,
        "sent_this_week": sent_this_week,
        "due_to_resurface": len(get_snoozed_leads_due(_now().date().isoformat())),
        "leads_with_sent_step": leads_with_sent_step,
        "replied_leads": replied_leads,
        "reply_rate": reply_rate,
        "unsubscribed_leads": unsubscribed_leads,
        "unsubscribe_rate": unsubscribe_rate,
        "suppression_count": count_suppression(),
    }


def get_daily_activity(days: int = 30) -> list[dict]:
    """Sent vs. replies per day for the last `days` days (UTC), zero-filled."""
    since_iso = _iso_days_ago(days - 1)
    sent_by_day = {r["day"]: r["c"] for r in get_daily_sent_counts(since_iso)}
    replies_by_day = {r["day"]: r["c"] for r in get_daily_reply_counts(since_iso)}

    today = _now().date()
    activity = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        activity.append(
            {
                "date": day,
                "sent": sent_by_day.get(day, 0),
                "replies": replies_by_day.get(day, 0),
            }
        )
    return activity


def get_angle_performance() -> list[dict]:
    """Reply performance by sequence angle, zero-guarded."""
    rows = get_angle_performance_rows()
    performance = []
    for row in rows:
        contacted = row["leads_contacted"]
        replies = row["replies"]
        performance.append(
            {
                "angle_name": row["angle_name"],
                "leads_contacted": contacted,
                "replies": replies,
                "reply_rate": (replies / contacted) if contacted else 0.0,
            }
        )
    return sorted(
        performance,
        key=lambda p: (p["reply_rate"], p["leads_contacted"]),
        reverse=True,
    )


def get_sequences_overview(limit: int = 100) -> list[dict]:
    """Sequences joined with lead info and their steps, for the dashboard table."""
    sequences = get_all_sequences_overview(limit=limit)
    overview = []
    for seq in sequences:
        steps = get_sequence_steps(seq["sequence_id"])
        overview.append(
            {
                "sequence_id": seq["sequence_id"],
                "sequence_status": seq["sequence_status"],
                "angle_name": seq["angle_name"],
                "lead_id": seq["lead_id"],
                "company_name": seq["company_name"],
                "contact_email": seq["contact_email"],
                "steps": [
                    {
                        "step_number": s["step_number"],
                        "status": s["status"],
                        "due_at": s["due_at"],
                    }
                    for s in steps
                ],
            }
        )
    return overview
