from datetime import datetime, timedelta, timezone

from loguru import logger

from ..storage.db import (
    create_sequence,
    create_sequence_step,
    get_lead,
    get_outreach_sets,
)
from ..storage.db import update_lead_status
from ..storage.models import OutreachAngle

_STEP_OFFSETS_DAYS = {1: 0, 2: 3, 3: 7}


def create_sequence_for_lead(lead_id: int, angle: int) -> int:
    """Create a 3-step sequence for a lead from an angle in its linked run's outreach_sets.

    Steps are due at day 0 / +3 / +7 (UTC), all status 'draft'. Sets lead status
    to 'sequenced'. Returns the new sequence id.
    """
    if angle not in (1, 2, 3):
        raise ValueError("angle must be 1, 2, or 3")

    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead #{lead_id} not found")
    if lead["run_id"] is None:
        raise ValueError(
            f"Lead #{lead_id} has not been enriched yet — run `enrich {lead_id}` first"
        )

    outreach_sets = get_outreach_sets(lead["run_id"])
    if len(outreach_sets) < angle:
        raise ValueError(
            f"Run #{lead['run_id']} has only {len(outreach_sets)} outreach angle(s) — "
            f"cannot use angle {angle}"
        )

    angle_row = outreach_sets[angle - 1]
    outreach_angle = OutreachAngle.model_validate_json(angle_row["angle_json"])

    sequence_id = create_sequence(lead_id, lead["run_id"], outreach_angle.angle_name)

    now = datetime.now(timezone.utc)
    step_emails = [
        (1, outreach_angle.initial_email),
        (2, outreach_angle.followup_day3),
        (3, outreach_angle.followup_day7),
    ]
    for step_number, email in step_emails:
        due_at = now + timedelta(days=_STEP_OFFSETS_DAYS[step_number])
        create_sequence_step(
            sequence_id=sequence_id,
            step_number=step_number,
            due_at=due_at.isoformat(),
            subject=email.subject,
            body=email.body,
        )

    update_lead_status(lead_id, "sequenced")
    logger.info(
        f"Sequence #{sequence_id} created for lead #{lead_id} (angle: {outreach_angle.angle_name})"
    )
    return sequence_id
