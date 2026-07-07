import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()

from ..outbound.leads import enrich_lead
from ..outbound.sender import (
    approve_sequence,
    approve_step,
    edit_step,
    get_send_cap_status,
    review_steps,
    send_tick,
)
from ..outbound.sequences import create_sequence_for_lead
from ..outbound.sourcing import PROFILES_DIR, source_leads
from ..outbound.stats import get_angle_performance, get_daily_activity, get_pipeline_stats, get_sequences_overview
from ..storage.db import (
    count_draft_steps,
    get_kv,
    get_lead,
    get_lead_outreach_sets,
    get_lead_research_brief,
    get_lead_sequences,
    get_recent_activity,
    get_sequence_steps,
    init_db,
    lead_has_active_sequence,
    list_lead_sources,
    list_leads,
    list_replies,
    set_kv,
    delete_kv,
    set_lead_outcome,
)
from ..storage.models import CompanyBrief, OutreachAngle

BASE_DIR = Path(__file__).parent
JOB_LOCK_KEY = "web_job_lock"
JOB_PROGRESS_KEY = "web_job_progress"
SEND_RESULT_KEY = "web_send_result"

app = FastAPI(title="Cold Email Lab")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _flash(message: str) -> RedirectResponse:
    set_kv("web_flash", message)
    return _redirect("/")


def _pop_flash() -> str | None:
    value = get_kv("web_flash")
    if value:
        delete_kv("web_flash")
    return value


def _fmt_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%-d %b")
    except Exception:
        return value[:10]


def _fmt_dt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%-d %b %H:%M")
    except Exception:
        return value[:19]


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _asdict(row) -> dict:
    return dict(row) if row is not None else {}


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {k: v[-1] if v else "" for k, v in parsed.items()}


def _send_results_text(results: list[dict], live: bool) -> str:
    if not results:
        return "No due steps to send."
    out: list[str] = []
    mode = "LIVE" if live else "DRY-RUN"
    step_results = [r for r in results if r.get("kind", "step") == "step"]
    for r in results:
        out.append("\n" + "─" * 60)
        if r.get("kind") == "resurface_notice":
            out.append(f"{r['count']} snoozed lead(s) are due to resurface:")
            for lead in r["leads"]:
                out.append(
                    f"  Lead #{lead['lead_id']} — {lead['company_name']} "
                    f"<{lead['contact_email']}> (snoozed until {lead['snooze_until']})"
                )
            continue
        status_label = "DEFERRED" if r["status"] == "deferred" else mode
        out.append(f"[{status_label}] Step #{r['step_id']} → {r['company_name']} <{r['to']}>")
        out.append(f"Subject: {r['subject']}")
        if r["status"] == "deferred":
            cap = r["cap"]
            out.append(
                f"Deferred: daily cap ({cap['sent_today']}/{cap['cap']} already sent today; "
                f"source: {cap['source']})"
            )
        else:
            out.extend(["", r["body"]])
        if live:
            if r["status"] == "sent":
                out.append(f"\n[SENT] Message-ID: {r.get('smtp_message_id')}")
            elif r["status"] == "failed":
                out.append(f"\n[FAILED] {r.get('error')}")
    out.append("\n" + "─" * 60)
    if live:
        sent = sum(1 for r in step_results if r["status"] == "sent")
        deferred = sum(1 for r in step_results if r["status"] == "deferred")
        out.append(f"{sent}/{len(step_results)} step(s) sent; {deferred} deferred by cap.")
    else:
        deferred = sum(1 for r in step_results if r["status"] == "deferred")
        out.append(f"{len(step_results)} due step(s); {deferred} deferred by cap — dry-run only, nothing sent or changed.")
    return "\n".join(out).strip()


def _base_context(request: Request, active: str, title: str, subtitle: str = "") -> dict:
    return {
        "request": request,
        "active": active,
        "title": title,
        "subtitle": subtitle,
        "draft_count": count_draft_steps(),
        "flash": _pop_flash(),
        "job": _job_progress(),
        "send_result": get_kv(SEND_RESULT_KEY),
    }


def _profile_names() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))


def _job_progress() -> dict | None:
    raw = get_kv(JOB_PROGRESS_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"state": "error", "done": 0, "total": 0, "inserted": 0, "error": raw}


