"""Self-contained HTML template for the `dashboard` command (Phase 7).

No external requests, no CDNs — all CSS inline in <style>, no JS required (pure
CSS bars). Light + dark handled via prefers-color-scheme.
"""

import html
from datetime import datetime, timezone

_STATUS_LABELS = {
    "new": "New",
    "enriched": "Enriched",
    "sequenced": "Sequenced",
    "replied": "Replied",
    "unsubscribed": "Unsubscribed",
    "bounced": "Bounced",
}

_STEP_STATUS_CLASS = {
    "draft": "step-draft",
    "approved": "step-approved",
    "sent": "step-sent",
    "skipped": "step-skipped",
    "cancelled": "step-cancelled",
}


def _esc(value) -> str:
    return html.escape(str(value))


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _funnel_bars(funnel: dict) -> str:
    max_count = max(funnel.values()) or 1
    rows = []
    for status, count in funnel.items():
        width = (count / max_count) * 100 if max_count else 0
        rows.append(
            f"""<div class="funnel-row">
                <span class="funnel-label">{_esc(_STATUS_LABELS.get(status, status))}</span>
                <div class="funnel-track">
                    <div class="funnel-fill funnel-{_esc(status)}" style="width:{width:.1f}%"></div>
                </div>
                <span class="funnel-count">{count}</span>
            </div>"""
        )
    return "\n".join(rows)


def _activity_chart(activity: list[dict]) -> str:
    max_val = max((max(d["sent"], d["replies"]) for d in activity), default=0) or 1
    bars = []
    for day in activity:
        sent_h = (day["sent"] / max_val) * 100 if max_val else 0
        reply_h = (day["replies"] / max_val) * 100 if max_val else 0
        short_day = day["date"][5:]  # MM-DD
        title = f"{day['date']}: {day['sent']} sent, {day['replies']} replied"
        bars.append(
            f"""<div class="activity-col" title="{_esc(title)}">
                <div class="activity-bars">
                    <div class="activity-bar activity-sent" style="height:{sent_h:.1f}%"></div>
                    <div class="activity-bar activity-reply" style="height:{reply_h:.1f}%"></div>
                </div>
                <span class="activity-day">{_esc(short_day)}</span>
            </div>"""
        )
    return "\n".join(bars)


def _sequences_table(sequences: list[dict]) -> str:
    if not sequences:
        return '<p class="empty">No sequences yet.</p>'

    rows = []
    for seq in sequences:
        step_badges = []
        for step in seq["steps"]:
            css = _STEP_STATUS_CLASS.get(step["status"], "step-draft")
            step_badges.append(
                f'<span class="step-badge {css}" title="Step {step["step_number"]}: '
                f'{_esc(step["status"])} (due {_esc(step["due_at"][:10])})">'
                f'{step["step_number"]}</span>'
            )
        rows.append(
            f"""<tr>
                <td>#{seq['sequence_id']}</td>
                <td>{_esc(seq['company_name'])}</td>
                <td>{_esc(seq['contact_email'])}</td>
                <td>{_esc(seq['angle_name'])}</td>
                <td><span class="seq-status seq-{_esc(seq['sequence_status'])}">{_esc(seq['sequence_status'])}</span></td>
                <td class="steps-cell">{''.join(step_badges)}</td>
            </tr>"""
        )

    return f"""<table class="seq-table">
        <thead>
            <tr><th>ID</th><th>Company</th><th>Contact</th><th>Angle</th><th>Status</th><th>Steps</th></tr>
        </thead>
        <tbody>
            {''.join(rows)}
        </tbody>
    </table>"""


def render_dashboard(stats: dict, activity: list[dict], sequences: list[dict]) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    funnel_html = _funnel_bars(stats["funnel"])
    activity_html = _activity_chart(activity)
    sequences_html = _sequences_table(sequences)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cold Email Lab — Pipeline Dashboard</title>
