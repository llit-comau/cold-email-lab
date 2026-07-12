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
from ..outbound.qa import lint_email_set
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


def _format_proof_points(proof_points: list[str]) -> str:
    if not proof_points:
        return "(none provided — see the rule above: no social-proof claims permitted)"
    return "\n".join(f"- {p}" for p in proof_points)


def _format_lint_feedback(flags_by_angle: dict[str, list[str]]) -> str:
    lines = [
        "The previous draft failed QA lint. Fix every issue below in the new draft — "
        "do not just shorten text to dodge a word count, actually address the problem "
        "(especially any fabrication flags: remove the invented claim entirely).",
    ]
    for angle_name, flags in flags_by_angle.items():
        if not flags:
            continue
        lines.append(f"\n{angle_name}:")
        lines += [f"  - {f}" for f in flags]
    return "\n".join(lines)


async def generate_outreach(
    run_id: int,
    pitch: str,
    proof_points: list[str] | None = None,
    offer_keywords: list[str] | None = None,
) -> list[OutreachAngle]:
    """Generate three outreach angles from a briefed run and persist them.

    `pitch` (required) and `proof_points` (optional, Phase 12) anchor every email on OUR
    offer and gate what social proof may be claimed. `offer_keywords` (optional) is used
    only by the post-generation QA lint (outbound/qa.py), never sent to the LLM.
    """
    proof_points = proof_points or []
    offer_keywords = offer_keywords or []

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

    base_prompt = (
        _PROMPT_PATH.read_text()
        .replace("{research_brief}", _format_brief(brief))
        .replace("{company_name}", brief.company_name)
        .replace("{{pitch}}", pitch)
        .replace("{{proof_points}}", _format_proof_points(proof_points))
    )

    tool_schema = _build_tool_schema()

    logger.info("Calling LLM for email generation …")
    result = await complete(base_prompt, max_tokens=8192, purpose="email_generation", tool_schema=tool_schema)
    total_input_tokens = result.input_tokens
    total_output_tokens = result.output_tokens
    total_cost = result.cost_usd or 0.0

    data = json.loads(result.text)
    angle_set = _AngleSet.model_validate(data)
    angles = angle_set.angles

    flags_by_angle = lint_email_set(angles, offer_keywords=offer_keywords, proof_points=proof_points)

    if flags_by_angle:
        logger.warning(f"QA lint flagged the first draft for run #{run_id}: {flags_by_angle}")
        retry_prompt = base_prompt + "\n\n## Lint feedback — fix these issues\n" + _format_lint_feedback(flags_by_angle)
        retry_result = await complete(
            retry_prompt, max_tokens=8192, purpose="email_generation_retry", tool_schema=tool_schema
        )
        total_input_tokens += retry_result.input_tokens
        total_output_tokens += retry_result.output_tokens
        total_cost += retry_result.cost_usd or 0.0

        retry_data = json.loads(retry_result.text)
        retry_angle_set = _AngleSet.model_validate(retry_data)
        angles = retry_angle_set.angles
        flags_by_angle = lint_email_set(angles, offer_keywords=offer_keywords, proof_points=proof_points)
        if flags_by_angle:
            logger.warning(
                f"QA lint still flagged run #{run_id} after retry — saving anyway with qa_flags: {flags_by_angle}"
            )

    for angle in angles:
        flags = flags_by_angle.get(angle.angle_name, [])
        save_outreach_set(
            run_id,
            angle.angle_name,
            angle.model_dump_json(),
            qa_flags=json.dumps(flags) if flags else None,
        )

    update_run_tokens(run_id, total_input_tokens, total_output_tokens, total_cost)
    update_run_status(run_id, "completed")

    logger.info(
        f"Outreach generated — {total_input_tokens} in / {total_output_tokens} out tokens, ${total_cost:.4f} USD"
    )
    return angles
