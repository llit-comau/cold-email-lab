# Cold Email Lab — Claude Context

Internal tool for LeadFlow System (Melbourne). Feed it a target company URL, get a research brief plus three outreach angles with drafted emails and follow-ups.

## Phase status
- [x] Phase 1 — Scaffold + Playwright scraper + SQLite persistence
- [x] Phase 2 — News source + LinkedIn-via-Google + synthesis brief
- [x] Phase 3 — Angle generator + email writer
- [ ] Phase 4 — Cost tracking, retry hardening, compare command (parked)
- [x] Phase 5 — Leads & sequences (data + import + enrichment + enrolment)
- [x] Phase 6 — Approval, sending, reply detection
- [x] Phase 7 — Pipeline dashboard
- [x] Phase 8 — Lead sourcing (`leads source` + ICP profiles)
- [x] Phase 9 — Pluggable LLM provider (`llm/client.py`: anthropic default, nvidia via NVIDIA_API_KEY)
- [x] Phase 10 — Campaign engine extensions (reply storage, outcomes, caps, angle performance)

Always stop and wait for Mash's approval after completing a phase.

## Repo layout

```
src/cold_email_lab/
  cli.py            — typer entrypoint
  scrape/           — Playwright site scraper
  research/         — news + LinkedIn-via-Google signals (Phase 2)
  analyse/          — LLM synthesis → CompanyBrief (Phase 2)
  generate/         — angle + email generation (Phase 3)
  outbound/         — leads, sequences, sending, inbox, stats, sourcing (Phases 5-10)
  llm/              — pluggable LLM client (anthropic | nvidia) (Phase 9)
  storage/          — SQLite schema, read/write helpers
prompts/            — all LLM prompts as .md files (no inline prompts in Python)
profiles/           — ICP sourcing profiles (*.toml) for `leads source` (Phase 8)
data/               — SQLite db (gitignored)
logs/               — per-run log files (gitignored)
```

## Dependency management

`uv` — run `uv sync` to install, `uv run cold-email-lab` to execute.
After first install: `uv run playwright install chromium`

## Stack

| Concern | Library |
|---|---|
| CLI | typer |
| Scraping | playwright (async, chromium headless) |
| HTTP | httpx |
| LLM | pluggable via `llm/client.py` — anthropic SDK (claude-sonnet-4-6, default) or NVIDIA (httpx, OpenAI-compatible) |
| Validation | pydantic v2 |
| Logging | loguru |
| Env vars | python-dotenv |
| Storage | sqlite3 (stdlib) |

## SQLite schema (db: `data/cold-email-lab.db`)

```sql
runs            — id, url, status, started_at, completed_at, tokens, cost_usd
scraped_pages   — id, run_id, url, page_type, content_text, scraped_at, error
research_briefs — id, run_id, brief_json, created_at
outreach_sets   — id, run_id, angle_name, angle_json, created_at
```

Status flow: `running` → `scraped` → `briefed` → `completed` | `failed`

## Proposed CompanyBrief schema (to confirm before Phase 2)

```python
class CompanyBrief(BaseModel):
    company_name: str
    tagline: str | None
    description: str                  # 2-3 sentences: what they do
    target_customers: list[str]       # segments, e.g. "mid-market B2B SaaS"
    products_services: list[str]      # named products/services
    recent_news: list[str]            # last 6 months: launches, funding, awards
    hiring_signals: list[str]         # active roles / team growth areas
    tech_stack_hints: list[str]       # tools/platforms mentioned
    likely_pain_points: list[str]     # inferred, 3-5 items
    linkedin_signals: list[str]       # from Google site:linkedin.com/company search
    sources_scraped: list[str]        # URLs used as input
    scraped_at: datetime
    confidence_notes: str | None      # flag low-confidence inferences
```

## Proposed news source (to confirm before Phase 2)

**Default: Google News RSS** — free, no key, `httpx` fetch.
URL pattern: `https://news.google.com/rss/search?q="{company_name}"&hl=en-AU&gl=AU&ceid=AU:en`

Upgrade path if coverage is thin: **Brave Search API** (free tier, API key required).

## Scraper behaviour

- Scrapes homepage + up to 5 internal pages: about, services, pricing, blog, careers
- Headless Chromium via Playwright async API
- User-agent set to avoid trivial blocks
- Text extracted after stripping nav/header/footer/scripts
- 50 000 char cap per page
- Timeout: 30 s (configurable via `COLD_EMAIL_LAB_SCRAPE_TIMEOUT_MS`)

## LLM rules

- No longer Anthropic-only: every LLM call routes through `llm/client.py`'s `complete()`, which
  picks a backend per call based on `LLM_PROVIDER` (env is read at call time, not import time —
  a `.env` edit takes effect on the very next call in the same process).
  - `anthropic` (default) — anthropic SDK, tool-forced structured output, model `claude-sonnet-4-6`
    (fit scoring in `outbound/sourcing.py` keeps the cheaper `claude-haiku-4-5`). Unchanged
    behaviour from pre-Phase-9.
  - `nvidia` — plain httpx POST to `{NVIDIA_BASE_URL or https://integrate.api.nvidia.com/v1}/chat/completions`
    (OpenAI chat-completions format, bearer `NVIDIA_API_KEY`, model `NVIDIA_MODEL`). No SDK
    dependency added. Since there's no tool-forcing over plain httpx, the JSON schema is appended
    to the prompt as an explicit "respond with only this JSON shape" instruction instead.
