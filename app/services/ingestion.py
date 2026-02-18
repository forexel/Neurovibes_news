from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
import trafilatura
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import session_scope
from app.core.config import settings
from app.models import Article, ArticleStatus, RawFeedEntry, RawPageSnapshot, Source, SourceHealthMetric
from app.services.runtime_settings import get_runtime_bool, get_runtime_csv_list
from app.services.topic_filter import passes_ai_topic_filter
from app.services.utils import normalize_url, stable_hash, strip_html

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency/runtime
    sync_playwright = None
    PlaywrightTimeoutError = Exception

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

RSS_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}

# Important: do NOT use RSS Accept headers for HTML pages. Some sites respond with 403 to "botty" headers.
HTML_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


def _looks_like_feed_url(url: str) -> bool:
    low = (url or "").lower()
    return any(
        tok in low
        for tok in [
            "/feed",
            "/rss",
            ".rss",
            ".xml",
            "feed=",
            "format=rss",
            "format=xml",
        ]
    )


def _looks_paywalled_or_thin(html: str | None) -> bool:
    if not html:
        return True
    low = html.lower()
    markers = [
        "subscribe to continue",
        "subscribe now",
        "sign in to continue",
        "please subscribe",
        "already a subscriber",
        "premium content",
        "member-only",
        "metered paywall",
        "create an account to read",
        "you've reached your limit",
        "read more by subscribing",
    ]
    if any(m in low for m in markers):
        return True
    return len(html) < 1500


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if not dt.tzinfo:
            return dt
        return dt.astimezone().replace(tzinfo=None)
    except Exception:
        return None


def _extract_html_text(entry: dict) -> str:
    if entry.get("content"):
        joined = "\n\n".join(item.get("value", "") for item in entry.get("content", []))
        if joined.strip():
            return strip_html(joined)
    return strip_html(entry.get("summary", ""))


def _entry_external_id(entry: dict) -> str:
    return str(entry.get("id") or entry.get("guid") or entry.get("link") or stable_hash(str(entry)))[:500]


def _entry_hash(entry: dict) -> str:
    seed = "|".join(
        [
            str(entry.get("title") or ""),
            str(entry.get("summary") or ""),
            str(entry.get("link") or ""),
        ]
    )
    return stable_hash(seed)


def _extract_canonical(html: str, final_url: str) -> str:
    if not html:
        return normalize_url(final_url)
    try:
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("link", rel="canonical")
        if link and link.get("href"):
            href = str(link.get("href")).strip()
            abs_url = urljoin(final_url, href)
            return normalize_url(abs_url)
    except Exception:
        pass
    return normalize_url(final_url)


def _fetch_page(url: str) -> tuple[str | None, str, int | None, float]:
    """
    Basic fetcher used across ingestion and admin "Read From Site".

    Returns: (html, final_url, status_code, latency_ms)
    """
    start = time.perf_counter()
    status_code: int | None = None
    final_url = url
    html: str | None = None

    try:
        headers = RSS_HEADERS if _looks_like_feed_url(url) else HTML_HEADERS
        with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as client:
            resp = client.get(url)
            status_code = resp.status_code
            final_url = str(resp.url)
            if 200 <= resp.status_code < 400:
                html = resp.text
    except Exception:
        pass

    if _should_use_browser_fetch(url=url, status_code=status_code, html=html):
        browser_html, browser_final_url, browser_status, browser_latency = _fetch_page_browser(url)
        if browser_html:
            html = browser_html
            final_url = browser_final_url
            status_code = browser_status
            latency_ms = (time.perf_counter() - start) * 1000.0 + browser_latency
            return html, final_url, status_code, latency_ms

    latency_ms = (time.perf_counter() - start) * 1000.0
    return html, final_url, status_code, latency_ms


def scrape_url(url: str) -> dict:
    """
    Scrape an article page and extract main text.

    Best-effort only; we don't attempt to bypass paywalls/captchas.
    Returns diagnostics that help explain why extraction failed.
    """
    html, final_url, status_code, latency_ms = _fetch_page(url)
    full_text, quality = _extract_full_text(html)
    extracted = (full_text or "").strip()
    html_len = 0 if not html else len(html)
    return {
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "latency_ms": float(latency_ms or 0.0),
        "html_len": int(html_len),
        "paywalled_or_thin": bool(_looks_paywalled_or_thin(html)),
        "parse_quality": float(quality or 0.0),
        "text": extracted,
        "text_len": len(extracted),
        # For internal use (snapshots/debug); do not expose outside trusted boundaries.
        "html": html or "",
    }


