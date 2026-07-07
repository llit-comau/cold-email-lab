import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

DB_PATH = Path(os.getenv("COLD_EMAIL_LAB_DB_PATH", "data/cold-email-lab.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'running',
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    total_input_tokens  INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_cost_usd      REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS scraped_pages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    url          TEXT NOT NULL,
    page_type    TEXT,
    content_text TEXT,
    scraped_at   TEXT NOT NULL,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS research_briefs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    brief_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach_sets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    angle_name TEXT NOT NULL,
    angle_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Phase 5: leads & sequences

CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name  TEXT NOT NULL,
    url           TEXT NOT NULL,
    contact_name  TEXT,
    contact_email TEXT NOT NULL UNIQUE,
    source        TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    run_id        INTEGER REFERENCES runs(id),
    created_at    TEXT NOT NULL,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS suppression (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL UNIQUE,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequences (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id),
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    angle_name TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id     INTEGER NOT NULL REFERENCES sequences(id),
    step_number     INTEGER NOT NULL,
    due_at          TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',
    sent_at         TEXT,
    smtp_message_id TEXT
);

-- Phase 6: kv store (e.g. inbox watermark)

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Phase 7: pipeline events (for dashboard activity chart — reply/unsubscribe/bounce
-- have no timestamp elsewhere; 'sent' activity is read straight off sequence_steps.sent_at)

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL, -- 'replied' | 'unsubscribed' | 'bounced'
    lead_id     INTEGER NOT NULL REFERENCES leads(id),
    sequence_id INTEGER REFERENCES sequences(id),
    created_at  TEXT NOT NULL
);

-- Phase 10: stored replies for the web dashboard

CREATE TABLE IF NOT EXISTS replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES leads(id),
    sequence_id INTEGER REFERENCES sequences(id),
    from_addr   TEXT NOT NULL,
    subject     TEXT,
    body_text   TEXT NOT NULL,
    kind        TEXT NOT NULL, -- 'reply' | 'bounce' | 'unsubscribe'
    received_at TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_fit_score_column(conn: sqlite3.Connection) -> None:
    """Phase 8: additive, guarded ALTER — never destructive. Adds leads.fit_score if missing."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if "fit_score" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN fit_score INTEGER")
        logger.info("Added fit_score column to leads table")


def _ensure_phase10_lead_columns(conn: sqlite3.Connection) -> None:
    """Phase 10: additive, guarded ALTERs for lead outcomes and snoozing."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if "outcome" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN outcome TEXT")
        logger.info("Added outcome column to leads table")
    if "snooze_until" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN snooze_until TEXT")
        logger.info("Added snooze_until column to leads table")


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _ensure_fit_score_column(conn)
        _ensure_phase10_lead_columns(conn)
    logger.debug(f"Database ready at {DB_PATH}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(url: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (url, status, started_at) VALUES (?, 'running', ?)",
            (url, _now()),
        )
        run_id = cur.lastrowid
    logger.debug(f"Created run #{run_id} for {url}")
    return run_id


def save_scraped_page(
    run_id: int,
    url: str,
    page_type: str,
    content_text: str,
    error: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO scraped_pages (run_id, url, page_type, content_text, scraped_at, error)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, url, page_type, content_text, _now(), error),
        )
        return cur.lastrowid


def update_run_status(run_id: int, status: str) -> None:
    completed_at = _now() if status in ("completed", "scraped", "briefed", "failed") else None
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?",
            (status, completed_at, run_id),
        )
    logger.debug(f"Run #{run_id} status → {status}")


