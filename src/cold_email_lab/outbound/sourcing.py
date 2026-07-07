"""Phase 8: lead sourcing — discover candidate companies, extract contacts, score fit, insert."""

import asyncio
import base64
import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from loguru import logger
from playwright.async_api import Browser, async_playwright

from ..llm.client import complete, is_configured
from ..storage.db import (
    create_lead,
    get_all_lead_urls,
    get_all_suppressed_emails,
    get_lead_by_email,
)

PROFILES_DIR = Path(__file__).parent.parent.parent.parent / "profiles"
_FIT_SCORE_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "fit_score.md"

# claude-haiku-4-5 for the anthropic backend (cheap); ignored by the nvidia
# backend, which always uses NVIDIA_MODEL regardless of purpose.
_FIT_SCORE_MODEL = "claude-haiku-4-5"

_DDG_URL = "https://html.duckduckgo.com/html/?q={query}"
_BING_URL = "https://www.bing.com/search?q={query}"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DISCOVERY_TIMEOUT_S = 15
SCRAPE_TIMEOUT_MS = 20_000
POLITENESS_DELAY_S = 2.0
CHAR_LIMIT_FOR_SCORING = 1500

# Built-in blocklist: directories, socials, gov, news, job boards, real estate.
# Profile-level exclude_domains are merged on top of this.
BUILTIN_BLOCKLIST = {
    "yellowpages.com.au", "yellowpages.com", "whitepages.com.au", "truelocal.com.au",
    "yelp.com", "yelp.com.au",
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    "wikipedia.org", "en.wikipedia.org",
    "seek.com.au", "indeed.com", "indeed.com.au", "jora.com", "adzuna.com.au",
    "realestate.com.au", "domain.com.au",
    "abc.net.au", "smh.com.au", "theage.com.au", "news.com.au", "9news.com.au",
    "7news.com.au", "afr.com", "theguardian.com", "dailymail.co.uk",
    "forbes.com", "investopedia.com", "canstar.com.au", "finder.com.au",
    "edu.au", "edu",
    "google.com", "bing.com", "duckduckgo.com",
    "gov.au", "business.gov.au", "ato.gov.au",
    "hotfrog.com.au", "startlocal.com.au", "localsearch.com.au", "aussieweb.com.au",
    "yp.com.au",
    "reddit.com", "quora.com",
    "wix.com", "squarespace.com", "wordpress.com",
    "glassdoor.com.au", "glassdoor.com",
}

_GENERIC_PREFIXES = {
    "info", "admin", "office", "enquiries", "enquiry", "enquire", "contact",
    "sales", "support", "hello", "team", "accounts", "reception", "mail",
    "bookings", "hi", "general",
}

_JUNK_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "sentry.io", "wixpress.com",
    "schema.org", "godaddy.com", "yourdomain.com", "domain.com", "email.com",
    "w3.org", "gmpg.org",
}
_JUNK_EMAIL_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js")

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9][a-zA-Z0-9._%+\-]*@[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?(?:\.[a-zA-Z0-9-]+)+"
)
_NAME_RE = re.compile(r"^[A-Z][a-zA-Z'\-]+(?: [A-Z][a-zA-Z'\-]+){1,2}$")

_CONTACT_HINTS = ["contact-us", "contact_us", "contactus", "get-in-touch", "contact"]
_ABOUT_HINTS = ["about-us", "about_us", "who-we-are", "our-story", "about"]

_EXTRACT_TEXT_JS = """
() => {
    const clone = document.body.cloneNode(true);
    const strip = ['nav', 'header', 'footer', 'script', 'style', 'noscript', 'svg', 'iframe'];
    strip.forEach(tag => clone.querySelectorAll(tag).forEach(el => el.remove()));
    return (clone.innerText || clone.textContent || '').trim();
}
"""

_MAILTO_JS = """
() => Array.from(document.querySelectorAll("a[href^='mailto:']")).map(a => ({
    href: a.getAttribute('href'), text: (a.innerText || a.textContent || '').trim()
}))
"""

_HREF_JS = "els => els.map(e => e.href)"


@dataclass
class Profile:
    name: str
    description: str
    queries: list[str]
    exclude_domains: list[str] = field(default_factory=list)
    max_leads: int = 25


@dataclass
class Candidate:
    domain: str
    url: str
    company_name: str
    contact_email: str
    contact_name: str | None
    evidence: str
    homepage_text: str = ""
    fit_score: int | None = None


