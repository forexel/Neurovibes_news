#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_cookie_json(path: str) -> str:
    p = Path(path)
    raw = p.read_text(encoding="utf-8").strip()
    # Validate that it's JSON (list[dict] is expected by Playwright add_cookies).
    obj = json.loads(raw)
    if not isinstance(obj, list):
        raise ValueError("cookies json must be a list")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a URL and extract main article text.")
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument("--browser", action="store_true", help="Force Playwright browser fetch")
    parser.add_argument("--cookies-json", default="", help="Path to cookies JSON (Playwright format)")
    parser.add_argument("--out", default="", help="Write extracted text to file")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output JSON with diagnostics")
    args = parser.parse_args()

    # Ensure repo root is importable even when script is executed from /app/scripts in Docker.
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # Import from app package (works inside docker compose exec api).
    from app.core.config import settings
    from app.services.ingestion import _extract_full_text, _fetch_page_browser, scrape_url, _looks_paywalled_or_thin

    if args.cookies_json:
        settings.browser_cookies_json = _load_cookie_json(args.cookies_json)  # type: ignore[misc]

    url = args.url.strip()

    if not args.browser:
        out = scrape_url(url)
        extracted = (out.get("text") or "").strip()
        diag = {
            "ok": bool(extracted),
            "url": url,
            "final_url": out.get("final_url") or url,
            "status_code": out.get("status_code"),
            "method": "auto",
            "latency_ms": round(float(out.get("latency_ms") or 0.0), 1),
            "html_len": int(out.get("html_len") or 0),
            "paywalled_or_thin": bool(out.get("paywalled_or_thin")),
            "extracted_len": len(extracted),
            "quality": round(float(out.get("parse_quality") or 0.0), 3),
        }
    else:
        html, final_url, status_code, latency_ms = _fetch_page_browser(url)
        extracted, quality = _extract_full_text(html)
        extracted = (extracted or "").strip()
        diag = {
            "ok": bool(extracted),
            "url": url,
            "final_url": final_url or url,
            "status_code": status_code,
            "method": "browser_forced",
            "latency_ms": round(float(latency_ms or 0.0), 1),
            "html_len": 0 if not html else len(html),
            "paywalled_or_thin": bool(_looks_paywalled_or_thin(html)),
            "extracted_len": len(extracted),
            "quality": round(float(quality or 0.0), 3),
        }

    if args.out:
        Path(args.out).write_text(extracted, encoding="utf-8")

    if args.as_json:
        sys.stdout.write(json.dumps({**diag, "text": extracted}, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(json.dumps(diag, ensure_ascii=False) + "\n")
        if extracted:
            sys.stdout.write("\n" + extracted + "\n")

    # Non-zero exit code if we couldn't extract.
    return 0 if extracted else 2


if __name__ == "__main__":
    raise SystemExit(main())