def update_run_tokens(run_id: int, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE runs
               SET total_input_tokens  = total_input_tokens  + ?,
                   total_output_tokens = total_output_tokens + ?,
                   total_cost_usd      = total_cost_usd      + ?
               WHERE id = ?""",
            (input_tokens, output_tokens, cost_usd, run_id),
        )


def save_research_brief(run_id: int, brief_json: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO research_briefs (run_id, brief_json, created_at) VALUES (?, ?, ?)",
            (run_id, brief_json, _now()),
        )
        return cur.lastrowid


def save_outreach_set(run_id: int, angle_name: str, angle_json: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO outreach_sets (run_id, angle_name, angle_json, created_at) VALUES (?, ?, ?, ?)",
            (run_id, angle_name, angle_json, _now()),
        )
        return cur.lastrowid


def get_run(run_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def get_scraped_pages(run_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM scraped_pages WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()


def get_research_brief(run_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM research_briefs WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()


def list_runs(limit: int = 20) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()


def get_outreach_sets(run_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM outreach_sets WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()


# --- Phase 5: leads & sequences ---


def create_lead(
    company_name: str,
    url: str,
    contact_email: str,
    contact_name: str | None = None,
    source: str | None = None,
    notes: str | None = None,
    fit_score: int | None = None,
) -> int:
    """Insert a new lead. Raises sqlite3.IntegrityError if contact_email already exists."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO leads (company_name, url, contact_name, contact_email, source, status, created_at, notes, fit_score)
               VALUES (?, ?, ?, ?, ?, 'new', ?, ?, ?)""",
            (company_name, url, contact_name, contact_email, source, _now(), notes, fit_score),
        )
        lead_id = cur.lastrowid
    logger.debug(f"Created lead #{lead_id} ({contact_email})")
    return lead_id


def get_lead(lead_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def get_lead_by_email(email: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM leads WHERE contact_email = ?", (email,)
        ).fetchone()


def list_leads(status: str | None = None, source: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    """List leads, optionally filtered by status and/or source.

    Ordered by fit_score DESC with NULLs last (rows without a score), then by
    recency, so scored leads (Phase 8) surface first without hiding unscored ones.
    """
    clauses = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source:
        clauses.append("source = ?")
        params.append(source)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _connect() as conn:
        return conn.execute(
            f"""SELECT * FROM leads {where}
                ORDER BY (fit_score IS NULL), fit_score DESC, created_at DESC
                LIMIT ?""",
            params,
        ).fetchall()


def get_all_lead_urls() -> list[str]:
    with _connect() as conn:
        return [r["url"] for r in conn.execute("SELECT url FROM leads").fetchall()]


def get_all_suppressed_emails() -> list[str]:
    with _connect() as conn:
        return [r["email"] for r in conn.execute("SELECT email FROM suppression").fetchall()]


def update_lead_status(lead_id: int, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    logger.debug(f"Lead #{lead_id} status → {status}")


def update_lead_run(lead_id: int, run_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE leads SET run_id = ? WHERE id = ?", (run_id, lead_id))


def set_lead_outcome(lead_id: int, outcome: str | None, snooze_until: str | None = None) -> bool:
    """Set or clear a lead outcome. Returns True if a lead row exists."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE leads SET outcome = ?, snooze_until = ? WHERE id = ?",
            (outcome, snooze_until, lead_id),
        )
        updated = cur.rowcount > 0
    if updated:
        logger.debug(f"Lead #{lead_id} outcome → {outcome or 'none'}")
    return updated


def get_snoozed_leads_due(today_iso: str) -> list[sqlite3.Row]:
    """Snoozed leads whose date has arrived and should be reviewed/resurfaced."""
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM leads
               WHERE outcome = 'snoozed'
                 AND snooze_until IS NOT NULL
                 AND snooze_until <= ?
               ORDER BY snooze_until, id""",
            (today_iso,),
        ).fetchall()


def add_suppression(email: str, reason: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO suppression (email, reason, created_at) VALUES (?, ?, ?)",
            (email, reason, _now()),
        )
    logger.debug(f"Suppressed {email} ({reason})")


def is_suppressed(email: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM suppression WHERE email = ?", (email,)
        ).fetchone()
        return row is not None


def create_sequence(lead_id: int, run_id: int, angle_name: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO sequences (lead_id, run_id, angle_name, status, created_at)
               VALUES (?, ?, ?, 'active', ?)""",
            (lead_id, run_id, angle_name, _now()),
        )
        return cur.lastrowid