@dataclass
class SourceReport:
    profile: str
    discovered: int = 0
    filtered: int = 0
    no_email_found: int = 0
    inserted: int = 0
    skipped_duplicate: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def load_profile(name: str) -> Profile:
    path = PROFILES_DIR / f"{name}.toml"
    if not path.exists():
        raise ValueError(
            f"Profile {name!r} not found (looked for {path}). "
            f"Available profiles: {', '.join(sorted(p.stem for p in PROFILES_DIR.glob('*.toml')))}"
        )
    with path.open("rb") as f:
        data = tomllib.load(f)

    queries = data.get("queries") or []
    if not queries:
        raise ValueError(f"Profile {name!r} has no queries defined")

    return Profile(
        name=data.get("name", name),
        description=data.get("description", ""),
        queries=queries,
        exclude_domains=list(data.get("exclude_domains") or []),
        max_leads=int(data.get("max_leads", 25)),
    )


def _registrable_domain(url: str) -> str | None:
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return None
    if not netloc:
        return None
    netloc = netloc.split("@")[-1].split(":")[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None


def _is_blocked(domain: str, blocklist: set[str]) -> bool:
    for blocked in blocklist:
        if domain == blocked or domain.endswith(f".{blocked}"):
            return True
    return False


async def _discover_ddg(query: str) -> list[str]:
    """DuckDuckGo HTML search results, via httpx. Returns [] on block/failure (never raises)."""
    url = _DDG_URL.format(query=quote_plus(query))
    try:
        async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(url, headers=_HEADERS)
            resp.raise_for_status()
        return _parse_ddg_html(resp.text)
    except Exception as exc:
        logger.warning(f"DDG discovery failed for {query!r}: {exc}")
        return []


def _parse_ddg_html(html: str) -> list[str]:
    urls: list[str] = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"', html):
        href = unquote(m.group(1))
        if href.startswith("//duckduckgo.com/l/?"):
            qs = parse_qs(urlparse("https:" + href).query)
            target = qs.get("uddg", [None])[0]
            if target:
                urls.append(target)
        elif href.startswith("http"):
            urls.append(href)
    return urls


def _decode_bing_href(href: str) -> str | None:
    """Bing wraps result links as /ck/a?...&u=a1<base64url>&... ; decode the target URL."""
    try:
        qs = parse_qs(urlparse(href).query)
        u = qs.get("u", [None])[0]
        if not u:
            return href if href.startswith("http") and "bing.com" not in href else None
        if u.startswith("a1"):
            u = u[2:]
        padded = u + "=" * (-len(u) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:
        return None


async def _discover_bing(browser: Browser, query: str) -> list[str]:
    """Bing search via headless Chromium (DDG fallback). Returns [] on failure (never raises)."""
    ctx = await browser.new_context(user_agent=_USER_AGENT)
    page = await ctx.new_page()
    try:
        url = _BING_URL.format(query=quote_plus(query))
        await page.goto(url, timeout=SCRAPE_TIMEOUT_MS, wait_until="domcontentloaded")
        hrefs: list[str] = await page.eval_on_selector_all("li.b_algo a, h2 a", _HREF_JS)
        urls = []
        for href in hrefs:
            decoded = _decode_bing_href(href)
            if decoded:
                urls.append(decoded)
        return urls
    except Exception as exc:
        logger.warning(f"Bing discovery failed for {query!r}: {exc}")
        return []
    finally:
        await ctx.close()


async def discover_domains(profile: Profile) -> tuple[list[str], int, int]:
    """Run all profile queries, return (ordered deduped candidate domains, discovered_count, filtered_count)."""
    existing_domains = {d for d in (_registrable_domain(u) for u in get_all_lead_urls()) if d}
    suppressed_domains = {
        d for d in (e.split("@")[-1].lower() for e in get_all_suppressed_emails() if "@" in e) if d
    }
    blocklist = BUILTIN_BLOCKLIST | {d.lower() for d in profile.exclude_domains}

    seen: set[str] = set()
    candidates: list[str] = []
    discovered = 0
    filtered = 0
    browser: Browser | None = None

    try:
        async with async_playwright() as pw:
            for query in profile.queries:
                urls = await _discover_ddg(query)
                if not urls:
                    logger.info(f"DDG returned nothing for {query!r} — falling back to Bing")
                    if browser is None:
                        browser = await pw.chromium.launch(headless=True)
                    urls = await _discover_bing(browser, query)

                for url in urls:
                    domain = _registrable_domain(url)
                    if not domain:
                        continue
                    discovered += 1
                    if domain in seen:
                        continue
                    seen.add(domain)
                    if (
                        _is_blocked(domain, blocklist)
                        or domain in existing_domains
                        or domain in suppressed_domains
                    ):
                        filtered += 1
                        continue
                    candidates.append(domain)
    finally:
        if browser is not None:
            await browser.close()

    return candidates, discovered, filtered


def _clean_emails(text: str) -> list[str]:
    found = []
    seen = set()
    for m in _EMAIL_RE.finditer(text):
        email = m.group(0).rstrip(".")
        low = email.lower()
        if low in seen:
            continue
        domain_part = low.split("@")[-1]
        if domain_part in _JUNK_EMAIL_DOMAINS:
            continue
        if low.endswith(_JUNK_EMAIL_EXT):
            continue
        if any(domain_part.endswith(ext) for ext in _JUNK_EMAIL_EXT):
            continue
        seen.add(low)
        found.append(email)
    return found


def _choose_best_email(emails: list[str], domain: str) -> str | None:
    if not emails:
        return None

    def is_domain_match(e: str) -> bool:
        e_domain = e.split("@")[-1].lower()
        return e_domain == domain or e_domain.endswith(f".{domain}")

    def is_generic(e: str) -> bool:
        prefix = e.split("@")[0].lower()
        return prefix in _GENERIC_PREFIXES

    ranked = sorted(
        emails,
        key=lambda e: (not is_domain_match(e), is_generic(e)),
    )
    return ranked[0]


def _guess_contact_name(chosen_email: str, mailto_pairs: list[dict]) -> str | None:
    for pair in mailto_pairs:
        href = (pair.get("href") or "").replace("mailto:", "").split("?")[0].strip().lower()
        if href == chosen_email.lower():
            text = (pair.get("text") or "").strip()
            if _NAME_RE.match(text):
                return text

    local = chosen_email.split("@")[0]
    if "." in local:
        parts = [p for p in local.split(".") if p.isalpha() and len(p) >= 2]
        if len(parts) == 2:
            return " ".join(p.capitalize() for p in parts)
    return None


def _guess_company_name(domain: str, title: str) -> str:
    title = (title or "").strip()
    if title:
        # Strip common site-title suffixes like " | Home", " - Accounting Firm"
        cleaned = re.split(r"\s*[|–—-]\s*", title)[0].strip()
        if 3 <= len(cleaned) <= 60:
            return cleaned
    stem = domain.split(".")[0]
    return stem.replace("-", " ").title()


async def _load_page(browser: Browser, url: str) -> tuple[str, str, list[dict]] | None:
    """Load a page, return (title, text, mailto_pairs) or None on failure."""
    ctx = await browser.new_context(user_agent=_USER_AGENT)
    page = await ctx.new_page()
    try:
        await page.goto(url, timeout=SCRAPE_TIMEOUT_MS, wait_until="domcontentloaded")
        title = await page.title()
        text: str = await page.evaluate(_EXTRACT_TEXT_JS)
        mailto_pairs: list[dict] = await page.evaluate(_MAILTO_JS)
        return title, text, mailto_pairs
    except Exception as exc:
        logger.debug(f"Page load failed {url}: {exc}")
        return None
    finally:
        await ctx.close()


async def _find_hint_page(browser: Browser, base_url: str, hints: list[str], homepage_hrefs: list[str]) -> str | None:
    base_domain = urlparse(base_url).netloc
    for href in homepage_hrefs:
        parsed = urlparse(href)
        if parsed.netloc != base_domain:
            continue
        path_lower = parsed.path.rstrip("/").lower()
        for hint in hints:
            if f"/{hint}" in path_lower or path_lower.endswith(hint):
                return parsed._replace(fragment="").geturl()
    return None


async def extract_contact(browser: Browser, domain: str) -> Candidate | None:
    """Scrape homepage + contact + about pages for a domain. Returns None if no usable email found."""
    for scheme in ("https://", "http://"):
        base_url = f"{scheme}{domain}"
        homepage = await _load_page(browser, base_url)
        if homepage is not None:
            break
    else:
        logger.info(f"Could not load homepage for {domain} — skipping")
        return None

    title, homepage_text, homepage_mailto = homepage

    homepage_hrefs: list[str] = []
    ctx = await browser.new_context(user_agent=_USER_AGENT)
    page = await ctx.new_page()
    try:
        await page.goto(base_url, timeout=SCRAPE_TIMEOUT_MS, wait_until="domcontentloaded")
        homepage_hrefs = await page.eval_on_selector_all("a[href]", _HREF_JS)
    except Exception as exc:
        logger.debug(f"Homepage link discovery failed for {domain}: {exc}")
    finally:
        await ctx.close()

    pages_text: list[tuple[str, str]] = [("homepage", homepage_text)]
    all_mailto: list[dict] = list(homepage_mailto)

    contact_url = await _find_hint_page(browser, base_url, _CONTACT_HINTS, homepage_hrefs)
    about_url = await _find_hint_page(browser, base_url, _ABOUT_HINTS, homepage_hrefs)

    for page_type, url in (("contact", contact_url), ("about", about_url)):
        if not url:
            continue
        result = await _load_page(browser, url)
        if result is None:
            continue
        _, text, mailto_pairs = result
        pages_text.append((page_type, text))
        all_mailto.extend(mailto_pairs)

    # Emails from mailto links first (higher confidence), then regex over page text.
    mailto_emails = [
        p["href"].replace("mailto:", "").split("?")[0].strip()
        for p in all_mailto
        if p.get("href", "").startswith("mailto:")
    ]
    all_emails = _clean_emails(" ".join(mailto_emails)) or []
    evidence_page = None
    if all_emails:
        evidence_page = "mailto link"

    if not all_emails:
        for page_type, text in pages_text:
            found = _clean_emails(text)
            if found:
                all_emails = found
                evidence_page = f"{page_type} page text"
                break

    chosen = _choose_best_email(all_emails, domain)
    if not chosen:
        return None

    contact_name = _guess_contact_name(chosen, all_mailto)
    company_name = _guess_company_name(domain, title)

    evidence = f"Email found via {evidence_page or 'page text'} on {domain}"

    return Candidate(
        domain=domain,
        url=base_url,
        company_name=company_name,
        contact_email=chosen,
        contact_name=contact_name,
        evidence=evidence,
        homepage_text=homepage_text[:CHAR_LIMIT_FOR_SCORING],
    )


async def score_fit(candidate: Candidate, description: str) -> tuple[int | None, str | None]:
    """Score a candidate's fit 0-100 via claude-haiku-4-5. Returns (score, rationale) or (None, None)
    on any failure — a scoring failure never crashes the run, it just leaves fit_score NULL."""
    try:
        prompt = (
            _FIT_SCORE_PROMPT_PATH.read_text()
            .replace("{description}", description)
            .replace("{homepage_text}", candidate.homepage_text or "(no homepage text captured)")
        )
        tool_schema = {
            "name": "score_fit",
            "description": "Record the fit score for this candidate lead.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "rationale": {"type": "string"},
                },
                "required": ["score", "rationale"],
            },
        }
        result = await complete(
            prompt, max_tokens=300, purpose="fit_score", tool_schema=tool_schema, model=_FIT_SCORE_MODEL
        )
        data = json.loads(result.text)
        return int(data["score"]), str(data.get("rationale", ""))
    except Exception as exc:
        logger.warning(f"Fit scoring failed for {candidate.domain}: {exc}")
        return None, None


