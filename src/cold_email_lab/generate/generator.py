import copy
import json
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from ..llm.client import complete
from ..storage.db import (
    get_research_brief,
    get_run,
    save_outreach_set,
    update_run_status,
    update_run_tokens,
)
from ..storage.models import CompanyBrief, OutreachAngle

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "email_generation.md"


class _AngleSet(BaseModel):
    angles: list[OutreachAngle] = Field(min_length=3, max_length=3)


def _build_tool_schema() -> dict:
    schema = copy.deepcopy(_AngleSet.model_json_schema())
    return {
        "name": "create_outreach_set",
        "description": "Record three distinct outreach angles with emails.",
        "input_schema": schema,
    }


def _format_brief(brief: CompanyBrief) -> str:
    lines = [f"Company: {brief.company_name}"]
    if brief.tagline:
        lines.append(f"Tagline: {brief.tagline}")
    lines.append(f"\nDescription: {brief.description}")
    if brief.target_customers:
        lines.append("Target customers: " + ", ".join(brief.target_customers))
    if brief.products_services:
        lines.append("Products/services: " + ", ".join(brief.products_services))
    if brief.recent_news:
        lines.append("\nRecent news:")
        lines += [f"  - {s}" for s in brief.recent_news]
    if brief.hiring_signals:
        lines.append("\nHiring signals:")
        lines += [f"  - {s}" for s in brief.hiring_signals]
    if brief.tech_stack_hints:
        lines.append("Tech stack: " + ", ".join(brief.tech_stack_hints))
    if brief.likely_pain_points:
        lines.append("\nLikely pain points:")
        lines += [f"  - {s}" for s in brief.likely_pain_points]
    if brief.linkedin_signals:
        lines.append("\nLinkedIn signals:")
        lines += [f"  - {s}" for s in brief.linkedin_signals]
    if brief.confidence_notes:
        lines.append(f"\nNotes: {brief.confidence_notes}")
    return "\n".join(lines)


async def generate_outreach(run_id: int) -> list[OutreachAngle]:
    """Generate three outreach angles from a briefed run and persist them."""
    run = get_run(run_id)
    if run is None:
        raise ValueError(f"Run #{run_id} not found")
    if run["status"] not in ("briefed", "completed"):
        raise ValueError(
            f"Run #{run_id} has status '{run['status']}', expected 'briefed'"
        )

    brief_row = get_research_brief(run_id)
    if brief_row is None:
        raise ValueError(f"Run #{run_id} has no research brief — run `brief` first")

    brief = CompanyBrief.model_validate_json(brief_row["brief_json"])
    logger.info(f"Generating outreach for {brief.company_name!r} (run #{run_id})")

    prompt = (
        _PROMPT_PATH.read_text()
        .replace("{research_brief}", _format_brief(brief))
        .replace("{company_name}", brief.company_name)
    )

    tool_schema = _build_tool_schema()

    logger.info("Calling LLM for email generation …")
    result = await complete(prompt, max_tokens=8192, purpose="email_generation", tool_schema=tool_schema)
    data = json.loads(result.text)

    angle_set = _AngleSet.model_validate(data)
    angles = angle_set.angles

    for angle in angles:
        save_outreach_set(run_id, angle.angle_name, angle.model_dump_json())

    cost = result.cost_usd or 0.0
    update_run_tokens(run_id, result.input_tokens, result.output_tokens, cost)
    update_run_status(run_id, "completed")

    logger.info(
        f"Outreach generated — {result.input_tokens} in / {result.output_tokens} out tokens, ${cost:.4f} USD"
    )
    return angles
