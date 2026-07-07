"""Small stdlib fallback for environments where Phase 11 web deps cannot be installed."""

import asyncio
import json
import threading
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..outbound.leads import enrich_lead
from ..outbound.sender import approve_sequence, approve_step, edit_step, get_send_cap_status, review_steps, send_tick
from ..outbound.sequences import create_sequence_for_lead
from ..outbound.sourcing import PROFILES_DIR, source_leads
from ..outbound.stats import get_angle_performance, get_pipeline_stats, get_sequences_overview
from ..storage.db import (
    count_draft_steps,
    get_kv,
    get_lead,
    get_lead_outreach_sets,
    get_lead_sequences,
    get_sequence_steps,
    init_db,
    list_leads,
    list_replies,
    set_kv,
    delete_kv,
    set_lead_outcome,
)
from ..storage.models import OutreachAngle

JOB_LOCK_KEY = "web_job_lock"
JOB_PROGRESS_KEY = "web_job_progress"
SEND_RESULT_KEY = "web_send_result"


def _layout(title: str, body: str) -> bytes:
    nav = (
        '<a href="/">Overview</a> · <a href="/review">Review queue</a> · '
        '<a href="/leads">Leads</a> · <a href="/sequences">Sequences</a> · <a href="/replies">Replies</a>'
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>
    <style>
    body{{font:15px/1.55 system-ui;background:#F6F8FB;color:#0F1B33;margin:24px}}a{{color:#2563EB}}
    .card{{background:white;border:1px solid #E9EDF5;border-radius:10px;padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(15,27,51,.05)}}
    table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #E9EDF5;padding:8px;text-align:left;vertical-align:top}}
    .pill{{border-radius:99px;background:#EDF0F6;padding:2px 8px;font-size:12px;font-weight:650}}.btn{{border:1px solid #DDE3EE;border-radius:8px;padding:7px 12px;background:white;cursor:pointer}}
    textarea,input,select{{width:100%;padding:8px;border:1px solid #DDE3EE;border-radius:8px}}pre{{white-space:pre-wrap;background:#EDF0F6;padding:12px;border-radius:8px}}
    </style></head><body><h1>{escape(title)}</h1><nav>{nav}</nav>{body}</body></html>"""
    return html.encode("utf-8")


def _redirect(handler, path: str) -> None:
    handler.send_response(303)
    handler.send_header("Location", path)
    handler.end_headers()


def _send_text(handler, text: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
    data = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _send_html(handler, title: str, body: str, status: int = 200) -> None:
    data = _layout(title, body)
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _form(handler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: v[-1] if v else "" for k, v in parsed.items()}


def _job_progress() -> dict:
    raw = get_kv(JOB_PROGRESS_KEY)
    if not raw:
        return {"state": "idle", "done": 0, "total": 0, "inserted": 0}
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
            done = len(getattr(result, "candidates", []) or []) or total
            set_kv(JOB_PROGRESS_KEY, json.dumps({"state": "complete", "kind": kind, "done": done, "total": total, "inserted": inserted}))
        except Exception as exc:
            set_kv(JOB_PROGRESS_KEY, json.dumps({"state": "error", "kind": kind, "done": 0, "total": total, "inserted": 0, "error": str(exc)}))
        finally:
            delete_kv(JOB_LOCK_KEY)

    threading.Thread(target=runner, daemon=True).start()
    return True


def _send_results_text(results: list[dict], live: bool) -> str:
    if not results:
        return "No due steps to send."
    lines = []
    mode = "LIVE" if live else "DRY-RUN"
    steps = [r for r in results if r.get("kind", "step") == "step"]
    for r in results:
        lines.append("\n" + "─" * 60)
        if r.get("kind") == "resurface_notice":
            lines.append(f"{r['count']} snoozed lead(s) are due to resurface:")
            continue
        label = "DEFERRED" if r["status"] == "deferred" else mode
        lines.append(f"[{label}] Step #{r['step_id']} → {r['company_name']} <{r['to']}>")
        lines.append(f"Subject: {r['subject']}")
        if r["status"] == "deferred":
            cap = r["cap"]
            lines.append(f"Deferred: daily cap ({cap['sent_today']}/{cap['cap']} already sent today; source: {cap['source']})")
        else:
            lines.extend(["", r["body"]])
    lines.append("\n" + "─" * 60)
    if live:
        sent = sum(1 for r in steps if r["status"] == "sent")
        deferred = sum(1 for r in steps if r["status"] == "deferred")
        lines.append(f"{sent}/{len(steps)} step(s) sent; {deferred} deferred by cap.")
    else:
        deferred = sum(1 for r in steps if r["status"] == "deferred")
        lines.append(f"{len(steps)} due step(s); {deferred} deferred by cap — dry-run only, nothing sent or changed.")
    return "\n".join(lines).strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        init_db()
        path = urlparse(self.path).path
        if path == "/job/progress":
            _send_text(self, json.dumps(_job_progress()), content_type="application/json")
            return
        if path == "/":
            stats = get_pipeline_stats()
            angles = get_angle_performance()
            profiles = "".join(f'<option value="{escape(p.stem)}">{escape(p.stem)}</option>' for p in PROFILES_DIR.glob("*.toml"))
            body = f"""<div class="card"><b>Leads</b> {stats['total_leads']} · <b>Drafts</b> {count_draft_steps()} · <b>Active sequences</b> {stats['active_sequences']} · <b>Reply rate</b> {stats['reply_rate']*100:.0f}%</div>
            <div class="card"><h2>Angle performance</h2><table>{''.join(f"<tr><td>{escape(a['angle_name'])}</td><td>{a['replies']}/{a['leads_contacted']}</td><td>{a['reply_rate']*100:.0f}%</td></tr>" for a in angles) or '<tr><td>No angle data yet.</td></tr>'}</table></div>
            <div class="card"><h2>Source leads</h2><form method="post" action="/source"><select name="profile">{profiles}</select><input name="limit" value="2"><button class="btn">Source leads</button></form><p>Job: {escape(json.dumps(_job_progress()))}</p></div>
            <form method="post" action="/send/dry-run"><button class="btn">Run send tick</button></form>"""
            _send_html(self, "Overview", body)
            return
        if path == "/review":
            rows = review_steps()
            body = "".join(f"""<div class="card"><b>{escape(r['company_name'])}</b> <span class="pill">draft</span><p>Subject: {escape(r['subject'])}</p><pre>{escape(r['body'])}</pre>
            <form method="post" action="/steps/{r['id']}/approve"><button class="btn">Approve</button></form>
            <form method="post" action="/steps/{r['id']}/edit"><input name="subject" value="{escape(r['subject'])}"><textarea name="body">{escape(r['body'])}</textarea><button class="btn">Save draft</button></form></div>""" for r in rows) or "<div class='card'>No draft steps pending.</div>"
            _send_html(self, "Review queue", body); return
        if path == "/leads":
            qs = parse_qs(urlparse(self.path).query)
            rows = list_leads(status=(qs.get("status", [""])[0] or None), source=(qs.get("source", [""])[0] or None), limit=500)
            body = "<div class='card'><table><tr><th>Company</th><th>Email</th><th>Status</th><th>Fit</th><th></th></tr>" + "".join(
                f"<tr><td>{escape(r['company_name'])}</td><td>{escape(r['contact_email'])}</td><td><span class='pill'>{escape(r['status'])}</span></td><td>{r['fit_score'] if r['fit_score'] is not None else '—'}</td><td><a href='/leads/{r['id']}'>Open</a></td></tr>" for r in rows
            ) + "</table></div>"
            _send_html(self, "Leads", body); return
        if path.startswith("/leads/"):
            lead_id = int(path.rsplit("/", 1)[-1])
            lead = get_lead(lead_id)
            if not lead:
                _send_html(self, "Lead not found", "Lead not found", status=404); return
            angles = []
            for i, row in enumerate(get_lead_outreach_sets(lead_id), 1):
                try:
                    angle = OutreachAngle.model_validate_json(row["angle_json"])
                    angles.append(f"<div class='card'><h2>Angle {i} — {escape(angle.angle_name)}</h2><p>{escape(angle.angle_rationale)}</p><form method='post' action='/leads/{lead_id}/sequence'><input type='hidden' name='angle' value='{i}'><button class='btn'>Create sequence with this angle</button></form></div>")
                except Exception:
                    pass
            body = f"<div class='card'><b>{escape(lead['company_name'])}</b><p>{escape(lead['contact_email'])}</p><p>{escape(lead['notes'] or '')}</p></div>{''.join(angles) or '<div class=card>No drafted angles yet.</div>'}"
            body += f"<div class='card'><form method='post' action='/leads/{lead_id}/outcome'><button name='value' value='won' class='btn'>won</button><button name='value' value='lost' class='btn'>lost</button><button name='value' value='followup' class='btn'>followup</button></form><form method='post' action='/leads/{lead_id}/snooze'><input type='date' name='until'><button class='btn'>Snooze</button></form><form method='post' action='/leads/{lead_id}/enrich'><button class='btn'>Enrich lead</button></form></div>"
            _send_html(self, lead["company_name"], body); return
        if path == "/sequences":
            seqs = get_sequences_overview(500)
            body = "<div class='card'>" + "".join(f"<p><b>{escape(s['company_name'])}</b> · {escape(s['angle_name'])} · {escape(s['sequence_status'])}</p>" for s in seqs) + "</div>"
            _send_html(self, "Sequences", body); return
        if path == "/replies":
            reps = list_replies(limit=500)
            body = "<div class='card'><table>" + "".join(f"<tr><td>{escape(r['kind'])}</td><td><a href='/leads/{r['lead_id']}'>{escape(r['company_name'])}</a></td><td>{escape(r['body_text'])}</td></tr>" for r in reps) + "</table></div>"
            _send_html(self, "Replies", body); return
        if path == "/send/result":
            _send_html(self, "Send tick result", f"<div class='card'><pre>{escape(get_kv(SEND_RESULT_KEY) or '')}</pre></div>"); return
        if path == "/send/live/confirm":
            cap = get_send_cap_status()
            _send_html(self, "Confirm live send", f"<div class='card'>Cap: {cap['sent_today']}/{cap['cap']} ({cap['source']})<form method='post' action='/send/live'><button class='btn'>Send live now</button></form></div>"); return
        _send_html(self, "Not found", "Not found", status=404)

    def do_POST(self):
        init_db()
        path = urlparse(self.path).path
        data = _form(self)
        try:
            if path == "/send/dry-run":
                set_kv(SEND_RESULT_KEY, _send_results_text(send_tick(live=False), False)); _redirect(self, "/send/result"); return
            if path == "/send/live":
                try:
                    set_kv(SEND_RESULT_KEY, _send_results_text(send_tick(live=True), True))
                except ValueError as exc:
                    set_kv(SEND_RESULT_KEY, f"Error: {exc}")
                _redirect(self, "/send/result"); return
            if path == "/source":
                _start_job("source", int(data.get("limit") or "2"), source_leads, data.get("profile", "leadflow"), int(data.get("limit") or "2"), False); _redirect(self, "/"); return
            if path.startswith("/steps/") and path.endswith("/approve"):
                approve_step(int(path.split("/")[2])); _redirect(self, "/review"); return
            if path.startswith("/steps/") and path.endswith("/edit"):
                edit_step(int(path.split("/")[2]), subject=data.get("subject"), body=data.get("body")); _redirect(self, "/review"); return
            if path.startswith("/sequences/") and path.endswith("/approve"):
                approve_sequence(int(path.split("/")[2])); _redirect(self, "/review"); return
            if path.startswith("/leads/"):
                lead_id = int(path.split("/")[2])
                if path.endswith("/sequence"):
                    create_sequence_for_lead(lead_id, int(data.get("angle") or "0"))
                elif path.endswith("/outcome"):
                    set_lead_outcome(lead_id, data.get("value"))
                elif path.endswith("/snooze"):
                    set_lead_outcome(lead_id, "snoozed", data.get("until"))
                elif path.endswith("/resurface"):
                    set_lead_outcome(lead_id, None)
                elif path.endswith("/enrich"):
                    _start_job("enrich", 1, enrich_lead, lead_id)
                _redirect(self, f"/leads/{lead_id}"); return
        except Exception as exc:
            _send_html(self, "Error", escape(str(exc)), status=500)
            return
        _redirect(self, "/")


def run_fallback_server(port: int) -> None:
    url = f"http://127.0.0.1:{port}"
    print(f"Web dashboard running at {url}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
