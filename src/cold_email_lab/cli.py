import asyncio
import sys
from datetime import datetime
from pathlib import Path

import typer
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

app = typer.Typer(
    name="cold-email-lab",
    help="AI-powered cold outreach research and drafting.",
    no_args_is_help=True,
)

leads_app = typer.Typer(name="leads", help="Manage leads.", no_args_is_help=True)
sequence_app = typer.Typer(name="sequence", help="Manage outreach sequences.", no_args_is_help=True)
send_app = typer.Typer(name="send", help="Send approved outreach steps.", no_args_is_help=True)
inbox_app = typer.Typer(name="inbox", help="Check for replies, unsubscribes and bounces.", no_args_is_help=True)
app.add_typer(leads_app)
app.add_typer(sequence_app)
app.add_typer(send_app)
app.add_typer(inbox_app)


def _setup_logging(timestamp: str) -> None:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{level: <8}</level> {message}")
    logger.add(
        f"logs/run-{timestamp}.log",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        rotation="50 MB",
    )


@app.command()
def research(
    url: str = typer.Argument(..., help="Target company URL"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print scraped text snippets"),
) -> None:
    """Scrape a target company's website and persist content to SQLite."""
    asyncio.run(_research(url, verbose))


async def _research(url: str, verbose: bool) -> None:
    from .scrape.playwright_scraper import scrape_company
    from .storage.db import (
        create_run,
        init_db,
        save_scraped_page,
        update_run_status,
    )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _setup_logging(timestamp)

    logger.info(f"Research started: {url}")
    init_db()

    run_id = create_run(url)
    typer.echo(f"Run #{run_id} — researching: {url}")

    try:
        typer.echo("Scraping website …")
        pages = await scrape_company(url)

        success_count = 0
        for p in pages:
            save_scraped_page(
                run_id=run_id,
                url=p.url,
                page_type=p.page_type,
                content_text=p.text_content,
                error=p.error,
            )
            if p.error:
                typer.echo(f"  [FAIL] {p.page_type:<10} {p.url}")
                typer.echo(f"         {p.error[:100]}", err=True)
            else:
                success_count += 1
                typer.echo(f"  [OK]   {p.page_type:<10} {p.url}  ({p.char_count:,} chars)")
                if verbose and p.text_content:
                    typer.echo(f"\n{'─'*60}")
                    typer.echo(p.text_content[:600])
                    typer.echo(f"{'─'*60}\n")

        update_run_status(run_id, "scraped")
        typer.echo(
            f"\nDone. {success_count}/{len(pages)} pages scraped. "
            f"Run #{run_id} persisted to SQLite."
        )
        logger.info(f"Run #{run_id} complete — {success_count}/{len(pages)} pages OK")

    except Exception as exc:
        logger.exception(f"Run #{run_id} failed: {exc}")
        update_run_status(run_id, "failed")
        typer.echo(f"\nRun failed: {exc}", err=True)
        raise typer.Exit(1)


@app.command()
def brief(
    run_id: int = typer.Argument(..., help="Run ID to synthesise"),
    json_out: bool = typer.Option(False, "--json", help="Print raw brief JSON"),
) -> None:
    """Synthesise a research brief from a scraped run."""
    asyncio.run(_brief(run_id, json_out))


async def _brief(run_id: int, json_out: bool) -> None:
    from .analyse.analyser import synthesise_brief
    from .storage.db import init_db

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _setup_logging(timestamp)
    init_db()

    typer.echo(f"Synthesising brief for run #{run_id} …")

    try:
        result = await synthesise_brief(run_id)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        logger.exception(f"Brief synthesis failed: {exc}")
        typer.echo(f"Synthesis failed: {exc}", err=True)
        raise typer.Exit(1)

    if json_out:
        typer.echo(result.model_dump_json(indent=2))
        return

    def _section(title: str, items: list[str]) -> None:
        if not items:
            return
        typer.echo(f"{title}:")
        for item in items:
            typer.echo(f"  • {item}")
        typer.echo()

    typer.echo(f"\n{'═' * 60}")
    typer.echo(f"  {result.company_name}")
    if result.tagline:
        typer.echo(f"  {result.tagline}")
    typer.echo(f"{'═' * 60}\n")
    typer.echo(f"{result.description}\n")
    _section("Target customers", result.target_customers)
    _section("Products / services", result.products_services)
    _section("Recent news", result.recent_news)
    _section("Hiring signals", result.hiring_signals)
    _section("Tech stack hints", result.tech_stack_hints)
    _section("Likely pain points", result.likely_pain_points)
    _section("LinkedIn signals", result.linkedin_signals)
    if result.confidence_notes:
        typer.echo(f"Notes: {result.confidence_notes}")


@app.command()
def generate(
    run_id: int = typer.Argument(..., help="Run ID to generate emails for"),
    json_out: bool = typer.Option(False, "--json", help="Print raw angles JSON"),
) -> None:
    """Generate three outreach angles and emails from a research brief."""
    asyncio.run(_generate(run_id, json_out))


async def _generate(run_id: int, json_out: bool) -> None:
    import json as _json

    from .generate.generator import generate_outreach
    from .storage.db import init_db

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _setup_logging(timestamp)
    init_db()

    typer.echo(f"Generating outreach for run #{run_id} …")

    try:
        angles = await generate_outreach(run_id)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        logger.exception(f"Generation failed: {exc}")
        typer.echo(f"Generation failed: {exc}", err=True)
        raise typer.Exit(1)

    if json_out:
        typer.echo(_json.dumps([a.model_dump() for a in angles], indent=2))
        return

    for i, angle in enumerate(angles, 1):
        typer.echo(f"\n{'═' * 60}")
        typer.echo(f"  Angle {i} — {angle.angle_name}")
        typer.echo(f"  {angle.angle_rationale}")
        typer.echo(f"{'═' * 60}")

        for label, email in [
            ("Initial email", angle.initial_email),
            ("Day 3 follow-up", angle.followup_day3),
            ("Day 7 follow-up", angle.followup_day7),
        ]:
            typer.echo(f"\n{label}")
            typer.echo(f"Subject: {email.subject}")
            typer.echo()
            typer.echo(email.body)

    typer.echo(f"\n{'─' * 60}")
    typer.echo(f"Run #{run_id} complete.")


@app.command()
def compare(
    run_a: int = typer.Argument(...),
    run_b: int = typer.Argument(...),
) -> None:
    """[Phase 4] Compare two runs side by side."""
    typer.echo("Phase 4 not yet implemented.", err=True)
    raise typer.Exit(1)


@app.command(name="list")
def list_runs() -> None:
    """List recent runs."""
    import sys

    from .storage.db import init_db, list_runs as db_list_runs

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()
    runs = db_list_runs(limit=20)
    if not runs:
        typer.echo("No runs yet.")
        return

    typer.echo(f"{'ID':<5} {'STATUS':<12} {'STARTED':<22} {'URL'}")
    typer.echo("─" * 80)
    for r in runs:
        typer.echo(f"{r['id']:<5} {r['status']:<12} {r['started_at'][:19]:<22} {r['url']}")


@leads_app.command(name="import")
def leads_import(
    csv_path: str = typer.Argument(..., help="Path to CSV file (company_name, url, contact_name, contact_email, notes)"),
) -> None:
    """Import leads from a CSV file, deduping on contact_email."""
    import sys

    from .outbound.leads import import_leads_csv
    from .storage.db import init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    try:
        imported, skipped = import_leads_csv(csv_path)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Imported {imported} lead(s), skipped {skipped} (duplicates or invalid rows).")


@leads_app.command(name="add")
def leads_add(
    company: str = typer.Option(..., "--company", help="Company name"),
    url: str = typer.Option(..., "--url", help="Company URL"),
    email: str = typer.Option(..., "--email", help="Contact email"),
    name: str = typer.Option(None, "--name", help="Contact name"),
    notes: str = typer.Option(None, "--notes", help="Optional notes"),
) -> None:
    """Add a single lead manually."""
    import sys

    from .outbound.leads import add_lead
    from .storage.db import init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    try:
        lead_id = add_lead(
            company_name=company, url=url, contact_email=email, contact_name=name, notes=notes
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Lead #{lead_id} added ({email}).")


@leads_app.command(name="list")
def leads_list(
    status: str = typer.Option(None, "--status", help="Filter by status"),
    source: str = typer.Option(None, "--source", help="Filter by source (e.g. a sourcing profile name)"),
) -> None:
    """List leads, optionally filtered by status and/or source."""
    import sys

    from .outbound.leads import list_leads
    from .storage.db import init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    rows = list_leads(status=status, source=source)
    if not rows:
        typer.echo("No leads found.")
        return

    typer.echo(f"{'ID':<5} {'STATUS':<11} {'COMPANY':<22} {'EMAIL':<26} {'SOURCE':<12} {'FIT':<5} {'RUN':<5}")
    typer.echo("─" * 90)
    for r in rows:
        fit = r["fit_score"] if r["fit_score"] is not None else "—"
        typer.echo(
            f"{r['id']:<5} {r['status']:<11} {r['company_name'][:22]:<22} "
            f"{r['contact_email'][:26]:<26} {(r['source'] or '')[:12]:<12} {str(fit):<5} {r['run_id'] or '':<5}"
        )


@leads_app.command(name="source")
def leads_source(
    profile: str = typer.Argument(..., help="Sourcing profile name (profiles/<name>.toml)"),
    limit: int = typer.Option(None, "--limit", help="Max leads to find/insert (overrides profile max_leads)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print candidates without inserting"),
) -> None:
    """Discover companies matching a profile, extract contacts, score fit, and insert as leads."""
    asyncio.run(_leads_source(profile, limit, dry_run))


async def _leads_source(profile: str, limit: int | None, dry_run: bool) -> None:
    from .outbound.sourcing import source_leads
    from .storage.db import init_db

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _setup_logging(timestamp)
    init_db()

    typer.echo(f"Sourcing leads for profile {profile!r}{' (dry-run)' if dry_run else ''} …")

    try:
        report = await source_leads(profile, limit=limit, dry_run=dry_run)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        logger.exception(f"Lead sourcing failed: {exc}")
        typer.echo(f"Lead sourcing failed: {exc}", err=True)
        raise typer.Exit(1)

    for notice in report.notices:
        typer.echo(f"Notice: {notice}")

    if report.candidates:
        typer.echo(f"\n{'COMPANY':<24} {'EMAIL':<28} {'NAME':<18} {'FIT':<5} EVIDENCE")
        typer.echo("─" * 100)
        for c in report.candidates:
            fit = c.fit_score if c.fit_score is not None else "—"
            typer.echo(
                f"{c.company_name[:24]:<24} {c.contact_email[:28]:<28} "
                f"{(c.contact_name or '')[:18]:<18} {str(fit):<5} {c.evidence}"
            )
    else:
        typer.echo("\nNo candidates with a usable contact email were found.")

    typer.echo(f"\n{'─' * 60}")
    typer.echo(
        f"Discovered {report.discovered} · filtered {report.filtered} · "
        f"no-email {report.no_email_found} · "
        + (
            f"would insert {len(report.candidates)} (dry-run, nothing inserted)"
            if dry_run
            else f"inserted {report.inserted} · skipped-duplicate {report.skipped_duplicate}"
        )
    )


@app.command()
def enrich(
    lead_id: int = typer.Argument(None, help="Lead ID to enrich"),
    all_new: bool = typer.Option(False, "--all-new", help="Enrich all leads with status 'new'"),
    limit: int = typer.Option(None, "--limit", help="Max number of leads to enrich with --all-new"),
) -> None:
    """Run the research → brief → generate pipeline for a lead (or all new leads)."""
    if lead_id is None and not all_new:
        typer.echo("Provide a lead_id or use --all-new.", err=True)
        raise typer.Exit(1)
    if lead_id is not None and all_new:
        typer.echo("Provide either a lead_id or --all-new, not both.", err=True)
        raise typer.Exit(1)

    asyncio.run(_enrich(lead_id, all_new, limit))


async def _enrich(lead_id: int | None, all_new: bool, limit: int | None) -> None:
    from .outbound.leads import enrich_all_new, enrich_lead
    from .storage.db import init_db

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _setup_logging(timestamp)
    init_db()

    if all_new:
        typer.echo("Enriching all new leads …")
        try:
            results = await enrich_all_new(limit=limit)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)

        if not results:
            typer.echo("No leads with status 'new' found.")
            return

        ok = 0
        for lid, run_id, err in results:
            if err is None:
                ok += 1
                typer.echo(f"  [OK]   Lead #{lid} → run #{run_id}")
            else:
                typer.echo(f"  [FAIL] Lead #{lid}: {err}", err=True)
        typer.echo(f"\nDone. {ok}/{len(results)} lead(s) enriched.")
        return

    typer.echo(f"Enriching lead #{lead_id} …")
    try:
        run_id = await enrich_lead(lead_id)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        logger.exception(f"Enrichment failed: {exc}")
        typer.echo(f"Enrichment failed: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Lead #{lead_id} enriched — run #{run_id}. Status → enriched.")


@sequence_app.command(name="create")
def sequence_create(
    lead_id: int = typer.Argument(..., help="Lead ID to create a sequence for"),
    angle: int = typer.Option(..., "--angle", help="Angle number (1, 2, or 3)"),
) -> None:
    """Create a 3-step outreach sequence for a lead from one of its angles."""
    import sys

    from .outbound.sequences import create_sequence_for_lead
    from .storage.db import get_sequence_steps, init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    try:
        sequence_id = create_sequence_for_lead(lead_id, angle)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    steps = get_sequence_steps(sequence_id)
    typer.echo(f"Sequence #{sequence_id} created for lead #{lead_id} (angle {angle}).")
    for step in steps:
        typer.echo(f"  Step {step['step_number']}: due {step['due_at'][:19]}  —  {step['subject']}")


@app.command()
def review(
    sequence: int = typer.Option(None, "--sequence", help="Limit to one sequence's draft steps"),
) -> None:
    """Show pending draft steps awaiting approval."""
    import sys

    from .outbound.sender import review_steps
    from .storage.db import init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    steps = review_steps(sequence_id=sequence)
    if not steps:
        typer.echo("No draft steps pending review.")
        return

    for step in steps:
        typer.echo(f"\n{'─' * 60}")
        typer.echo(
            f"Step #{step['id']}  (sequence #{step['sequence_id']}, step {step['step_number']}, "
            f"due {step['due_at'][:19]})"
        )
        typer.echo(f"To: {step['company_name']} <{step['contact_email']}>")
        typer.echo(f"Subject: {step['subject']}")
        typer.echo()
        typer.echo(step["body"])
    typer.echo(f"\n{'─' * 60}")
    typer.echo(f"{len(steps)} draft step(s) pending review.")


@app.command()
def approve(
    step_id: int = typer.Argument(None, help="Step ID to approve"),
    sequence: int = typer.Option(None, "--sequence", help="Approve all draft steps in a sequence"),
) -> None:
    """Approve a draft step, or all draft steps in a sequence."""
    import sys

    from .outbound.sender import approve_sequence, approve_step
    from .storage.db import init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    if step_id is None and sequence is None:
        typer.echo("Provide a step_id or --sequence.", err=True)
        raise typer.Exit(1)
    if step_id is not None and sequence is not None:
        typer.echo("Provide either a step_id or --sequence, not both.", err=True)
        raise typer.Exit(1)

    try:
        if sequence is not None:
            count = approve_sequence(sequence)
            typer.echo(f"Approved {count} step(s) in sequence #{sequence}.")
        else:
            approve_step(step_id)
            typer.echo(f"Step #{step_id} approved.")
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@app.command(name="edit-step")
def edit_step_cmd(
    step_id: int = typer.Argument(..., help="Step ID to edit"),
    subject: str = typer.Option(None, "--subject", help="New subject line"),
    body: str = typer.Option(None, "--body", help="New body text"),
) -> None:
    """Edit a draft step's subject and/or body before approval."""
    import sys

    from .outbound.sender import edit_step
    from .storage.db import init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    try:
        edit_step(step_id, subject=subject, body=body)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Step #{step_id} updated.")


@send_app.command(name="tick")
def send_tick_cmd(
    live: bool = typer.Option(False, "--live", help="Actually send via SMTP (default: dry-run)"),
) -> None:
    """Send (or dry-run) due, approved outreach steps."""
    import sys

    from .outbound.sender import send_tick
    from .storage.db import init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    try:
        results = send_tick(live=live)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if not results:
        typer.echo("No due steps to send.")
        return

    mode = "LIVE" if live else "DRY-RUN"
    step_results = [r for r in results if r.get("kind", "step") == "step"]
    for r in results:
        if r.get("kind") == "resurface_notice":
            typer.echo(f"\n{'─' * 60}")
            typer.echo(f"{r['count']} snoozed lead(s) are due to resurface:")
            for lead in r["leads"]:
                typer.echo(
                    f"  Lead #{lead['lead_id']} — {lead['company_name']} "
                    f"<{lead['contact_email']}> (snoozed until {lead['snooze_until']})"
                )
            continue

        typer.echo(f"\n{'─' * 60}")
        status_label = "DEFERRED" if r["status"] == "deferred" else mode
        typer.echo(f"[{status_label}] Step #{r['step_id']} → {r['company_name']} <{r['to']}>")
        typer.echo(f"Subject: {r['subject']}")
        if r["status"] == "deferred":
            cap = r["cap"]
            typer.echo(
                f"Deferred: daily cap ({cap['sent_today']}/{cap['cap']} already sent today; "
                f"source: {cap['source']})"
            )
        else:
            typer.echo()
            typer.echo(r["body"])
        if live:
            if r["status"] == "sent":
                typer.echo(f"\n[SENT] Message-ID: {r.get('smtp_message_id')}")
            elif r["status"] == "deferred":
                pass
            else:
                typer.echo(f"\n[FAILED] {r.get('error')}", err=True)

    typer.echo(f"\n{'─' * 60}")
    if live:
        sent = sum(1 for r in step_results if r["status"] == "sent")
        deferred = sum(1 for r in step_results if r["status"] == "deferred")
        typer.echo(f"{sent}/{len(step_results)} step(s) sent; {deferred} deferred by cap.")
    else:
        deferred = sum(1 for r in step_results if r["status"] == "deferred")
        typer.echo(
            f"{len(step_results)} due step(s); {deferred} deferred by cap — "
            "dry-run only, nothing sent or changed."
        )


@inbox_app.command(name="check")
def inbox_check_cmd() -> None:
    """Check the inbox for replies, unsubscribes and bounces since the last check."""
    import sys

    from .outbound.inbox import check_inbox
    from .storage.db import init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    try:
        stats = check_inbox()
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    except Exception as exc:
        logger.exception(f"Inbox check failed: {exc}")
        typer.echo(f"Inbox check failed: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(
        f"Scanned {stats['scanned']} message(s): {stats['reply']} reply(ies), "
        f"{stats['unsubscribe']} unsubscribe(s), {stats['bounce']} bounce(s), "
        f"{stats['no_match']} unmatched."
    )


@app.command()
def replies(
    lead_id: int = typer.Argument(None, help="Lead ID to show replies for"),
    all_replies: bool = typer.Option(False, "--all", help="Show replies across all leads"),
) -> None:
    """Show stored replies, unsubscribes and bounces."""
    import sys

    from .storage.db import get_lead, init_db, list_replies

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    if lead_id is None and not all_replies:
        typer.echo("Provide a lead_id or use --all.", err=True)
        raise typer.Exit(1)
    if lead_id is not None and all_replies:
        typer.echo("Provide either a lead_id or --all, not both.", err=True)
        raise typer.Exit(1)
    if lead_id is not None and get_lead(lead_id) is None:
        typer.echo(f"Error: Lead #{lead_id} not found", err=True)
        raise typer.Exit(1)

    rows = list_replies(lead_id=None if all_replies else lead_id)
    if not rows:
        typer.echo("No stored replies found.")
        return

    for row in rows:
        typer.echo(f"\n{'─' * 60}")
        typer.echo(
            f"Reply #{row['id']} · {row['kind']} · {row['received_at'][:19]} · "
            f"Lead #{row['lead_id']} {row['company_name']} <{row['contact_email']}>"
        )
        typer.echo(f"From: {row['from_addr']}")
        if row["subject"]:
            typer.echo(f"Subject: {row['subject']}")
        typer.echo()
        typer.echo(row["body_text"])


@app.command()
def outcome(
    lead_id: int = typer.Argument(..., help="Lead ID"),
    value: str = typer.Argument(..., help="Outcome: won, lost or followup"),
) -> None:
    """Mark a lead outcome."""
    import sys

    from .storage.db import get_lead, init_db, set_lead_outcome

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    if value not in {"won", "lost", "followup"}:
        typer.echo("Error: outcome must be one of: won, lost, followup", err=True)
        raise typer.Exit(1)
    if get_lead(lead_id) is None:
        typer.echo(f"Error: Lead #{lead_id} not found", err=True)
        raise typer.Exit(1)
    set_lead_outcome(lead_id, value)
    typer.echo(f"Lead #{lead_id} outcome → {value}.")


@app.command()
def snooze(
    lead_id: int = typer.Argument(..., help="Lead ID"),
    until: str = typer.Option(..., "--until", help="Date to resurface, YYYY-MM-DD"),
) -> None:
    """Snooze a lead until a UTC date."""
    import sys

    from .storage.db import get_lead, init_db, set_lead_outcome

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    try:
        datetime.strptime(until, "%Y-%m-%d")
    except ValueError:
        typer.echo("Error: --until must be YYYY-MM-DD", err=True)
        raise typer.Exit(1)
    if get_lead(lead_id) is None:
        typer.echo(f"Error: Lead #{lead_id} not found", err=True)
        raise typer.Exit(1)
    set_lead_outcome(lead_id, "snoozed", snooze_until=until)
    typer.echo(f"Lead #{lead_id} snoozed until {until}.")


@app.command()
def resurface(
    lead_id: int = typer.Argument(..., help="Lead ID to clear snooze/outcome for"),
) -> None:
    """Clear a lead's outcome and snooze date."""
    import sys

    from .storage.db import get_lead, init_db, set_lead_outcome

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    if get_lead(lead_id) is None:
        typer.echo(f"Error: Lead #{lead_id} not found", err=True)
        raise typer.Exit(1)
    set_lead_outcome(lead_id, None)
    typer.echo(f"Lead #{lead_id} resurfaced.")


@app.command()
def pipeline() -> None:
    """Print a terminal funnel/stats overview of the outbound pipeline."""
    import sys

    from .outbound.stats import get_angle_performance, get_pipeline_stats
    from .storage.db import init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    stats = get_pipeline_stats()

    typer.echo(f"\n{'═' * 50}")
    typer.echo("  PIPELINE OVERVIEW")
    typer.echo(f"{'═' * 50}\n")

    typer.echo("Leads by status:")
    for status, count in stats["funnel"].items():
        typer.echo(f"  {status:<14} {count}")
    typer.echo(f"  {'total':<14} {stats['total_leads']}")

    typer.echo(f"\nActive sequences:   {stats['active_sequences']}")
    typer.echo(f"Steps due today:    {stats['due_today']}")
    typer.echo(f"Sent this week:     {stats['sent_this_week']}")
    typer.echo(f"Due to resurface:   {stats['due_to_resurface']}")

    typer.echo(
        f"\nReply rate:         {stats['reply_rate'] * 100:.1f}%  "
        f"({stats['replied_leads']}/{stats['leads_with_sent_step']} leads with a sent step)"
    )
    typer.echo(
        f"Unsubscribe rate:   {stats['unsubscribe_rate'] * 100:.1f}%  "
        f"({stats['unsubscribed_leads']}/{stats['total_leads']} leads)"
    )
    typer.echo(f"Suppressed:         {stats['suppression_count']}")

    angle_performance = get_angle_performance()
    if angle_performance:
        typer.echo("\nAngle performance:")
        for row in angle_performance:
            typer.echo(
                f"  {row['angle_name'][:28]:<28} "
                f"{row['reply_rate'] * 100:>5.1f}%  "
                f"({row['replies']}/{row['leads_contacted']} contacted)"
            )
    typer.echo(f"\n{'─' * 50}\n")


@app.command()
def dashboard() -> None:
    """Write a self-contained HTML pipeline dashboard to data/dashboard.html."""
    import sys

    from .outbound.dashboard_html import render_dashboard
    from .outbound.stats import get_daily_activity, get_pipeline_stats, get_sequences_overview
    from .storage.db import DB_PATH, init_db

    logger.remove()
    logger.add(sys.stderr, level="WARNING", format="<level>{level}</level> {message}")
    init_db()

    stats = get_pipeline_stats()
    activity = get_daily_activity(days=30)
    sequences = get_sequences_overview()

    html_out = render_dashboard(stats, activity, sequences)

    out_path = DB_PATH.parent / "dashboard.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")

    typer.echo(f"Dashboard written to {out_path}")


if __name__ == "__main__":
    app()