def _start_job(kind: str, total: int, target, *args) -> bool:
    if get_kv(JOB_LOCK_KEY):
        return False
    set_kv(JOB_LOCK_KEY, kind)
    set_kv(JOB_PROGRESS_KEY, json.dumps({"state": "running", "kind": kind, "done": 0, "total": total, "inserted": 0}))

    def runner() -> None:
        try:
            result = asyncio.run(target(*args))
            inserted = getattr(result, "inserted", 0)
            total_done = len(getattr(result, "candidates", []) or []) or total
            set_kv(
                JOB_PROGRESS_KEY,
                json.dumps({"state": "complete", "kind": kind, "done": total_done, "total": total, "inserted": inserted}),
            )
        except Exception as exc:
            set_kv(
                JOB_PROGRESS_KEY,
                json.dumps({"state": "error", "kind": kind, "done": 0, "total": total, "inserted": 0, "error": str(exc)}),
            )
        finally:
            delete_kv(JOB_LOCK_KEY)

    threading.Thread(target=runner, daemon=True).start()
    return True


templates.env.filters["date"] = _fmt_date
templates.env.filters["dt"] = _fmt_dt
templates.env.filters["pct"] = _pct


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def overview(request: Request) -> HTMLResponse:
    stats = get_pipeline_stats()
    activity = get_daily_activity(days=30)
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            **_base_context(request, "overview", "Overview", "Campaign engine · last 30 days"),
            "stats": stats,
            "activity": activity,
            "recent": get_recent_activity(),
            "angles": get_angle_performance(),
            "sources": list_lead_sources(),
            "profiles": _profile_names(),
        },
    )


@app.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            **_base_context(request, "review", "Review queue", "Approve or edit before anything sends"),
            "steps": review_steps(),
        },
    )


@app.post("/steps/{step_id}/approve")
def approve_step_route(step_id: int):
    try:
        approve_step(step_id)
        return _flash(f"Step #{step_id} approved.")
    except ValueError as exc:
        return _flash(f"Error: {exc}")


@app.post("/sequences/{sequence_id}/approve")
def approve_sequence_route(sequence_id: int):
    try:
        count = approve_sequence(sequence_id)
        return _flash(f"Approved {count} step(s) in sequence #{sequence_id}.")
    except ValueError as exc:
        return _flash(f"Error: {exc}")


@app.post("/steps/{step_id}/edit")
async def edit_step_route(request: Request, step_id: int):
    data = await _form(request)
    try:
        edit_step(step_id, subject=data.get("subject") or None, body=data.get("body") or None)
        return _flash(f"Step #{step_id} updated.")
    except ValueError as exc:
        return _flash(f"Error: {exc}")


@app.get("/leads", response_class=HTMLResponse)
def leads(request: Request, status: str | None = None, source: str | None = None) -> HTMLResponse:
    rows = list_leads(status=status or None, source=source or None, limit=500)
    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            **_base_context(request, "leads", "Leads", f"{len(rows)} leads in view"),
            "leads": rows,
            "sources": list_lead_sources(),
            "selected_status": status or "",
            "selected_source": source or "",
        },
    )


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: int) -> HTMLResponse:
    lead = get_lead(lead_id)
    if lead is None:
        return templates.TemplateResponse(
            request,
            "message.html",
            {**_base_context(request, "leads", "Lead not found"), "message": f"Lead #{lead_id} was not found."},
            status_code=404,
        )
    brief_row = get_lead_research_brief(lead_id)
    brief = None
    if brief_row:
        try:
            brief = CompanyBrief.model_validate_json(brief_row["brief_json"])
        except Exception:
            brief = None
    angles = []
    for idx, row in enumerate(get_lead_outreach_sets(lead_id), 1):
        try:
            angles.append({"number": idx, "angle": OutreachAngle.model_validate_json(row["angle_json"])})
        except Exception:
            pass
    sequences = []
    for seq in get_lead_sequences(lead_id):
        sequences.append({"row": seq, "steps": get_sequence_steps(seq["id"])})
    return templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            **_base_context(request, "leads", lead["company_name"], lead["contact_email"]),
            "lead": lead,
            "brief": brief,
            "angles": angles,
            "sequences": sequences,
            "replies": list_replies(lead_id=lead_id),
            "has_active_sequence": lead_has_active_sequence(lead_id),
        },
    )


