from __future__ import annotations

import re

from app.services.runtime_settings import get_runtime_bool, get_runtime_csv_list


def _normalize_text(value: str) -> str:
    v = (value or "").lower()
    v = re.sub(r"[^a-z0-9+#\-\s]", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v


def passes_ai_topic_filter(title: str, subtitle: str, text: str, tags: list[str] | None = None) -> bool:
    if not get_runtime_bool("ai_prefilter_enabled", default=True):
        return True

    tags = tags or []
    title_n = _normalize_text(title or "")
    subtitle_n = _normalize_text(subtitle or "")
    tags_n = _normalize_text(" ".join(tags))
    body_n = _normalize_text(text or "")
    hay = _normalize_text(" ".join([title_n, subtitle_n, body_n, tags_n]))
    if not hay:
        return False

    keywords = [x.strip().lower() for x in get_runtime_csv_list("ai_prefilter_keywords_csv") if x.strip()]
    title_hits = 0
    title_subtitle_hits = 0
    body_hits = 0

    for keyword in keywords:
        k = _normalize_text(keyword)
        if not k:
            continue
        # Single-token keywords must match as standalone words, otherwise
        # noise like "management" may trigger keyword "agent".
        if " " not in k:
            pattern = rf"\b{re.escape(k)}\b"
            in_title_only = bool(re.search(pattern, title_n))
            in_subtitle = bool(re.search(pattern, subtitle_n))
            in_tags = bool(re.search(pattern, tags_n))
            in_body = bool(re.search(pattern, body_n))
        else:
            in_title_only = k in title_n
            in_subtitle = k in subtitle_n
            in_tags = k in tags_n
            in_body = k in body_n
        if in_title_only:
            title_hits += 1
        if in_title_only or in_subtitle or in_tags:
            title_subtitle_hits += 1
        if in_body:
            body_hits += 1

    # Strict centrality rules:
    # HN-like entries are noisy (title + "Article URL / Comments URL" metadata).
    # For those, require explicit AI signal in title/subtitle/tags and do not allow body-only pass.
    is_hn_style = ("article url" in subtitle_n and "comments url" in subtitle_n) or (
        "article url" in body_n and "comments url" in body_n
    )
    if is_hn_style:
        if title_hits >= 1:
            return True
        if title_subtitle_hits >= 2:
            return True
        return False

    # 1) Strong pass if AI is in title.
    if title_hits >= 1:
        return True

    # 2) Or enough AI signal across subtitle/tags/body.
    if title_subtitle_hits >= 2:
        return True

    # 3) If only one hit in subtitle/tags, require stronger body density.
    if title_subtitle_hits >= 1 and body_hits >= 2:
        return True

    # 4) Body-only pass requires high density.
    if body_hits >= 3:
        return True

    return False
