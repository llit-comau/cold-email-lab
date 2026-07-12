import csv
import sqlite3
from pathlib import Path

from loguru import logger

from ..analyse.analyser import synthesise_brief
from ..generate.generator import generate_outreach
from ..llm.client import require_configured
from ..scrape.playwright_scraper import scrape_company
from ..storage.db import (
    create_lead,
    create_run,
    get_lead,
    list_leads as db_list_leads,
    save_scraped_page,
    update_lead_run,
    update_lead_status,
    update_run_status,
)
from .sourcing import resolve_profile_for_lead


def import_leads_csv(path: str | Path) -> tuple[int, int]:
    """Import leads from a CSV file. Returns (imported, skipped).

    Expected columns: company_name, url, contact_name, contact_email, optional notes.
    Dedupes on contact_email.
    """
    path = Path(path)
    if not path.exists():
        raise ValueError(f"CSV file not found: {path}")

    imported = 0
    skipped = 0
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = (row.get("contact_email") or "").strip()
            company_name = (row.get("company_name") or "").strip()
            url = (row.get("url") or "").strip()
            if not email or not company_name or not url:
                logger.warning(f"Skipping row with missing required fields: {row}")
                skipped += 1
                continue
            try:
                create_lead(
                    company_name=company_name,
                    url=url,
                    contact_email=email,
                    contact_name=(row.get("contact_name") or "").strip() or None,
                    source="csv-import",
                    notes=(row.get("notes") or "").strip() or None,
                )
                imported += 1
            except sqlite3.IntegrityError:
                logger.debug(f"Skipping duplicate lead: {email}")
                skipped += 1
    return imported, skipped


def add_lead(
    company_name: str,
    url: str,
    contact_email: str,
    contact_name: str | None = None,
    notes: str | None = None,
) -> int:
    """Add a single lead. Raises ValueError on duplicate contact_email."""
    try:
        return create_lead(
            company_name=company_name,
            url=url,
            contact_email=contact_email,
            contact_name=contact_name,
            source="manual",
            notes=notes,
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"A lead with email {contact_email} already exists") from exc


def list_leads(status: str | None = None, source: str | None = None) -> list[sqlite3.Row]:
    return db_list_leads(status=status, source=source)


async def enrich_lead(lead_id: int, profile: str | None = None) -> int:
    """Run the research → brief → generate pipeline for a lead's URL.

    Reuses scrape_company / synthesise_brief / generate_outreach — does not
    duplicate their logic. Links the resulting run_id to the lead and sets
    status to 'enriched'. Returns the run_id.

    Phase 12: the offer pitch/proof_points come from an ICP profile — resolved
    from profiles/<lead.source>.toml when it exists, else `profile` (CLI
    `--profile`) must be given. Resolution happens before create_run, so a
    missing profile never leaves a lead half-touched.
    """
    require_configured()  # fail before create_run — never touch the lead/run tables on a bad config

    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead #{lead_id} not found")

    resolved_profile = resolve_profile_for_lead(lead["source"], profile)

    logger.info(f"Enriching lead #{lead_id} ({lead['company_name']}, {lead['url']})")

    run_id = create_run(lead["url"])
    try:
        pages = await scrape_company(lead["url"])
        for p in pages:
            save_scraped_page(
                run_id=run_id,
                url=p.url,
                page_type=p.page_type,
                content_text=p.text_content,
                error=p.error,
            )
        update_run_status(run_id, "scraped")

        await synthesise_brief(run_id)
        await generate_outreach(
            run_id,
            pitch=resolved_profile.pitch,
            proof_points=resolved_profile.proof_points,
            offer_keywords=resolved_profile.offer_keywords,
        )

        update_lead_run(lead_id, run_id)
        update_lead_status(lead_id, "enriched")
        logger.info(f"Lead #{lead_id} enriched — run #{run_id}")
        return run_id
    except Exception:
        update_run_status(run_id, "failed")
        raise


async def enrich_all_new(
    limit: int | None = None, profile: str | None = None
) -> list[tuple[int, int | None, Exception | None]]:
    """Enrich all leads with status 'new'. Returns list of (lead_id, run_id_or_None, error_or_None).

    `profile` (CLI `--profile`) is passed through to every lead as a fallback for leads whose
    `source` doesn't resolve to a profiles/*.toml file on its own.
    """
    leads = db_list_leads(status="new")
    if limit is not None:
        leads = leads[:limit]

    results: list[tuple[int, int | None, Exception | None]] = []
    for lead in leads:
        lead_id = lead["id"]
        try:
            run_id = await enrich_lead(lead_id, profile=profile)
            results.append((lead_id, run_id, None))
        except Exception as exc:
            logger.exception(f"Enrichment failed for lead #{lead_id}: {exc}")
            results.append((lead_id, None, exc))
    return results
