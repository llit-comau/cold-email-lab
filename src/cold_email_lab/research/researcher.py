import re
from urllib.parse import quote_plus
from xml.etree.ElementTree import ParseError, fromstring

import httpx
from loguru import logger

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-AU&gl=AU&ceid=AU:en"
_GOOGLE_SEARCH = "https://www.google.com/search?q={query}&num=5&hl=en"


async def fetch_news_signals(company_name: str) -> list[str]:
    """Fetch recent news mentions via Google News RSS."""
    url = _NEWS_RSS.format(query=quote_plus(f'"{company_name}"'))
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=_HEADERS)
            resp.raise_for_status()

        root = fromstring(resp.text)
        signals: list[str] = []
        for item in root.findall("./channel/item")[:10]:
            title_el = item.find("title")
            pub_el = item.find("pubDate")
            if title_el is not None and title_el.text:
                signal = title_el.text.strip()
                if pub_el is not None and pub_el.text:
                    signal += f"  [{pub_el.text[:16].strip()}]"
                signals.append(signal)

        logger.info(f"News signals for {company_name!r}: {len(signals)} items")
        return signals

    except ParseError as exc:
        logger.warning(f"News RSS parse error for {company_name!r}: {exc}")
        return []
    except Exception as exc:
        logger.warning(f"News fetch failed for {company_name!r}: {exc}")
        return []


async def fetch_linkedin_signals(company_name: str) -> list[str]:
    """Approximate LinkedIn signals via a Google site: search."""
    query = quote_plus(f'site:linkedin.com/company "{company_name}"')
    url = _GOOGLE_SEARCH.format(query=query)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=_HEADERS)
            if resp.status_code != 200:
                logger.warning(f"LinkedIn Google search returned {resp.status_code}")
                return []

        signals = _extract_snippets(resp.text)
        logger.info(f"LinkedIn signals for {company_name!r}: {len(signals)} snippets")
        return signals

    except Exception as exc:
        logger.warning(f"LinkedIn signal fetch failed for {company_name!r}: {exc}")
        return []


def _extract_snippets(html: str, max_snippets: int = 5) -> list[str]:
    """Strip HTML tags and extract sentence-length text blocks."""
    html = re.sub(
        r"<(script|style|head)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    segments = re.split(r"(?<=[.!?])\s+", text)
    snippets: list[str] = []
    seen: set[str] = set()
    for seg in segments:
        seg = seg.strip()
        if not (40 <= len(seg) <= 400):
            continue
        key = seg[:60]
        if key in seen:
            continue
        seen.add(key)
        snippets.append(seg)
        if len(snippets) >= max_snippets:
            break
    return snippets