def create_sequence_step(
    sequence_id: int,
    step_number: int,
    due_at: str,
    subject: str,
    body: str,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO sequence_steps (sequence_id, step_number, due_at, subject, body, status)
               VALUES (?, ?, ?, ?, ?, 'draft')""",
            (sequence_id, step_number, due_at, subject, body),
        )
        return cur.lastrowid


def get_sequence(sequence_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM sequences WHERE id = ?", (sequence_id,)
        ).fetchone()


def get_sequence_steps(sequence_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY step_number",
            (sequence_id,),
        ).fetchall()


# --- Phase 6: approval, sending, reply detection ---

_STEP_JOIN = """
    SELECT ss.*, sq.lead_id AS lead_id, sq.angle_name AS angle_name,
           sq.status AS sequence_status, l.company_name AS company_name,
           l.contact_email AS contact_email
    FROM sequence_steps ss
    JOIN sequences sq ON ss.sequence_id = sq.id
    JOIN leads l ON sq.lead_id = l.id
"""


def get_sequence_step(step_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM sequence_steps WHERE id = ?", (step_id,)
        ).fetchone()


def list_draft_steps(sequence_id: int | None = None) -> list[sqlite3.Row]:
    with _connect() as conn:
        if sequence_id is not None:
            return conn.execute(
                _STEP_JOIN + " WHERE ss.status = 'draft' AND ss.sequence_id = ? "
                "ORDER BY ss.sequence_id, ss.step_number",
                (sequence_id,),
            ).fetchall()
        return conn.execute(
            _STEP_JOIN + " WHERE ss.status = 'draft' ORDER BY ss.sequence_id, ss.step_number"
        ).fetchall()


def list_due_steps(now_iso: str) -> list[sqlite3.Row]:
    """Approved steps that are due, belonging to still-active sequences."""
    with _connect() as conn:
        return conn.execute(
            _STEP_JOIN + """
            WHERE ss.status = 'approved' AND ss.due_at <= ? AND sq.status = 'active'
            ORDER BY ss.due_at
            """,
            (now_iso,),
        ).fetchall()


def approve_step(step_id: int) -> bool:
    """Mark a single draft step approved. Returns True if a row was updated."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE sequence_steps SET status = 'approved' WHERE id = ? AND status = 'draft'",
            (step_id,),
        )
        return cur.rowcount > 0


def approve_sequence_steps(sequence_id: int) -> int:
    """Mark all draft steps in a sequence approved. Returns count updated."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE sequence_steps SET status = 'approved' WHERE sequence_id = ? AND status = 'draft'",
            (sequence_id,),
        )
        return cur.rowcount


def update_step_content(step_id: int, subject: str | None = None, body: str | None = None) -> None:
    with _connect() as conn:
        if subject is not None:
            conn.execute("UPDATE sequence_steps SET subject = ? WHERE id = ?", (subject, step_id))
        if body is not None:
            conn.execute("UPDATE sequence_steps SET body = ? WHERE id = ?", (body, step_id))


def update_step_status(
    step_id: int,
    status: str,
    sent_at: str | None = None,
    smtp_message_id: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE sequence_steps
               SET status = ?, sent_at = COALESCE(?, sent_at),
                   smtp_message_id = COALESCE(?, smtp_message_id)
               WHERE id = ?""",
            (status, sent_at, smtp_message_id, step_id),
        )
    logger.debug(f"Step #{step_id} status → {status}")


def update_sequence_status(sequence_id: int, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE sequences SET status = ? WHERE id = ?", (status, sequence_id))
    logger.debug(f"Sequence #{sequence_id} status → {status}")


def cancel_pending_steps(sequence_id: int) -> int:
    """Cancel any draft/approved steps in a sequence (e.g. after a reply). Returns count cancelled."""
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE sequence_steps SET status = 'cancelled'
               WHERE sequence_id = ? AND status IN ('draft', 'approved')""",
            (sequence_id,),
        )
        return cur.rowcount


def get_step_by_message_id(message_id: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            _STEP_JOIN + " WHERE ss.smtp_message_id = ?", (message_id,)
        ).fetchone()


def get_active_sequence_by_lead_email(email: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            """SELECT sq.* FROM sequences sq
               JOIN leads l ON sq.lead_id = l.id
               WHERE l.contact_email = ? AND sq.status = 'active'
               ORDER BY sq.created_at DESC LIMIT 1""",
            (email,),
        ).fetchone()


def get_kv(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_kv(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def delete_kv(key: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))


# --- Phase 7: pipeline dashboard & events ---


def log_event(event_type: str, lead_id: int, sequence_id: int | None = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO events (event_type, lead_id, sequence_id, created_at) VALUES (?, ?, ?, ?)",
            (event_type, lead_id, sequence_id, _now()),
        )
        return cur.lastrowid


def count_leads_by_status() -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS c FROM leads GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}


def count_active_sequences() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM sequences WHERE status = 'active'").fetchone()
        return row["c"]


def count_steps_due_today() -> int:
    """Draft/approved steps whose due_at falls within today (UTC)."""
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM sequence_steps
               WHERE status IN ('draft', 'approved') AND due_at >= ? AND due_at < ?""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        return row["c"]


def count_steps_sent_since(since_iso: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM sequence_steps WHERE status = 'sent' AND sent_at >= ?",
            (since_iso,),
        ).fetchone()
        return row["c"]


def count_leads_with_sent_step() -> int:
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT sq.lead_id) AS c
               FROM sequence_steps ss JOIN sequences sq ON ss.sequence_id = sq.id
               WHERE ss.status = 'sent'"""
        ).fetchone()
        return row["c"]


