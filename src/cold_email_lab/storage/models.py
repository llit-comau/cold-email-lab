from datetime import datetime
from pydantic import BaseModel


class Run(BaseModel):
    id: int | None = None
    url: str
    status: str = "running"
    started_at: datetime
    completed_at: datetime | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0


class ScrapedPage(BaseModel):
    id: int | None = None
    run_id: int
    url: str
    page_type: str | None = None
    content_text: str | None = None
    scraped_at: datetime
    error: str | None = None


class ResearchBrief(BaseModel):
    id: int | None = None
    run_id: int
    brief_json: str
    created_at: datetime


class OutreachSet(BaseModel):
    id: int | None = None
    run_id: int
    angle_name: str
    angle_json: str
    created_at: datetime


# Phase 2: confirm this schema with Mash before wiring up
class CompanyBrief(BaseModel):
    company_name: str
    tagline: str | None = None
    description: str
    target_customers: list[str]
    products_services: list[str]
    recent_news: list[str]
    hiring_signals: list[str]
    tech_stack_hints: list[str]
    likely_pain_points: list[str]
    linkedin_signals: list[str]
    sources_scraped: list[str]
    scraped_at: datetime
    confidence_notes: str | None = None


# Phase 3
class Email(BaseModel):
    subject: str
    body: str


class OutreachAngle(BaseModel):
    angle_name: str
    angle_rationale: str
    initial_email: Email
    followup_day3: Email
    followup_day7: Email


# Phase 5
class Lead(BaseModel):
    id: int | None = None
    company_name: str
    url: str
    contact_name: str | None = None
    contact_email: str
    source: str | None = None
    status: str = "new"
    run_id: int | None = None
    created_at: datetime
    notes: str | None = None
    fit_score: int | None = None  # Phase 8: lead sourcing fit score, 0-100
    outcome: str | None = None  # Phase 10: won/lost/followup/snoozed
    snooze_until: str | None = None


class Suppression(BaseModel):
    id: int | None = None
    email: str
    reason: str
    created_at: datetime


class Sequence(BaseModel):
    id: int | None = None
    lead_id: int
    run_id: int
    angle_name: str
    status: str = "active"
    created_at: datetime


class SequenceStep(BaseModel):
    id: int | None = None
    sequence_id: int
    step_number: int
    due_at: datetime
    subject: str
    body: str
    status: str = "draft"
    sent_at: datetime | None = None
    smtp_message_id: str | None = None


# Phase 6
class Kv(BaseModel):
    key: str
    value: str | None = None


# Phase 7
class Event(BaseModel):
    id: int | None = None
    event_type: str
    lead_id: int
    sequence_id: int | None = None
    created_at: datetime


# Phase 10
class Reply(BaseModel):
    id: int | None = None
    lead_id: int
    sequence_id: int | None = None
    from_addr: str
    subject: str | None = None
    body_text: str
    kind: str
    received_at: datetime
