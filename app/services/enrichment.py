from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select

from app.core.config import settings
from app.models import Article, ArticleEnrichment
from app.services.llm import get_client, track_usage_from_response
from app.services.topic_filter import _normalize_text

_CONTENT_TYPES = {"hot", "tool", "case", "playbook", "trend", "other"}
_TOOL_HINTS = ("tool", "assistant", "copilot", "plugin", "api", "sdk", "template", "prompt", "agent")
_PLAYBOOK_HINTS = ("how to", "guide", "step-by-step", "template", "playbook", "workflow", "tutorial")
_CASE_HINTS = ("case study", "used", "using", "adoption", "rolled out", "deployed", "saved", "reduced")
_HOT_HINTS = ("today", "now", "urgent", "breaking", "launches", "launched", "releases", "released")
_RISK_HINTS = {
    "too_technical": ("benchmark", "lora", "hypernetwork", "retrieval", "distillation", "zero-shot", "architecture"),
    "funding_hype": ("funding", "valuation", "raised", "billion", "million", "acquisition"),
    "infra_noise": ("chips", "gpu", "gpus", "data center", "datacenter", "server", "compute"),
}
_USE_CASE_HINTS = {
    "marketing": ("marketing", "content", "ads", "campaign"),
    "sales": ("sales", "crm", "lead"),
    "support": ("support", "customer service", "helpdesk"),
    "operations": ("operations", "workflow", "automation", "back office"),
    "founder": ("small business", "startup", "founder", "entrepreneur"),
}


def _clip10(value: int | float | None) -> int:
    try:
        return int(max(0, min(10, round(float(value or 0)))))
    except Exception:
        return 0


def _heuristic_enrichment(article: Article) -> dict:
    text = " ".join(
        [
            _normalize_text(article.title or ""),
            _normalize_text(article.subtitle or ""),
            _normalize_text(article.text[:4000] if article.text else ""),
        ]
    )
    content_type = "other"
    if any(h in text for h in _PLAYBOOK_HINTS):
        content_type = "playbook"
    elif any(h in text for h in _TOOL_HINTS):
        content_type = "tool"
    elif any(h in text for h in _CASE_HINTS):
        content_type = "case"
    elif any(h in text for h in _HOT_HINTS):
        content_type = "hot"
    elif "trend" in text or "market" in text or "shift" in text:
        content_type = "trend"

    tool_detected = content_type == "tool" or any(h in text for h in _TOOL_HINTS)
    use_cases = [name for name, hints in _USE_CASE_HINTS.items() if any(h in text for h in hints)]
    risk_flags = [name for name, hints in _RISK_HINTS.items() if any(h in text for h in hints)]

    practical = 4
    audience_fit = 5
    actionability = 4
    if content_type == "tool":
        practical += 3
        actionability += 3
        audience_fit += 2
    elif content_type == "case":
        practical += 2
        actionability += 2
        audience_fit += 2
    elif content_type == "playbook":
        practical += 3
        actionability += 4
        audience_fit += 2
    elif content_type == "hot":
        practical += 1
        audience_fit += 1
    elif content_type == "trend":
        practical -= 1
        actionability -= 2

    if "too_technical" in risk_flags:
        practical -= 2
        audience_fit -= 2
        actionability -= 2
    if "funding_hype" in risk_flags or "infra_noise" in risk_flags:
        practical -= 1
        audience_fit -= 1

    tool_name = None
    if tool_detected:
        m = re.search(r"\b([A-Z][A-Za-z0-9\-\+]{1,30})\b", article.title or "")
        if m:
            tool_name = m.group(1)

    return {
        "content_type": content_type,
        "practical_value": _clip10(practical),
        "audience_fit": _clip10(audience_fit),
        "actionability": _clip10(actionability),
        "use_cases": use_cases,
        "tool_detected": bool(tool_detected),
        "tool_name": tool_name,
        "tool_is_free_tier": None,
        "requires_code": None,
        "setup_time_minutes": None,
        "risk_flags": risk_flags,
        "why_short": "Heuristic enrichment fallback",
        "enrichment_json": {"mode": "heuristic"},
    }


