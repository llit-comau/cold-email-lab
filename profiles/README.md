# Lead sourcing profiles

Each `.toml` file in this directory is an ICP (ideal customer profile) used by
`leads source <profile>`. The profile id is the filename stem (e.g.
`job-tracker.toml` → `leads source job-tracker`).

Fields:

| Field | Required | Description |
|---|---|---|
| `name` | yes | Profile id. Should match the filename stem (informational — the filename is what's actually used to look it up). |
| `description` | yes | 1–2 sentences describing the target company. Used as the fit-scoring prompt input when `ANTHROPIC_API_KEY` is set. |
| `queries` | yes | List of search-engine query strings. Each is run against DuckDuckGo (falling back to Bing) to discover candidate company websites. Vary city/service to widen coverage. |
| `exclude_domains` | no | Extra domains to blocklist for this profile (merged with the built-in blocklist of directories, socials, gov/news sites, etc). |
| `max_leads` | no (default 25) | Cap on how many leads a single `leads source` run will insert, if `--limit` isn't passed on the command line. |

## Editing / adding a profile

Copy `job-tracker.toml` or `leadflow.toml`, change `name`, `description`,
`queries`, and re-run:

```
uv run cold-email-lab leads source <your-profile-name> --limit 5 --dry-run
```

to sanity-check candidates before a real (inserting) run.

## What happens when you run it

1. **Discover** — each query is run against DuckDuckGo HTML search; if that's
   blocked or empty, Bing (via headless Chromium) is tried instead.
2. **Filter** — known directories/socials/news/gov sites, this profile's
   `exclude_domains`, domains already in your `leads` table, and domains
   belonging to suppressed email addresses are all dropped.
3. **Extract** — homepage + contact + about pages of each remaining candidate
   are scraped for an email address (and, where trivially available, a
   contact name).
4. **Score (optional)** — if `ANTHROPIC_API_KEY` is set, each candidate is
   scored 0–100 for fit against `description` using `claude-haiku-4-5`. No
   key → `fit_score` stays `NULL` and everything else still works.
5. **Insert** — new leads are created with `source = <profile name>` and
   `status = 'new'`. Re-running the same profile is safe — duplicate emails
   are skipped.