@app.post("/leads/{lead_id}/sequence")
async def create_sequence_route(request: Request, lead_id: int):
    data = await _form(request)
    try:
        angle = int(data.get("angle", "0"))
        sequence_id = create_sequence_for_lead(lead_id, angle)
        return _flash(f"Sequence #{sequence_id} created for lead #{lead_id}.")
    except ValueError as exc:
        return _flash(f"Error: {exc}")


@app.post("/leads/{lead_id}/outcome")
async def outcome_route(request: Request, lead_id: int):
    value = (await _form(request)).get("value", "")
    if value not in {"won", "lost", "followup"}:
        return _flash("Error: outcome must be won, lost or followup.")
    set_lead_outcome(lead_id, value)
    return _flash(f"Lead #{lead_id} outcome set to {value}.")


@app.post("/leads/{lead_id}/snooze")
async def snooze_route(request: Request, lead_id: int):
    until = (await _form(request)).get("until", "")
    try:
        datetime.strptime(until, "%Y-%m-%d")
    except ValueError:
        return _flash("Error: snooze date must be YYYY-MM-DD.")
    set_lead_outcome(lead_id, "snoozed", snooze_until=until)
    return _flash(f"Lead #{lead_id} snoozed until {until}.")


@app.post("/leads/{lead_id}/resurface")
def resurface_route(lead_id: int):
    set_lead_outcome(lead_id, None)
    return _flash(f"Lead #{lead_id} resurfaced.")


@app.post("/leads/{lead_id}/enrich")
def enrich_route(lead_id: int):
    ok = _start_job("enrich", 1, enrich_lead, lead_id)
    return _flash("Enrichment started." if ok else "Another background job is already running.")


@app.get("/sequences", response_class=HTMLResponse)
def sequences(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sequences.html",
        {
            **_base_context(request, "sequences", "Sequences", "Dot timelines for active and historical outreach"),
            "sequences": get_sequences_overview(limit=500),
        },
    )


@app.get("/replies", response_class=HTMLResponse)
def replies(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "replies.html",
        {
            **_base_context(request, "replies", "Replies", "Matched replies, unsubscribes and bounces"),
            "replies": list_replies(limit=500),
        },
    )


@app.post("/send/dry-run")
def send_dry_run():
    results = send_tick(live=False)
    set_kv(SEND_RESULT_KEY, _send_results_text(results, live=False))
    return _redirect("/send/result")


@app.get("/send/result", response_class=HTMLResponse)
def send_result(request: Request):
    return templates.TemplateResponse(
        request,
        "send_result.html",
        {**_base_context(request, "overview", "Send tick result", "Dry-run output from the send engine")},
    )


@app.get("/send/live/confirm", response_class=HTMLResponse)
def send_live_confirm(request: Request):
    dry_results = send_tick(live=False)
    due = [r for r in dry_results if r.get("kind", "step") == "step" and r["status"] != "deferred"]
    return templates.TemplateResponse(
        request,
        "send_confirm.html",
        {
            **_base_context(request, "overview", "Confirm live send", "This will attempt SMTP delivery"),
            "due_count": len(due),
            "cap": get_send_cap_status(),
            "recipients": due,
        },
    )


@app.post("/send/live")
def send_live():
    try:
        results = send_tick(live=True)
        set_kv(SEND_RESULT_KEY, _send_results_text(results, live=True))
    except ValueError as exc:
        set_kv(SEND_RESULT_KEY, f"Error: {exc}")
    return _redirect("/send/result")


@app.post("/source")
async def source_route(request: Request):
    data = await _form(request)
    profile = data.get("profile", "")
    try:
        limit = int(data.get("limit") or "2")
    except ValueError:
        limit = 2
    ok = _start_job("source", limit, source_leads, profile, limit, False)
    return _flash("Lead sourcing started." if ok else "Another background job is already running.")


@app.get("/job/progress")
def job_progress():
    return JSONResponse(_job_progress() or {"state": "idle", "done": 0, "total": 0, "inserted": 0})