- Retry (3 attempts, exponential backoff) lives inside `llm/client.py` itself now (moved there from
  each call site during Phase 9) — callers just call `complete()` and get a `LLMResult` or an
  exception after all retries are exhausted.
- Markdown-fence stripping (` ```json ... ``` `) happens inside the client before returning
  `LLMResult.text`, since NVIDIA-hosted reasoning models (e.g. GLM) sometimes wrap JSON answers in
  fences — harmless no-op for Claude, which normally doesn't.
- Missing credentials for the *selected* provider raise a clear upfront `ValueError` naming the
  exact env var(s) (`llm.client.require_configured()` — same style as the SMTP/IMAP guards), and
  do so *before* any other side effect (e.g. `enrich_lead` checks before creating a run row, so a
  bad LLM config never leaves a lead half-enriched).
- Optional LLM steps (Phase 8 fit scoring) use `llm.client.is_configured()` instead, which reports
  missing-config without raising, so scoring degrades to `NULL` gracefully regardless of provider.
- Every call logs: input tokens, output tokens, cost USD. NVIDIA cost is unknown/unpriced — the
  client logs `0`/`None` cost rather than fabricating a price; token counts are always logged.
- All prompts in `prompts/*.md`, loaded at call time (prompts still say "call `create_brief`" etc.
  for historical/Anthropic-tool-use reasons; the NVIDIA path's appended JSON-schema instruction
  takes precedence for that backend).

## Email voice rules

- Australian English (ise not ize, autumn not fall)
- No "I hope this finds you well", "circle back", "synergy", "I noticed your impressive…"
- First sentence references something specific to them
- Subject lines: lowercase, max 6 words, curiosity over claim
- CTA is a question, not "let's hop on a call"
- Max 120 words per email body

## Three outreach angles

Must differ in what they *lead with*, not just tone:
1. Pain-point angle
2. Opportunity/trend angle
3. Peer-credibility angle

Each angle: initial email + follow-up at day 3 + follow-up at day 7.

## Env vars