async def source_leads(profile_name: str, limit: int | None = None, dry_run: bool = False) -> SourceReport:
    """Run the full Phase 8 pipeline for a profile: discover → filter → extract → score → insert."""
    profile = load_profile(profile_name)
    effective_limit = limit if limit is not None else profile.max_leads

    report = SourceReport(profile=profile.name)

    candidates_domains, discovered, filtered = await discover_domains(profile)
    report.discovered = discovered
    report.filtered = filtered

    if not candidates_domains:
        report.notices.append("No candidate domains survived discovery/filtering.")
        return report

    have_llm, llm_hint = is_configured()
    if not have_llm:
        report.notices.append(f"LLM not configured ({llm_hint}) — fit scoring skipped, fit_score will be NULL.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for i, domain in enumerate(candidates_domains):
                if len(report.candidates) >= effective_limit:
                    break
                try:
                    candidate = await extract_contact(browser, domain)
                except Exception as exc:
                    logger.warning(f"Contact extraction failed for {domain}: {exc}")
                    candidate = None

                if candidate is None:
                    report.no_email_found += 1
                else:
                    if have_api_key:
                        score, rationale = await score_fit(candidate, profile.description)
                        candidate.fit_score = score
                        if rationale:
                            candidate.evidence += f"; fit {score}: {rationale}"
                    report.candidates.append(candidate)

                if i < len(candidates_domains) - 1:
                    await asyncio.sleep(POLITENESS_DELAY_S)
        finally:
            await browser.close()

    if dry_run:
        return report

    for candidate in report.candidates:
        if get_lead_by_email(candidate.contact_email) is not None:
            report.skipped_duplicate += 1
            continue
        try:
            create_lead(
                company_name=candidate.company_name,
                url=candidate.url,
                contact_email=candidate.contact_email,
                contact_name=candidate.contact_name,
                source=profile.name,
                notes=candidate.evidence,
                fit_score=candidate.fit_score,
            )
            report.inserted += 1
        except sqlite3.IntegrityError:
            report.skipped_duplicate += 1

    return report