def _should_use_browser_fetch(url: str, status_code: int | None, html: str | None) -> bool:
    if not get_runtime_bool("browser_fetch_enabled", default=True):
        return False
    host = (urlparse(url).netloc or "").lower()
    domains = [x.strip().lower() for x in get_runtime_csv_list("browser_fetch_domains_csv")]
    if not any(d in host for d in domains):
        return False
    if status_code in {401, 403, 406, 429}:
        return True
    if _looks_paywalled_or_thin(html):
        return True
    return False


def _fetch_page_browser(url: str) -> tuple[str | None, str, int | None, float]:
    if sync_playwright is None:
        return None, url, None, 0.0

    start = time.perf_counter()
    final_url = url
    status_code: int | None = None
    html: str | None = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=UA,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            if settings.browser_cookies_json.strip():
                try:
                    cookies = json.loads(settings.browser_cookies_json)
                    if isinstance(cookies, list):
                        prepared = []
                        host = (urlparse(url).netloc or "").lower()
                        for c in cookies:
                            if not isinstance(c, dict):
                                continue
                            c_name = str(c.get("name") or "").strip()
                            c_val = str(c.get("value") or "").strip()
                            if not c_name:
                                continue
                            domain = str(c.get("domain") or "").strip()
                            path = str(c.get("path") or "/").strip() or "/"
                            if not domain:
                                domain = "." + host if host else ""
                            prepared.append(
                                {
                                    "name": c_name,
                                    "value": c_val,
                                    "domain": domain,
                                    "path": path,
                                    "httpOnly": bool(c.get("httpOnly", False)),
                                    "secure": bool(c.get("secure", True)),
                                    "sameSite": c.get("sameSite", "Lax"),
                                }
                            )
                        if prepared:
                            context.add_cookies(prepared)
                except Exception:
                    pass
            # basic stealth hardening
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.new_page()
            # Use sane headers: RSS endpoints can accept XML, but HTML pages should look like a browser.
            is_feed = _looks_like_feed_url(url)
            page.set_extra_http_headers(
                {
                    "Accept-Language": "en-US,en;q=0.9",
                    "Upgrade-Insecure-Requests": "1",
                    "Accept": (RSS_HEADERS["Accept"] if is_feed else HTML_HEADERS["Accept"]),
                }
            )
            resp = page.goto(url, wait_until="domcontentloaded", timeout=35000)
            try:
                page.wait_for_load_state("networkidle", timeout=7000)
            except PlaywrightTimeoutError:
                pass
            final_url = page.url or url
            status_code = resp.status if resp else 200
            try:
                # For RSS/XML endpoints, prefer raw response body over browser-rendered markup.
                html = resp.text() if resp else page.content()
            except Exception:
                html = page.content()
            context.close()
            browser.close()
    except Exception:
        return None, final_url, status_code, (time.perf_counter() - start) * 1000.0

    return html, final_url, status_code, (time.perf_counter() - start) * 1000.0


def _extract_full_text(html: str | None) -> tuple[str, float]:
    if not html:
        return "", 0.0
    extracted = trafilatura.extract(html, include_comments=False, include_tables=False, no_fallback=False)
    if extracted:
        txt = strip_html(extracted)
        quality = min(1.0, len(txt) / 3000.0)
        if len(txt) >= 700:
            return txt, quality

    # Fallback 1: structured data (JSON-LD articleBody)
    soup = BeautifulSoup(html, "html.parser")
    ld_text = _extract_from_jsonld(soup)
    if len(ld_text) >= 700:
        return ld_text, min(1.0, len(ld_text) / 3000.0)

    # Fallback 2: visible article paragraphs in DOM
    dom_text = _extract_from_dom_paragraphs(soup)
    if len(dom_text) >= 700:
        return dom_text, min(1.0, len(dom_text) / 3000.0)

    # If only short fragments are available, return best available but low quality.
    best = max([strip_html(extracted or ""), ld_text, dom_text], key=len)
    if not best:
        return "", 0.0
    return best, min(0.2, len(best) / 3000.0)


