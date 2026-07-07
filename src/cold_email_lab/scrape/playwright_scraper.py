import os
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from loguru import logger
from playwright.async_api import Browser, async_playwright

MAX_PAGES = int(os.getenv("COLD_EMAIL_LAB_MAX_PAGES", "6"))
TIMEOUT_MS = int(os.getenv("COLD_EMAIL_LAB_SCRAPE_TIMEOUT_MS", "30000"))
CHAR_LIMIT = 50_000

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Paths to look for within the target domain, keyed by page_type
_PAGE_HINTS: dict[str, list[str]] = {
    "about": ["about", "about-us", "who-we-are", "company", "our-story", "team"],
    "services": ["services", "solutions", "products", "what-we-do", "offerings", "platform"],
    "pricing": ["pricing", "plans", "packages", "rates", "cost"],
    "blog": ["blog", "news", "insights", "resources", "articles", "posts", "media"],
    "careers": ["careers", "jobs", "work-with-us", "join-us", "hiring", "join-the-team"],
}

# JS to strip non-content elements and return clean text
_EXTRACT_TEXT_JS = """
() => {
    const clone = document.body.cloneNode(true);
    const strip = ['nav', 'header', 'footer', 'script', 'style', 'noscript', 'svg', 'iframe'];
    strip.forEach(tag => clone.querySelectorAll(tag).forEach(el => el.remove()));
    return (clone.innerText || clone.textContent || '').trim();
}
"""


@dataclass
class ScrapedPage:
    url: str
    page_type: str
    title: str
    text_content: str
    error: str | None = None
    char_count: int = field(init=False)

    def __post_init__(self):
        self.char_count = len(self.text_content)


async def scrape_company(url: str) -> list[ScrapedPage]:
    """Scrape a company website: homepage + up to MAX_PAGES-1 key internal pages."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            return await _scrape_all(browser, url)
        finally:
            await browser.close()


async def _scrape_all(browser: Browser, base_url: str) -> list[ScrapedPage]:
    results: list[ScrapedPage] = []

    homepage = await _scrape_page(browser, base_url, "homepage")
    results.append(homepage)

    if homepage.error:
        logger.warning(f"Homepage failed — skipping internal page discovery: {homepage.error}")
        return results

    internal = await _discover_internal_links(browser, base_url)
    scraped_urls = {base_url}

    for page_type, page_url in internal.items():
        if len(results) >= MAX_PAGES:
            break
        if page_url in scraped_urls:
            continue
        scraped_urls.add(page_url)
        page = await _scrape_page(browser, page_url, page_type)
        results.append(page)

    return results


async def _discover_internal_links(browser: Browser, base_url: str) -> dict[str, str]:
    """Walk homepage links to find typed internal pages."""
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc

    ctx = await browser.new_context(user_agent=_USER_AGENT)
    page = await ctx.new_page()
    found: dict[str, str] = {}

    try:
        await page.goto(base_url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        hrefs: list[str] = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")

        for href in hrefs:
            parsed = urlparse(href)
            if parsed.netloc != base_domain:
                continue
            path_lower = parsed.path.rstrip("/").lower()

            for page_type, hints in _PAGE_HINTS.items():
                if page_type in found:
                    continue
                for hint in hints:
                    if f"/{hint}" in path_lower or path_lower.endswith(hint):
                        # Reconstruct clean URL without fragment
                        clean = parsed._replace(fragment="").geturl()
                        found[page_type] = clean
                        logger.debug(f"Found {page_type} page: {clean}")
                        break

        logger.info(f"Discovered {len(found)} internal page(s): {list(found.keys())}")
    except Exception as exc:
        logger.warning(f"Internal link discovery failed: {exc}")
    finally:
        await ctx.close()

    return found


async def _scrape_page(browser: Browser, url: str, page_type: str) -> ScrapedPage:
    ctx = await browser.new_context(user_agent=_USER_AGENT)
    page = await ctx.new_page()

    try:
        logger.info(f"Scraping [{page_type}] {url}")
        await page.goto(url, timeout=TIMEOUT_MS, wait_until="networkidle")

        title = await page.title()
        raw_text: str = await page.evaluate(_EXTRACT_TEXT_JS)

        # Collapse blank lines and cap length
        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        text = "\n".join(lines)[:CHAR_LIMIT]

        logger.debug(f"  → {len(text):,} chars, title: {title!r}")
        return ScrapedPage(url=url, page_type=page_type, title=title, text_content=text)

    except Exception as exc:
        logger.error(f"Scrape failed [{page_type}] {url}: {exc}")
        return ScrapedPage(url=url, page_type=page_type, title="", text_content="", error=str(exc))
    finally:
        await ctx.close()
