# Cold Email Lab

LeadFlow internal tool. Feed it a target company URL, get a researched brief and three tailored outreach sequences.

## Quick start

```bash
# Install dependencies (requires uv — https://docs.astral.sh/uv/getting-started/installation/)
uv sync

# Install Playwright browser (once)
uv run playwright install chromium

# Copy env file and add your Anthropic key
cp .env.example .env

# Run a research pass
uv run cold-email-lab research https://example.com
```

## Commands

| Command | Phase | Description |
|---|---|---|
| `research <url>` | 1 | Scrape the target site and persist to SQLite |
| `brief <run-id>` | 2 | Synthesise a research brief from a scraped run |
| `generate <run-id>` | 3 | Generate three outreach angles + emails |
| `compare <id> <id>` | 4 | View two runs side by side |

## Architecture

```
src/cold_email_lab/
  cli.py          typer entrypoint
  scrape/         Playwright site scraper
  research/       news + LinkedIn-via-Google (Phase 2)
  analyse/        LLM synthesis → CompanyBrief (Phase 2)
  generate/       angle + email generation (Phase 3)
  storage/        SQLite schema + helpers
prompts/          LLM prompts as .md files — edit these to tune behaviour
data/             SQLite database (gitignored)
logs/             Per-run logs (gitignored)
```

## Scraping strategy

The scraper loads the homepage via headless Chromium (handles JS-rendered content), then follows internal links to: about, services/products, pricing, blog, and careers pages. Up to 6 pages per run. LinkedIn is not scraped directly — Phase 2 uses a `site:linkedin.com/company` Google search workaround instead.

## News source decision

**Phase 2 will use Google News RSS** as the default — free, no API key, reasonable coverage.

URL pattern:
```
https://news.google.com/rss/search?q="Company Name"&hl=en-AU&gl=AU&ceid=AU:en
```

If coverage proves insufficient for niche industries, upgrade path is **Brave Search API** (requires free API key at search.brave.com). Document which you're using in `.env`.

## Proposed research brief schema (review before Phase 2)

```python
class CompanyBrief(BaseModel):
    company_name: str
    tagline: str | None
    description: str                  # 2-3 sentences
    target_customers: list[str]       # e.g. "mid-market B2B SaaS founders"
    products_services: list[str]
    recent_news: list[str]            # last 6 months
    hiring_signals: list[str]
    tech_stack_hints: list[str]
    likely_pain_points: list[str]     # inferred, 3-5 items
    linkedin_signals: list[str]
    sources_scraped: list[str]
    scraped_at: datetime
    confidence_notes: str | None
```

## Email voice rules

- Australian English: "ise" not "ize", "autumn" not "fall"
- No clichés: "I hope this finds you well", "circle back", "synergy", "I noticed your impressive…"
- First sentence references something specific to the recipient
- Subject lines: lowercase, max 6 words, curiosity over claim
- CTA is a question, not "let's hop on a call"
- Max 120 words per email body

## Development phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Scaffold, scraper, SQLite | Done |
| 2 | News + LinkedIn signals, research brief | Done |
| 3 | Angle generator, email writer | Done |
| 4 | Cost tracking, retry hardening, compare command | Pending |