def count_events_distinct_leads(event_type: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT lead_id) AS c FROM events WHERE event_type = ?",
            (event_type,),
        ).fetchone()
        return row["c"]


def count_suppression() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM suppression").fetchone()
        return row["c"]


def get_daily_sent_counts(since_iso: str) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT substr(sent_at, 1, 10) AS day, COUNT(*) AS c
               FROM sequence_steps
               WHERE status = 'sent' AND sent_at >= ?
               GROUP BY day ORDER BY day""",
            (since_iso,),
        ).fetchall()


def get_daily_reply_counts(since_iso: str) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c
               FROM events
               WHERE event_type = 'replied' AND created_at >= ?
               GROUP BY day ORDER BY day""",
            (since_iso,),
        ).fetchall()


def get_all_sequences_overview(limit: int = 100) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT sq.id AS sequence_id, sq.status AS sequence_status, sq.angle_name,
                      sq.created_at AS created_at,
                      l.id AS lead_id, l.company_name AS company_name,
                      l.contact_email AS contact_email
               FROM sequences sq JOIN leads l ON sq.lead_id = l.id
               ORDER BY sq.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


# --- Phase 10: replies, outcomes, caps, angle performance ---


def save_reply(
    lead_id: int,
    sequence_id: int | None,
    from_addr: str,
    body_text: str,
    kind: str,
    subject: str | None = None,
    received_at: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO replies
               (lead_id, sequence_id, from_addr, subject, body_text, kind, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, sequence_id, from_addr, subject, body_text, kind, received_at or _now()),
        )
        return cur.lastrowid


def list_replies(lead_id: int | None = None, limit: int = 200) -> list[sqlite3.Row]:
    params: list = []
    where = ""
    if lead_id is not None:
        where = "WHERE r.lead_id = ?"
        params.append(lead_id)
    params.append(limit)
    with _connect() as conn:
        return conn.execute(
            f"""SELECT r.*, l.company_name, l.contact_email
                FROM replies r
                JOIN leads l ON r.lead_id = l.id
                {where}
                ORDER BY r.received_at DESC, r.id DESC
                LIMIT ?""",
            params,
        ).fetchall()


def count_steps_sent_between(start_iso: str, end_iso: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS c FROM sequence_steps
               WHERE status = 'sent' AND sent_at >= ? AND sent_at < ?""",
            (start_iso, end_iso),
        ).fetchone()
        return row["c"]


def get_angle_performance_rows() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT
                   sq.angle_name AS angle_name,
                   COUNT(DISTINCT CASE WHEN ss.status = 'sent' THEN sq.lead_id END) AS leads_contacted,
                   COUNT(DISTINCT CASE WHEN e.event_type = 'replied' THEN sq.lead_id END) AS replies
               FROM sequences sq
               LEFT JOIN sequence_steps ss ON ss.sequence_id = sq.id
               LEFT JOIN events e ON e.sequence_id = sq.id AND e.event_type = 'replied'
               GROUP BY sq.angle_name
               HAVING leads_contacted > 0 OR replies > 0
               ORDER BY replies DESC, leads_contacted DESC"""
        ).fetchall()
