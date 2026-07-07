import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from ..llm.client import complete
from ..research.researcher import fetch_linkedin_signals, fetch_news_signals
from ..storage.db import (
    get_run,
    get_scraped_pages,
    save_research_brief,
    update_run_status,
    update_run_tokens,
)
from ..storage.models import CompanyBrief

_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "synthesis.md"

_SKIP_WORDS = {"menu", "skip", "navigation", "cookie", "loading", "close", "search"}


def _extract_company_name(pages, url: str) -> str:
    for page in pages:
        if page["page_type"] == "homepage" and page["content_text"]:
            for line in page["content_text"].split("\n")[:15]:
                line = line.strip()
                if not line or not (3 <= len(line) <= 60):
                    continue
                if any(kw in line.lower() for kw in _SKIP_WORDS):
                    continue
                return line
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.replace("www.", "")
    return domain.split(".")[0].title()


def _format_scraped_content(pages) -> str:
    parts = []
    for page in pages:
        if page["content_text"]:
            parts.append(
                f"## [{page['page_type']}] {page['url']}\n\n{page['content_text'][:8000]}"
            )
    return "\n\n---\n\n".join(parts) if parts else "(no content scraped)"


def _build_tool_schema() -> dict:
    full = CompanyBrief.model_json_schema()
    excluded = {"scraped_at", "sources_scraped"}
    props = {k: v for k, v in full.get("properties", {}).items() if k not in excluded}
    required = [f for f in full.get("required", []) if f not in excluded]
    return {
        "name": "create_brief",
        "description": "Record the structured research brief for this company.",
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


async def synthesise_brief(run_id: int) -> CompanyBrief:
    """Synthesise scraped content into a CompanyBrief and persist it."""
    run = get_run(run_id)
    if run is None:
        raise ValueError(f"Run #{run_id} not found")
    if run["status"] not in ("scraped", "briefed"):
        raise ValueError(
            f"Run #{run_id} has status '{run['status']}', expected 'scraped'"
        )

    pages = get_scraped_pages(run_id)
    if not pages:
        raise ValueError(f"Run #{run_id} has no scraped pages")

    company_name = _extract_company_name(pages, run["url"])
    logger.info(f"Synthesising brief for {company_name!r} (run #{run_id})")

    logger.info("Fetching news signals …")
    news = await fetch_news_signals(company_name)

    logger.info("Fetching LinkedIn signals …")
    linkedin = await fetch_linkedin_signals(company_name)

    scraped_content = _format_scraped_content(pages)
    news_text = "\n".join(f"- {s}" for s in news) or "(none found)"
    linkedin_text = "\n".join(f"- {s}" for s in linkedin) or "(none found)"

    prompt = (
        _PROMPT_PATH.read_text()
        .replace("{company_name}", company_name)
        .replace("{scraped_content}", scraped_content)
        .replace("{recent_news}", news_text)
        .replace("{linkedin_signals}", linkedin_text)
    )

    tool_schema = _build_tool_schema()

    logger.info("Calling LLM for synthesis …")
    result = await complete(prompt, max_tokens=4096, purpose="synthesis", tool_schema=tool_schema)
    brief_data = json.loads(result.text)

    brief_data["scraped_at"] = datetime.now(timezone.utc).isoformat()
    brief_data["sources_scraped"] = [p["url"] for p in pages if not p["error"]]

    brief = CompanyBrief.model_validate(brief_data)

    save_research_brief(run_id, brief.model_dump_json())

    cost = result.cost_usd or 0.0
    update_run_tokens(run_id, result.input_tokens, result.output_tokens, cost)
    update_run_status(run_id, "briefed")

    logger.info(
        f"Brief created — {result.input_tokens} in / {result.output_tokens} out tokens, ${cost:.4f} USD"
    )
    return brief