def _extract_from_jsonld(soup: BeautifulSoup) -> str:
    chunks: list[str] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = (tag.string or tag.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for body in _walk_json_for_article_body(data):
            txt = strip_html(str(body))
            if len(txt) >= 120:
                chunks.append(txt)
    deduped = _dedupe_lines(chunks)
    return "\n\n".join(deduped)[:50000]


def _walk_json_for_article_body(node):
    if isinstance(node, dict):
        for k, v in node.items():
            lk = str(k).lower()
            if lk in {"articlebody", "text", "description"} and isinstance(v, str) and len(v) >= 120:
                yield v
            else:
                yield from _walk_json_for_article_body(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json_for_article_body(item)


def _extract_from_dom_paragraphs(soup: BeautifulSoup) -> str:
    candidates: list[str] = []

    # Try likely article containers first.
    selectors = [
        "article",
        "[itemprop='articleBody']",
        "main article",
        "[data-qa*='article']",
        "[data-testid*='article']",
        "[class*='article-body']",
        "[class*='articleBody']",
        "[class*='story-body']",
        "[class*='entry-content']",
        "[class*='post-content']",
        "[class*='content-body']",
        "main",
        "[role='main']",
    ]
    scopes = []
    for sel in selectors:
        try:
            node = soup.select_one(sel)
            if node is not None:
                scopes.append(node)
        except Exception:
            continue
    if not scopes:
        scopes = [soup]

    for scope in scopes:
        for p in scope.find_all(["p", "h2", "h3", "li"]):
            txt = strip_html(p.get_text(" ", strip=True))
            if len(txt) < 60:
                continue
            low = txt.lower()
            if (
                "subscribe" in low
                or "sign in" in low
                or "newsletter" in low
                or "advertisement" in low
                or "cookie" in low
            ):
                continue
            candidates.append(txt)

    deduped = _dedupe_lines(candidates)
    return "\n\n".join(deduped)[:50000]


def _should_upgrade_text(prev_text: str, full_text: str, quality: float) -> bool:
    prev_len = len(prev_text or "")
    full_len = len(full_text or "")
    if full_len < 700:
        return False
    if quality >= 0.20 and full_len >= max(800, int(prev_len * 1.15)):
        return True
    if quality >= 0.12 and full_len >= prev_len + 180:
        return True
    if prev_len < 500 and full_len >= 700:
        return True
    return False


def _should_set_full_when_prev_summary_only(prev_mode: str | None, full_text: str, quality: float, url: str | None = None) -> bool:
    """
    When an article is currently summary_only, accept extracted page text even if the RSS
    summary is long (so length-based comparison may fail). Never downgrade full -> summary.
    """
    if (prev_mode or "summary_only") == "full":
        return False
    full_len = len(full_text or "")
    host = (urlparse(url or "").netloc or "").lower()
    # OpenAI posts can be concise but still complete; accept shorter extraction.
    if ("openai.com" in host) and quality >= 0.10 and full_len >= 300:
        return True
    if full_len < 700:
        return False
    # Low but non-zero quality is fine: editor needs readable content.
    if quality >= 0.08 and full_len >= 700:
        return True
    if quality >= 0.15 and full_len >= 500:
        return True
    return False


def _dedupe_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = stable_hash(line.strip().lower())[:16]
        if key in seen:
            continue
        seen.add(key)
        out.append(line.strip())
    return out


def _save_health_metric(source_id: int, success_rate: float, avg_latency_ms: float, parse_quality_avg: float, stale_minutes: float, last_error: str | None) -> None:
    with session_scope() as session:
        session.add(
            SourceHealthMetric(
                source_id=source_id,
                window_started_at=datetime.utcnow(),
                window_minutes=60,
                success_rate=success_rate,
                avg_latency_ms=avg_latency_ms,
                parse_quality_avg=parse_quality_avg,
                stale_minutes=stale_minutes,
                last_error=last_error,
            )
        )


def _load_feed(rss_url: str):
    """
    Fetch RSS/Atom feed content with explicit timeouts.

    Important: never fall back to `feedparser.parse(url)` because it may perform its own network
    request without our timeouts and hang the whole ingestion cycle.
    """
    text = ""
    status_code: int | None = None
    try:
        with httpx.Client(follow_redirects=True, timeout=20, headers=RSS_HEADERS) as client:
            resp = client.get(rss_url)
            status_code = int(resp.status_code)
            if 200 <= resp.status_code < 400 and (resp.text or "").strip():
                text = resp.text
            else:
                # Some feeds block non-browser clients (403/429/etc). Try Playwright fallback.
                if _should_use_browser_fetch(rss_url, resp.status_code, resp.text):
                    browser_text, _, browser_status, _ = _fetch_page_browser(rss_url)
                    if browser_status and 200 <= browser_status < 400 and browser_text and browser_text.strip():
                        text = browser_text
    except Exception:
        text = ""

    parsed = feedparser.parse(text or "")
    # Attach minimal diagnostics for downstream metrics (best-effort).
    try:
        parsed["nv_status_code"] = status_code
        parsed["nv_url"] = rss_url
    except Exception:
        pass
    return parsed


def geo_check_sources(limit: int | None = None, timeout_s: int = 15, progress_cb=None) -> dict:
    """
    Quick diagnostic: fetch each source.rss_url and classify common blocks.
    Intended to detect Geo/IP restrictions vs anti-bot vs generic failures.
    """
    def _classify(status_code: int | None, text: str) -> str:
        t = (text or "").lower()
        if status_code == 451:
            return "geo_block"
        if status_code in {401, 402, 403, 406}:
            if any(x in t for x in ["not available", "your region", "your country", "unavailable in", "restricted", "санкц", "росси", "region blocked"]):
                return "geo_block"
            if any(x in t for x in ["captcha", "not a robot", "cloudflare", "perimeterx", "akamai", "verify you are human", "attention required"]):
                return "anti_bot"
            return "forbidden"
        if status_code == 429:
            return "rate_limited"
        if status_code and status_code >= 500:
            return "server_error"
        if status_code and 200 <= status_code < 400:
            return "ok"
        if status_code:
            return "http_error"
        return "network_error"

    with session_scope() as session:
        q = select(Source).where(Source.is_active.is_(True)).order_by(Source.priority_rank.asc())
        if limit is not None:
            q = q.limit(max(1, int(limit)))
        sources = session.scalars(q).all()

    total = len(sources)
    out: list[dict] = []
    ok = 0
    geo = 0
    antibot = 0
    other = 0
    with httpx.Client(follow_redirects=True, timeout=timeout_s, headers=RSS_HEADERS) as client:
        for idx, s in enumerate(sources, start=1):
            if progress_cb:
                try:
                    progress_cb(idx, total, s.name)
                except Exception:
                    pass
            status_code = None
            body = ""
            err = None
            try:
                resp = client.get(s.rss_url)
                status_code = int(resp.status_code)
                body = (resp.text or "")[:20_000]
            except Exception as exc:
                err = str(exc)
            cls = _classify(status_code, body)
            if cls == "ok":
                ok += 1
            elif cls == "geo_block":
                geo += 1
            elif cls in {"anti_bot", "rate_limited"}:
                antibot += 1
            else:
                other += 1
            out.append(
                {
                    "source_id": int(s.id),
                    "name": s.name,
                    "url": s.rss_url,
                    "status_code": status_code,
                    "class": cls,
                    "error": err,
                }
            )

    return {
        "total": total,
        "ok": ok,
        "geo_block": geo,
        "anti_bot_or_rate_limited": antibot,
        "other_errors": other,
        "results": out,
    }


def fetch_source_articles(
    source: Source,
    days_back: int = 30,
    hours_back: int | None = None,
    fetch_full_pages: bool = True,
    max_entries: int = 120,
    progress_cb=None,
) -> int:
    parsed = _load_feed(source.rss_url)
    if parsed.bozo and not parsed.entries:
        _save_health_metric(source.id, 0.0, 0.0, 0.0, 0.0, str(parsed.bozo_exception) if parsed.bozo_exception else "bozo")
        return 0

    if hours_back is not None:
        min_dt = datetime.utcnow() - timedelta(hours=hours_back)
    else:
        min_dt = datetime.utcnow() - timedelta(days=days_back)
    inserted = 0
    processed = 0
    success_fetch = 0
    latency_sum = 0.0
    quality_sum = 0.0
    latest_published: datetime | None = None
    last_error: str | None = None

    entries = list(getattr(parsed, "entries", []) or [])
    total_entries = min(len(entries), max_entries)
    if progress_cb:
        try:
            progress_cb("read", 0, total_entries, source.name)
        except Exception:
            pass

    with session_scope() as session:
        max_rank = int(session.scalar(select(func.max(Source.priority_rank))) or 22)
        score_fn = None
        if settings.auto_score_on_ingest:
            # Local import to avoid import cycles at module import time.
            from app.services.scoring import score_article_in_session as _score_article_in_session

            score_fn = _score_article_in_session

        for entry in entries:
            processed += 1
            if processed > max_entries:
                break
            if progress_cb:
                try:
                    progress_cb("read", processed, total_entries, source.name)
                except Exception:
                    pass
            link = normalize_url(entry.get("link", ""))
            if not link:
                continue

            external_id = _entry_external_id(entry)
            content_hash = _entry_hash(entry)

            raw_exists = session.scalar(
                select(RawFeedEntry.id).where(RawFeedEntry.source_id == source.id, RawFeedEntry.external_id == external_id)
            )
            if raw_exists:
                continue
            # Some feeds change entry IDs; guard by content_hash too.
            hash_exists = session.scalar(
                select(RawFeedEntry.id).where(RawFeedEntry.source_id == source.id, RawFeedEntry.content_hash == content_hash)
            )
            if hash_exists:
                continue

            published_at = _parse_dt(entry.get("published") or entry.get("updated"))
            if published_at and published_at < min_dt:
                continue

            title = (entry.get("title") or "").strip() or "Untitled"
            subtitle = strip_html(entry.get("summary", ""))[:350]
            tags = [tag.get("term") for tag in entry.get("tags", []) if tag.get("term")]
            fallback_text = _extract_html_text(entry)

            html = None
            final_url = link
            status_code = None
            latency_ms = 0.0
            full_text = ""
            quality = 0.0
            canonical_url = link
            if fetch_full_pages:
                html, final_url, status_code, latency_ms = _fetch_page(link)
                latency_sum += latency_ms
                canonical_url = _extract_canonical(html or "", final_url)
                full_text, quality = _extract_full_text(html)
                quality_sum += quality
                if status_code and 200 <= status_code < 400:
                    success_fetch += 1
                else:
                    last_error = f"status_code={status_code}"
            else:
                # Fast mode: do not fetch article pages at ingestion time.
                canonical_url = normalize_url(link)

            final_text = full_text or fallback_text
            # Be slightly permissive: many sites yield decent full text but with moderate extraction quality.
            content_mode = "full" if (full_text and len(full_text) >= 700 and quality >= 0.08) else "summary_only"
            image_url = None
            if entry.get("media_content"):
                image_url = entry["media_content"][0].get("url")

            raw_feed = RawFeedEntry(
                source_id=source.id,
                external_id=external_id,
                entry_url=link,
                payload=json.loads(json.dumps(dict(entry), default=str)),
                parsed_article={"title": title, "subtitle": subtitle, "tags": tags, "published_at": str(published_at)},
                content_hash=content_hash,
            )
            session.add(raw_feed)
            try:
                session.flush()
            except IntegrityError:
                # Another worker/run inserted the same (source_id, content_hash) concurrently.
                session.rollback()
                continue

            if not passes_ai_topic_filter(title=title, subtitle=subtitle, text=final_text or subtitle, tags=tags):
                continue

            exists_by_url = session.scalar(select(Article.id).where(Article.canonical_url == canonical_url))
            exists_by_external = session.scalar(
                select(Article.id).where(Article.source_id == source.id, Article.external_id == external_id)
            )
            if exists_by_url or exists_by_external:
                continue

            article = Article(
                source_id=source.id,
                raw_feed_entry_id=raw_feed.id,
                external_id=external_id,
                content_hash=content_hash,
                title=title,
                subtitle=subtitle,
                tags=tags,
                text=final_text or subtitle or "",
                content_mode=content_mode,
                image_url=image_url,
                published_at=published_at,
                canonical_url=canonical_url,
                status=ArticleStatus.INBOX,
            )
            session.add(article)
            session.flush()

            if score_fn is not None:
                try:
                    score_fn(session, article, max_rank=max_rank)
                except Exception:
                    # Scoring failures shouldn't block ingestion.
                    pass

            if fetch_full_pages:
                session.add(
                    RawPageSnapshot(
                        article_id=article.id,
                        source_id=source.id,
                        url=link,
                        final_url=final_url,
                        html_text=html,
                        status_code=status_code,
                        latency_ms=latency_ms,
                        parse_quality=quality,
                    )
                )

            inserted += 1
            if progress_cb:
                try:
                    progress_cb("save", inserted, total_entries, source.name)
                except Exception:
                    pass
            if published_at and (latest_published is None or published_at > latest_published):
                latest_published = published_at

    stale_minutes = 0.0
    if latest_published:
        stale_minutes = max(0.0, (datetime.utcnow() - latest_published).total_seconds() / 60.0)
    success_rate = (success_fetch / processed) if processed else 0.0
    avg_latency = (latency_sum / processed) if processed else 0.0
    avg_quality = (quality_sum / processed) if processed else 0.0
    _save_health_metric(source.id, success_rate, avg_latency, avg_quality, stale_minutes, last_error)

    return inserted


def _extract_published_at_from_html(html: str) -> datetime | None:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for prop in ["article:published_time", "og:published_time"]:
            tag = soup.find("meta", property=prop)
            if tag and tag.get("content"):
                return _parse_dt(str(tag.get("content")))
        tag = soup.find("meta", attrs={"name": "pubdate"})
        if tag and tag.get("content"):
            return _parse_dt(str(tag.get("content")))
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            return _parse_dt(str(time_tag.get("datetime")))
    except Exception:
        return None
    return None


def _extract_section_links(section_url: str, html: str) -> list[str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        base = urlparse(section_url)
        links: list[str] = []
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
                continue
            abs_url = urljoin(section_url, href)
            u = urlparse(abs_url)
            if u.scheme not in {"http", "https"}:
                continue
            if u.netloc and u.netloc.lower() != (base.netloc or "").lower():
                continue
            norm = normalize_url(abs_url)
            # Heuristic: avoid non-article pages
            low = norm.lower()
            if any(x in low for x in ["/tag/", "/tags/", "/category/", "/categories/", "/author/", "/about", "/privacy", "/terms"]):
                continue
            links.append(norm)
        # keep order, de-dupe
        out: list[str] = []
        seen: set[str] = set()
        for l in links:
            if l in seen:
                continue
            seen.add(l)
            out.append(l)
        return out[:200]
    except Exception:
        return []


def fetch_source_articles_html(
    source: Source,
    days_back: int = 30,
    hours_back: int | None = None,
    fetch_full_pages: bool = True,
) -> int:
    section_url = source.rss_url
    if hours_back is not None:
        min_dt = datetime.utcnow() - timedelta(hours=hours_back)
    else:
        min_dt = datetime.utcnow() - timedelta(days=days_back)

    html, final_url, status_code, latency_ms = _fetch_page(section_url)
    if not html or not (status_code and 200 <= status_code < 400):
        _save_health_metric(source.id, 0.0, latency_ms, 0.0, 0.0, f"status_code={status_code}")
        return 0

    links = _extract_section_links(final_url or section_url, html)
    processed = 0
    inserted = 0
    success_fetch = 0
    latency_sum = 0.0
    quality_sum = 0.0
    latest_published: datetime | None = None
    last_error: str | None = None

    with session_scope() as session:
        max_rank = int(session.scalar(select(func.max(Source.priority_rank))) or 22)
        score_fn = None
        if settings.auto_score_on_ingest:
            from app.services.scoring import score_article_in_session as _score_article_in_session

            score_fn = _score_article_in_session

        for link in links:
            processed += 1
            exists_by_url = session.scalar(select(Article.id).where(Article.canonical_url == link))
            if exists_by_url:
                continue

            if not fetch_full_pages:
                # Fast mode does not support html sources; they require page fetch.
                continue

            page_html, page_final, page_status, page_latency = _fetch_page(link)
            latency_sum += page_latency
            if page_status and 200 <= page_status < 400:
                success_fetch += 1
            else:
                last_error = f"status_code={page_status}"
                continue
            canonical_url = _extract_canonical(page_html or "", page_final or link)
            if session.scalar(select(Article.id).where(Article.canonical_url == canonical_url)):
                continue

            published_at = _extract_published_at_from_html(page_html or "")
            if published_at and published_at < min_dt:
                continue

            full_text, quality = _extract_full_text(page_html)
            quality_sum += quality
            title = "Untitled"
            subtitle = ""
            try:
                soup = BeautifulSoup(page_html or "", "html.parser")
                t = soup.find("meta", property="og:title")
                if t and t.get("content"):
                    title = str(t.get("content")).strip()[:500] or title
                elif soup.title and soup.title.string:
                    title = str(soup.title.string).strip()[:500] or title
                d = soup.find("meta", attrs={"name": "description"})
                if d and d.get("content"):
                    subtitle = str(d.get("content")).strip()[:350]
            except Exception:
                pass

            final_text = full_text or subtitle or ""
            if not passes_ai_topic_filter(title=title, subtitle=subtitle, text=final_text, tags=[]):
                continue

            content_mode = "full" if (full_text and len(full_text) >= 700 and quality >= 0.08) else "summary_only"
            external_id = stable_hash(canonical_url)[:64]
            content_hash = stable_hash(title + "|" + (subtitle or "") + "|" + canonical_url)[:64]

            raw_feed = RawFeedEntry(
                source_id=source.id,
                external_id=external_id,
                entry_url=canonical_url,
                payload={"kind": "html", "section_url": section_url, "url": canonical_url},
                parsed_article={"title": title, "subtitle": subtitle, "tags": [], "published_at": str(published_at)},
                content_hash=content_hash,
            )
            session.add(raw_feed)
            session.flush()

            article = Article(
                source_id=source.id,
                raw_feed_entry_id=raw_feed.id,
                external_id=external_id,
                content_hash=content_hash,
                title=title,
                subtitle=subtitle,
                tags=[],
                text=final_text,
                content_mode=content_mode,
                image_url=None,
                published_at=published_at,
                canonical_url=canonical_url,
                status=ArticleStatus.INBOX,
            )
            session.add(article)
            session.flush()

            if score_fn is not None:
                try:
                    score_fn(session, article, max_rank=max_rank)
                except Exception:
                    pass

            session.add(
                RawPageSnapshot(
                    article_id=article.id,
                    source_id=source.id,
                    url=canonical_url,
                    final_url=page_final,
                    html_text=(page_html or "")[:1_000_000],
                    status_code=page_status,
                    latency_ms=page_latency,
                    parse_quality=quality,
                )
            )
            inserted += 1
            if published_at and (latest_published is None or published_at > latest_published):
                latest_published = published_at

            if inserted >= 40:
                break

    stale_minutes = 0.0
    if latest_published:
        stale_minutes = max(0.0, (datetime.utcnow() - latest_published).total_seconds() / 60.0)
    success_rate = (success_fetch / processed) if processed else 0.0
    avg_latency = (latency_sum / processed) if processed else 0.0
    avg_quality = (quality_sum / processed) if processed else 0.0
    _save_health_metric(source.id, success_rate, avg_latency, avg_quality, stale_minutes, last_error)
    return inserted


def check_source_health(source_id: int) -> dict:
    with session_scope() as session:
        src = session.get(Source, source_id)
        if not src:
            return {"ok": False, "error": "source_not_found"}
        kind = (src.kind or "rss").lower()
        url = src.rss_url

    if kind == "html":
        html, final_url, status_code, latency_ms = _fetch_page(url)
        links = _extract_section_links(final_url or url, html or "") if html else []
        return {
            "ok": True,
            "kind": "html",
            "url": url,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 1),
            "links_found": len(links),
            "sample_links": links[:5],
        }

    parsed = _load_feed(url)
    entries = list(getattr(parsed, "entries", []) or [])
    return {
        "ok": True,
        "kind": "rss",
        "url": url,
        "bozo": bool(getattr(parsed, "bozo", False)),
        "bozo_exception": str(getattr(parsed, "bozo_exception", "") or ""),
        "entries_found": len(entries),
        "sample_titles": [strip_html((e.get("title") or "")).strip() for e in entries[:5]],
    }


def run_ingestion(days_back: int = 30, hours_back: int | None = None, status_cb=None, progress_cb=None) -> dict[str, int]:
    results: dict[str, int] = {}
    with session_scope() as session:
        source_ids = [row[0] for row in session.execute(select(Source.id).where(Source.is_active.is_(True)).order_by(Source.priority_rank.asc())).all()]

    total = len(source_ids)
    for idx, source_id in enumerate(source_ids, start=1):
        with session_scope() as session:
            source = session.get(Source, source_id)
            if not source:
                continue
            source_name = source.name
            source_kind = (source.kind or "rss").lower()
        if status_cb:
            try:
                status_cb(f"{idx}/{total}: {source_name}")
            except Exception:
                pass
        if source_kind == "html":
            results[source_name] = fetch_source_articles_html(source, days_back=days_back, hours_back=hours_back)
        else:
            results[source_name] = fetch_source_articles(
                source, days_back=days_back, hours_back=hours_back, progress_cb=progress_cb
            )

    return results


def run_ingestion_fast(
    days_back: int = 30,
    hours_back: int | None = None,
    max_entries: int = 120,
    status_cb=None,
    progress_cb=None,
) -> dict[str, int]:
    """RSS-only ingestion that does not fetch full article pages (much faster)."""
    results: dict[str, int] = {}
    with session_scope() as session:
        source_ids = [
            row[0]
            for row in session.execute(
                select(Source.id)
                .where(Source.is_active.is_(True))
                .order_by(Source.priority_rank.asc())
            ).all()
        ]

    total = len(source_ids)
    for idx, source_id in enumerate(source_ids, start=1):
        with session_scope() as session:
            source = session.get(Source, source_id)
            if not source:
                continue
            source_name = source.name
            source_kind = (source.kind or "rss").lower()
        if status_cb:
            try:
                status_cb(f"{idx}/{total}: {source_name}")
            except Exception:
                pass

        if source_kind != "rss":
            # html sources require page fetch; skip in fast mode
            continue

        results[source_name] = fetch_source_articles(
            source,
            days_back=days_back,
            hours_back=hours_back,
            fetch_full_pages=False,
            max_entries=max_entries,
            progress_cb=progress_cb,
        )

    return results


def run_backfill_batched(total_days: int = 30, batch_days: int = 3) -> list[dict[str, int]]:
    batches: list[dict[str, int]] = []
    days_remaining = total_days
    while days_remaining > 0:
        current = min(batch_days, days_remaining)
        batches.append(run_ingestion(days_back=current))
        days_remaining -= current
    return batches


def enrich_summary_only_articles(limit: int = 200, days_back: int = 30, progress_cb=None) -> dict:
    min_dt = datetime.utcnow() - timedelta(days=days_back)
    scanned = 0
    upgraded = 0

    with session_scope() as session:
        rows = session.scalars(
            select(Article)
            .where(
                Article.content_mode == "summary_only",
                Article.created_at >= min_dt,
                Article.status != ArticleStatus.DOUBLE,
            )
            .order_by(Article.created_at.desc())
            .limit(limit)
        ).all()
        total = len(rows)
        if progress_cb:
            try:
                progress_cb(0, total)
            except Exception:
                pass

        for article in rows:
            scanned += 1
            if progress_cb:
                try:
                    progress_cb(scanned, total)
                except Exception:
                    pass
            html, final_url, status_code, latency_ms = _fetch_page(article.canonical_url)
            full_text, quality = _extract_full_text(html)
            if full_text and (
                _should_upgrade_text(article.text or "", full_text, quality)
                or _should_set_full_when_prev_summary_only(article.content_mode, full_text, quality, article.canonical_url)
            ):
                article.text = full_text
                article.content_mode = "full"
                article.updated_at = datetime.utcnow()
                upgraded += 1
                session.add(
                    RawPageSnapshot(
                        article_id=article.id,
                        source_id=article.source_id,
                        url=article.canonical_url,
                        final_url=final_url,
                        html_text=(html or "")[:1_000_000],
                        status_code=status_code,
                        latency_ms=latency_ms,
                        parse_quality=quality,
                    )
                )

    return {"scanned": scanned, "upgraded_to_full": upgraded, "still_summary_only": max(0, scanned - upgraded)}


def enrich_article_from_source(article_id: int) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return {"ok": False, "error": "article_not_found"}
        source_id = article.source_id
        url = article.canonical_url
        prev_len = len(article.text or "")
        prev_mode = article.content_mode or "summary_only"

    scraped = scrape_url(url)
    snap_html = scraped.get("html") or ""
    final_url = scraped.get("final_url") or url
    status_code = scraped.get("status_code")
    latency_ms = float(scraped.get("latency_ms") or 0.0)
    full_text = (scraped.get("text") or "").strip()
    quality = float(scraped.get("parse_quality") or 0.0)
    extracted_len = int(scraped.get("text_len") or 0)

    updated = False
    new_mode = prev_mode
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return {"ok": False, "error": "article_not_found"}
        if full_text and (
            _should_upgrade_text(article.text or "", full_text, quality)
            or _should_set_full_when_prev_summary_only(article.content_mode, full_text, quality, article.canonical_url)
        ):
            article.text = full_text
            article.content_mode = "full"
            article.updated_at = datetime.utcnow()
            updated = True
            new_mode = "full"

        session.add(
            RawPageSnapshot(
                article_id=article.id,
                source_id=source_id,
                url=url,
                final_url=final_url,
                html_text=(snap_html or "")[:1_000_000],
                status_code=status_code,
                latency_ms=float(latency_ms or 0.0),
                parse_quality=quality,
            )
        )

    reason = None
    if not updated:
        if status_code in {401, 403, 406, 429}:
            reason = f"blocked_http_{status_code}"
        elif scraped.get("paywalled_or_thin"):
            reason = "paywalled_or_thin"
        elif extracted_len < 700:
            reason = "extracted_too_short"
        else:
            reason = "not_better_than_existing"

    return {
        "ok": True,
        "article_id": article_id,
        "updated": updated,
        "status_code": status_code,
        "final_url": final_url,
        "parse_quality": quality,
        "paywalled_or_thin": bool(scraped.get("paywalled_or_thin")),
        "extracted_len": extracted_len,
        "reason": reason,
        "prev_mode": prev_mode,
        "content_mode": new_mode,
        "prev_len": prev_len,
        "new_len": extracted_len if updated else prev_len,
    }