def _llm_enrichment(article: Article) -> dict | None:
    if not settings.openrouter_api_key:
        return None
    prompt = f"""
Ты редактор канала про ИИ для микро и малого бизнеса.
Цель — находить практичные материалы для аудитории 18-64.
Ключевой вопрос: можно ли это применить в течение 7 дней?
Не хайпуй. Возвращай только JSON.

JSON schema:
{{
  "content_type": "hot|tool|case|playbook|trend|other",
  "practical_value": 0,
  "audience_fit": 0,
  "actionability": 0,
  "use_cases": ["marketing|sales|support|operations|founder"],
  "tool_detected": false,
  "tool_name": null,
  "tool_is_free_tier": null,
  "requires_code": null,
  "setup_time_minutes": null,
  "risk_flags": ["too_technical|funding_hype|infra_noise|weak_source|wow_but_risky"],
  "why_short": "short reason"
}}

Title: {article.title}
Subtitle: {article.subtitle}
Text: {article.text[:5000]}
URL: {article.canonical_url}
"""
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Return strictly valid JSON. Do not invent facts not present in the text."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        track_usage_from_response(resp, operation="enrichment.classify_article", model=settings.llm_text_model, kind="chat")
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return None

    content_type = str(data.get("content_type") or "other").strip().lower()
    if content_type not in _CONTENT_TYPES:
        content_type = "other"
    return {
        "content_type": content_type,
        "practical_value": _clip10(data.get("practical_value")),
        "audience_fit": _clip10(data.get("audience_fit")),
        "actionability": _clip10(data.get("actionability")),
        "use_cases": [str(x).strip().lower() for x in list(data.get("use_cases") or []) if str(x).strip()][:8],
        "tool_detected": bool(data.get("tool_detected")),
        "tool_name": (str(data.get("tool_name") or "").strip() or None),
        "tool_is_free_tier": data.get("tool_is_free_tier"),
        "requires_code": data.get("requires_code"),
        "setup_time_minutes": int(data.get("setup_time_minutes")) if data.get("setup_time_minutes") is not None else None,
        "risk_flags": [str(x).strip().lower() for x in list(data.get("risk_flags") or []) if str(x).strip()][:8],
        "why_short": (str(data.get("why_short") or "").strip() or "LLM enrichment"),
        "enrichment_json": {"mode": "llm", "raw": data},
    }


def enrich_article_in_session(session, article: Article, force: bool = False) -> ArticleEnrichment:
    row = session.scalars(select(ArticleEnrichment).where(ArticleEnrichment.article_id == int(article.id))).first()
    if row is not None and not force:
        return row

    data = _heuristic_enrichment(article)
    if row is None:
        row = ArticleEnrichment(article_id=int(article.id))
        session.add(row)

    row.content_type = str(data["content_type"])
    row.practical_value = _clip10(data["practical_value"])
    row.audience_fit = _clip10(data["audience_fit"])
    row.actionability = _clip10(data["actionability"])
    row.use_cases = list(data.get("use_cases") or [])
    row.tool_detected = bool(data.get("tool_detected"))
    row.tool_name = data.get("tool_name")
    row.tool_is_free_tier = data.get("tool_is_free_tier")
    row.requires_code = data.get("requires_code")
    row.setup_time_minutes = data.get("setup_time_minutes")
    row.risk_flags = list(data.get("risk_flags") or [])
    row.why_short = data.get("why_short")
    row.enrichment_json = data.get("enrichment_json") or {}
    row.enriched_at = datetime.utcnow()

    article.content_type = row.content_type
    article.practical_value = row.practical_value
    article.audience_fit = row.audience_fit
    article.updated_at = datetime.utcnow()
    return row