<style>
  :root {{
    --bg: #f7f7f8;
    --fg: #1a1a1e;
    --muted: #6b6b76;
    --card-bg: #ffffff;
    --border: #e2e2e7;
    --accent: #2f6fed;
    --accent-2: #9b59d0;
    --good: #2e9b5f;
    --warn: #d99a1f;
    --bad: #d64545;
    --track: #edeef2;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #14151a;
      --fg: #eceef2;
      --muted: #9a9ba6;
      --card-bg: #1c1e26;
      --border: #2b2d38;
      --accent: #6d9bff;
      --accent-2: #c48cf0;
      --good: #4fd489;
      --warn: #f0c14b;
      --bad: #f0716a;
      --track: #262834;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 2rem;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--fg);
    line-height: 1.4;
  }}
  h1 {{ font-size: 1.5rem; margin: 0 0 0.25rem; }}
  .subtitle {{ color: var(--muted); font-size: 0.85rem; margin: 0 0 2rem; }}
  .card {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.25rem 1.5rem;
    margin-bottom: 1.5rem;
  }}
  .card h2 {{ font-size: 1.05rem; margin: 0 0 1rem; }}
  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 1rem;
  }}
  .stat-tile {{
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.75rem 1rem;
  }}
  .stat-value {{ font-size: 1.6rem; font-weight: 600; }}
  .stat-label {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}

  .funnel-row {{ display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.6rem; }}
  .funnel-label {{ width: 110px; flex-shrink: 0; font-size: 0.85rem; color: var(--muted); }}
  .funnel-track {{ flex: 1; background: var(--track); border-radius: 5px; height: 18px; overflow: hidden; }}
  .funnel-fill {{ height: 100%; border-radius: 5px; background: var(--accent); min-width: 2px; }}
  .funnel-count {{ width: 32px; text-align: right; font-variant-numeric: tabular-nums; font-size: 0.85rem; }}
  .funnel-replied {{ background: var(--good); }}
  .funnel-unsubscribed {{ background: var(--warn); }}
  .funnel-bounced {{ background: var(--bad); }}

  .activity-chart {{
    display: flex;
    align-items: flex-end;
    gap: 3px;
    height: 140px;
    overflow-x: auto;
    padding-bottom: 0.25rem;
  }}
  .activity-col {{ display: flex; flex-direction: column; align-items: center; flex: 0 0 auto; width: 18px; }}
  .activity-bars {{ display: flex; align-items: flex-end; gap: 1px; height: 110px; width: 100%; }}
  .activity-bar {{ width: 8px; border-radius: 2px 2px 0 0; min-height: 1px; }}
  .activity-sent {{ background: var(--accent); }}
  .activity-reply {{ background: var(--accent-2); }}
  .activity-day {{ font-size: 0.6rem; color: var(--muted); margin-top: 0.25rem; writing-mode: vertical-rl; transform: rotate(180deg); height: 26px; }}
  .legend {{ display: flex; gap: 1.25rem; margin-top: 0.75rem; font-size: 0.8rem; color: var(--muted); }}
  .legend-swatch {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 0.4rem; vertical-align: middle; }}

  .seq-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  .seq-table th, .seq-table td {{ text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--border); }}
  .seq-table th {{ color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }}
  .seq-status {{ padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.75rem; background: var(--track); }}
  .seq-active {{ color: var(--accent); }}
  .seq-replied {{ color: var(--good); }}
  .seq-cancelled {{ color: var(--muted); }}
  .seq-completed {{ color: var(--muted); }}
  .steps-cell {{ white-space: nowrap; }}
  .step-badge {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 20px; height: 20px; border-radius: 50%; margin-right: 4px;
    font-size: 0.7rem; font-weight: 600; color: #fff;
  }}
  .step-draft {{ background: var(--muted); }}
  .step-approved {{ background: var(--warn); }}
  .step-sent {{ background: var(--good); }}
  .step-skipped {{ background: var(--muted); opacity: 0.6; }}
  .step-cancelled {{ background: var(--bad); opacity: 0.7; }}
  .empty {{ color: var(--muted); font-size: 0.9rem; }}

  .overflow-x {{ overflow-x: auto; }}
</style>
</head>
<body>
  <h1>Cold Email Lab — Pipeline Dashboard</h1>
  <p class="subtitle">Generated {generated_at} &middot; snapshot only, refresh by re-running <code>dashboard</code></p>

  <div class="card">
    <h2>Key metrics</h2>
    <div class="stat-grid">
      <div class="stat-tile">
        <div class="stat-value">{stats['total_leads']}</div>
        <div class="stat-label">Total leads</div>
      </div>
      <div class="stat-tile">
        <div class="stat-value">{stats['active_sequences']}</div>
        <div class="stat-label">Active sequences</div>
      </div>
      <div class="stat-tile">
        <div class="stat-value">{stats['due_today']}</div>
        <div class="stat-label">Steps due today</div>
      </div>
      <div class="stat-tile">
        <div class="stat-value">{stats['sent_this_week']}</div>
        <div class="stat-label">Sent this week</div>
      </div>
      <div class="stat-tile">
        <div class="stat-value">{_pct(stats['reply_rate'])}</div>
        <div class="stat-label">Reply rate</div>
      </div>
      <div class="stat-tile">
        <div class="stat-value">{_pct(stats['unsubscribe_rate'])}</div>
        <div class="stat-label">Unsubscribe rate</div>
      </div>
      <div class="stat-tile">
        <div class="stat-value">{stats['suppression_count']}</div>
        <div class="stat-label">Suppressed addresses</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Lead funnel</h2>
    {funnel_html}
  </div>

  <div class="card">
    <h2>Activity — last 30 days</h2>
    <div class="overflow-x">
      <div class="activity-chart">
        {activity_html}
      </div>
    </div>
    <div class="legend">
      <span><span class="legend-swatch" style="background:var(--accent)"></span>Sent</span>
      <span><span class="legend-swatch" style="background:var(--accent-2)"></span>Replied</span>
    </div>
  </div>

  <div class="card">
    <h2>Sequences</h2>
    <div class="overflow-x">
      {sequences_html}
    </div>
  </div>
</body>
</html>
"""
