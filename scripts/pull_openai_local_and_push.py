#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from html import unescape
from urllib import error, parse, request
from dataclasses import dataclass


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


@dataclass
class FetchResult:
    status_code: int | None
    final_url: str
    html: str
    text: str


def _extract_text(html: str) -> str:
    # Lightweight extractor without external dependencies.
    cleaned = re.sub(r"(?is)<(script|style|noscript|svg|footer|nav|header|aside).*?>.*?</\1>", " ", html)
    # Keep paragraph-like structure.
    cleaned = re.sub(r"(?is)</(p|li|h1|h2|h3|h4|h5|h6|div|section|article)>", "\n", cleaned)
    cleaned = re.sub(r"(?is)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    text = unescape(cleaned)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join([ln.strip() for ln in text.splitlines() if ln.strip()])
    return text


def fetch_page(url: str, cookies_header: str = "", timeout_s: int = 45) -> FetchResult:
    req_headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookies_header.strip():
        req_headers["Cookie"] = cookies_header.strip()

    status_code: int | None = None
    final_url = url
    html = ""
    opener = request.build_opener(request.HTTPRedirectHandler())
    req = request.Request(url, headers=req_headers, method="GET")
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            status_code = int(getattr(resp, "status", 200) or 200)
            final_url = str(resp.geturl() or url)
            if 200 <= status_code < 400:
                html = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as e:
        status_code = int(e.code)
        final_url = url
        try:
            html = e.read().decode("utf-8", errors="replace")
        except Exception:
            html = ""
    except Exception:
        status_code = None
        final_url = url
        html = ""

    text = _extract_text(html) if html else ""
    return FetchResult(status_code=status_code, final_url=final_url, html=html, text=text)


def post_override(base_url: str, article_id: int, text: str, timeout_s: int = 45) -> dict:
    url = f"{base_url.rstrip('/')}/articles/{article_id}/text/override"
    body = json.dumps({"text": text}).encode("utf-8")
    req = request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def post_generate(base_url: str, article_id: int, timeout_s: int = 60) -> dict:
    url = f"{base_url.rstrip('/')}/articles/{article_id}/post/generate"
    req = request.Request(url, data=b"", method="POST")
    with request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def main() -> int:
    p = argparse.ArgumentParser(
        description="Fetch full OpenAI page text locally and push into article text on server."
    )
    p.add_argument("--article-id", type=int, required=True, help="Target article id in portal DB, e.g. 6317")
    p.add_argument("--url", required=True, help="Source URL to fetch, e.g. https://openai.com/index/introducing-gpt-5-4/")
    p.add_argument("--base-url", default="http://77.222.55.88:18100", help="Portal API base URL")
    p.add_argument(
        "--cookies",
        default="",
        help="Raw Cookie header string (optional), e.g. '__cf_bm=...; _cfuvid=...; cf_clearance=...'",
    )
    p.add_argument("--min-chars", type=int, default=1800, help="Minimum extracted text length to accept")
    p.add_argument("--generate-post", action="store_true", help="Run /post/generate after text override")
    args = p.parse_args()

    res = fetch_page(args.url, cookies_header=args.cookies)
    print(json.dumps({"fetch_status": res.status_code, "final_url": res.final_url, "text_len": len(res.text)}, ensure_ascii=False))

    if not res.html or len(res.text) < int(args.min_chars):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "full_text_not_extracted",
                    "hint": "Open page in browser, refresh challenge, pass --cookies with __cf_bm/_cfuvid/cf_clearance, then retry.",
                },
                ensure_ascii=False,
            )
        )
        return 2

    out = post_override(args.base_url, int(args.article_id), res.text)
    print(json.dumps({"override_ok": True, "response": out}, ensure_ascii=False))

    if args.generate_post:
        gen = post_generate(args.base_url, int(args.article_id))
        print(json.dumps({"generate_ok": True, "response": gen}, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