| Var | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `nvidia` — selects the backend in `llm/client.py`; read per-call, not cached |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=anthropic` (the default) |
| `NVIDIA_API_KEY` | — | Required when `LLM_PROVIDER=nvidia` — your build.nvidia.com API key |
| `NVIDIA_MODEL` | — | Required when `LLM_PROVIDER=nvidia` — exact model id, e.g. `z-ai/glm-5.2` (check https://build.nvidia.com) |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Optional override (e.g. to point at a local LiteLLM proxy); `/chat/completions` is appended |
| `COLD_EMAIL_LAB_DB_PATH` | `data/cold-email-lab.db` | |
| `COLD_EMAIL_LAB_MAX_PAGES` | `6` | homepage + up to 5 internal |
| `COLD_EMAIL_LAB_SCRAPE_TIMEOUT_MS` | `30000` | |
| `BRAVE_SEARCH_API_KEY` | — | Optional, Phase 2 upgrade |
| `SMTP_HOST` | — | Required for `send tick --live` |
| `SMTP_PORT` | `587` | STARTTLS |
| `SMTP_USER` | — | Required for `send tick --live` |
| `SMTP_PASS` | — | Required for `send tick --live` |
| `SMTP_FROM_NAME` | — | Used in the From header and as a signature fallback |
| `SMTP_FROM_EMAIL` | — | Required for `send tick --live` |
| `OUTBOUND_SIGNATURE` | falls back to `SMTP_FROM_NAME` | Appended to every sent email body |
| `UNSUBSCRIBE_TEXT` | "If you'd rather not hear from me, just reply 'unsubscribe' and I won't email again." | Appended to every sent email body (AU Spam Act compliance) |
| `IMAP_HOST` | — | Required for `inbox check` |
| `IMAP_USER` | — | Required for `inbox check` |
| `IMAP_PASS` | — | Required for `inbox check` |
| `SEND_DAILY_CAP` | `10` | Max live sends per UTC day; dry-run reports cap deferrals too |
| `SEND_WARMUP_SCHEDULE` | — | Optional comma-separated weekly caps, e.g. `5,10,20,40`; overrides `SEND_DAILY_CAP` after first live send date is anchored in `kv` |

## Phase 6 — sending & reply detection

- `review [--sequence N]` / `approve <step_id>` / `approve --sequence N` / `edit-step <step_id> --subject … --body …` — human approval gate before anything can send.
- `send tick` is dry-run by default (prints due steps with signature + unsubscribe footer, touches nothing). `send tick --live` sends via SMTP (STARTTLS) and refuses up front with a clear error if any `SMTP_*` env var is missing — nothing is sent or marked in that case. Per-message failures are logged and skipped; they never abort the batch.
- `inbox check` polls IMAP since a watermark stored in the `kv` table, matches replies to sequences via stored Message-ID (`In-Reply-To`/`References`) falling back to a from-address match against active sequences, and fails gracefully with a clear message if `IMAP_*` env vars are missing.
- Suggested crontab (not installed — set up manually if/when going live):
  ```
  */30 * * * * cd /home/mash/cold-email-lab && uv run cold-email-lab send tick --live && uv run cold-email-lab inbox check >> logs/cron.log 2>&1
  ```

## Phase 7 — pipeline dashboard

- `pipeline` — terminal funnel: leads by status, active sequences, steps due today, sent this week, reply rate (replied / leads with ≥1 sent step, zero-guarded), unsubscribe rate (unsubscribed / total leads, zero-guarded), suppressed count.
- `dashboard` — writes a fully self-contained `data/dashboard.html` (inline CSS only, no JS/CDN/external requests, light+dark via `prefers-color-scheme`): funnel bar, sequences table with per-step status badges, 30-day sent-vs-replies activity chart (CSS bars), suppression count. Prints the output path; opens nothing automatically.
- Business logic lives in `outbound/stats.py` (pure queries + derived rates, no printing/HTML); HTML template lives in `outbound/dashboard_html.py` (Python string template, `render_dashboard(stats, activity, sequences)`).
- New `events` table (`id`, `event_type` `'replied'|'unsubscribed'|'bounced'`, `lead_id`, `sequence_id` NULL, `created_at`) added to `_SCHEMA` — `inbox.py`'s `process_incoming_message` now logs an event on each outcome so the dashboard can chart replies by day (sent activity is read straight off `sequence_steps.sent_at`, which already carries a timestamp).

## Phase 8 — lead sourcing

- `leads source <profile> [--limit N] [--dry-run]` — discovers matching companies on the web, extracts contact emails from their sites, optionally scores fit, and inserts them as leads (`source = <profile>`, status `new`). `--dry-run` prints the candidate table and inserts nothing. Finishes with a report: discovered / filtered / no-email / inserted / skipped-duplicate.
- **Profiles** live in `profiles/*.toml` (read with stdlib `tomllib`); the profile id is the filename stem. Fields: `name`, `description`, `queries`, optional `exclude_domains`, optional `max_leads` (default 25, overridden by `--limit`). Two seed profiles ship: `job-tracker` (AU accounting firms) and `leadflow` (AU small service businesses). See `profiles/README.md`.
- **Pipeline** (in `outbound/sourcing.py`): DuckDuckGo HTML search via httpx → Bing via headless Chromium as fallback (DDG serves an "anomaly" block page from this machine, so Bing is the effective path); result URLs reduced to registrable domains; filtered against a built-in blocklist (directories, socials, gov, news, job boards, edu) + profile `exclude_domains` + domains already in `leads` + suppressed emails' domains; each surviving domain's homepage + contact + about pages scraped for emails (`mailto:` links preferred, regex over page text as fallback; person-looking addresses preferred over `info@`-style, but generic accepted); ~2s politeness delay between sites; every step has a timeout and per-item failure isolation.
- **Fit scoring** — only if `ANTHROPIC_API_KEY` is set: `claude-haiku-4-5` scores each candidate 0–100 against the profile `description` (prompt: `prompts/fit_score.md`, input = homepage text ≤1500 chars). Stored in the nullable `leads.fit_score` INTEGER column, added via a guarded `PRAGMA table_info` check + `ALTER TABLE ADD COLUMN` inside `init_db()` (additive only). No key → fit_score NULL + a one-line notice; everything else works.
- `leads list` gained `--source <profile>` and a FIT column; rows sort by fit_score desc with NULLs last.
- Re-running the same profile is idempotent: already-sourced domains are filtered out before scraping, and the `contact_email` UNIQUE constraint backstops inserts.

## Phase 10 — campaign engine extensions

- Reply storage: new `replies` table stores matched replies, unsubscribes, and bounces with `from_addr`, optional `subject`, full `body_text`, `kind`, and `received_at`. `inbox.py` stores a reply row from `process_incoming_message` whenever an inbound message matches a sequence; unmatched messages remain ignored as before. CLI: `replies <lead_id>` / `replies --all`.
- Lead outcomes: guarded `leads.outcome` and `leads.snooze_until` columns. CLI: `outcome <lead_id> <won|lost|followup>`, `snooze <lead_id> --until YYYY-MM-DD`, and `resurface <lead_id>` to clear both fields. Resurfacing is manual: `send tick` and `pipeline` report snoozed leads whose date has arrived, but they do not automatically clear the snooze.
- Daily cap + warm-up: `send tick` enforces `SEND_DAILY_CAP` (default 10) and optional `SEND_WARMUP_SCHEDULE` (`5,10,20,40` style weekly caps). Dry-run shows which due steps would be deferred; live mode leaves deferred steps approved and opens SMTP only for steps inside the cap. Warm-up uses `kv.send_first_live_date`, set when the first warm-up live send is attempted.
- Angle performance: `outbound/stats.py` exposes `get_angle_performance()` and `pipeline` shows per-angle contacted leads, replies, and reply rate.
