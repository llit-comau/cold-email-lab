"""Phase 13: campaign circuit breaker.

Guards against runaway autopilot sending. Two independent trip conditions,
checked after every `send_tick(live=True)` call (whether invoked directly via
`send tick --live` or as stage (e) of `campaign tick --live`):

1. Bounce rate > 5% over the trailing 20 sent steps (only evaluated once at
   least 10 steps have been sent — too few data points otherwise).
2. Two consecutive SMTP connection/authentication failures.

State lives in the existing `kv` table (no new table needed). Tripping is
idempotent — re-tripping with a new reason just overwrites the stored reason.
Only a deliberate `campaign resume` (or `resume_breaker()`) clears it; nothing
clears it automatically.
"""

from loguru import logger

from ..storage.db import delete_kv, get_kv, get_recent_sent_steps, set_kv

BREAKER_KEY = "breaker_tripped"
BREAKER_REASON_KEY = "breaker_reason"
SMTP_STREAK_KEY = "smtp_consecutive_failures"

BOUNCE_RATE_THRESHOLD = 0.05
BOUNCE_RATE_MIN_SENT = 10
BOUNCE_RATE_WINDOW = 20
SMTP_FAILURE_THRESHOLD = 2


def get_breaker_status() -> dict:
    """Current breaker state: {"tripped": bool, "reason": str | None}."""
    tripped = get_kv(BREAKER_KEY) == "1"
    reason = get_kv(BREAKER_REASON_KEY) if tripped else None
    return {"tripped": tripped, "reason": reason}


def trip_breaker(reason: str) -> None:
    already_tripped = get_kv(BREAKER_KEY) == "1"
    set_kv(BREAKER_KEY, "1")
    set_kv(BREAKER_REASON_KEY, reason)
    if not already_tripped:
        logger.warning(f"Circuit breaker TRIPPED: {reason}")


def resume_breaker() -> bool:
    """Deliberately clear a tripped breaker (the only way it clears). Returns True
    if it had been tripped."""
    was_tripped = get_kv(BREAKER_KEY) == "1"
    delete_kv(BREAKER_KEY)
    delete_kv(BREAKER_REASON_KEY)
    delete_kv(SMTP_STREAK_KEY)
    if was_tripped:
        logger.info("Circuit breaker resumed (cleared) by operator")
    return was_tripped


def record_smtp_result(success: bool) -> None:
    """Track consecutive SMTP connection/auth failures; trip on the 2nd in a row."""
    if success:
        set_kv(SMTP_STREAK_KEY, "0")
        return
    streak = int(get_kv(SMTP_STREAK_KEY) or "0") + 1
    set_kv(SMTP_STREAK_KEY, str(streak))
    if streak >= SMTP_FAILURE_THRESHOLD:
        trip_breaker(f"{streak} consecutive SMTP connection failures")


def evaluate_bounce_rate() -> None:
    """Trip the breaker if the trailing sent-steps window shows >5% bounces."""
    rows = get_recent_sent_steps(limit=BOUNCE_RATE_WINDOW)
    total = len(rows)
    if total < BOUNCE_RATE_MIN_SENT:
        return
    bounced = sum(1 for r in rows if r["lead_status"] == "bounced")
    rate = bounced / total
    if rate > BOUNCE_RATE_THRESHOLD:
        trip_breaker(
            f"bounce rate {rate * 100:.1f}% over trailing {total} sent step(s) "
            f"({bounced}/{total} bounced) exceeds the 5% threshold"
        )
