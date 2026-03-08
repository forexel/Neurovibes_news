from __future__ import annotations

import json
import re
import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func, select

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleStatus, ContentVersion, Score
from app.services.llm import get_client, llm_budget_allows, track_usage_from_response
from app.services.object_storage import upload_generated_image
from app.services.runtime_settings import get_runtime_csv_list, get_runtime_int


_LLM_CACHE: dict[str, tuple[float, dict]] = {}
_LLM_CACHE_MAX = 400
_LLM_CACHE_TTL_SECONDS = 6 * 60 * 60


def _cache_key(op: str, article: Article, payload: dict | None = None) -> str:
    base = {
        "op": op,
        "article_id": int(article.id),
        "title": article.title or "",
        "subtitle": article.subtitle or "",
        "canonical_url": article.canonical_url or "",
        "text_hash": hashlib.sha256(str(article.text or "").encode("utf-8", errors="ignore")).hexdigest(),
        "payload": payload or {},
    }
    raw = json.dumps(base, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _cache_get(key: str) -> dict | None:
    row = _LLM_CACHE.get(key)
    if not row:
        return None
    ts, data = row
    if (datetime.now(timezone.utc).timestamp() - float(ts)) > _LLM_CACHE_TTL_SECONDS:
        _LLM_CACHE.pop(key, None)
        return None
    return dict(data)


def _cache_set(key: str, data: dict) -> None:
    _LLM_CACHE[key] = (datetime.now(timezone.utc).timestamp(), dict(data))
    if len(_LLM_CACHE) > _LLM_CACHE_MAX:
        # keep the newest keys only
        ordered = sorted(_LLM_CACHE.items(), key=lambda kv: kv[1][0], reverse=True)[:_LLM_CACHE_MAX]
        _LLM_CACHE.clear()
        _LLM_CACHE.update(dict(ordered))


def generate_ru_summary(article_id: int) -> bool:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return False

    extraction = _extract_facts(article)
    rewrite = _rewrite_ru(article, extraction)
    rewrite = _enforce_temporal_consistency(article, rewrite, extraction)
    rewrite = _ensure_key_takeaways_block(article, rewrite, extraction)
    quality = _quality_checks(rewrite)
    factual = _factual_consistency_checks(article, extraction, rewrite)
    quality["factual"] = factual

    if not quality["is_valid"] or not factual.get("is_valid", True):
        rewrite["ru_summary"] = _safe_fallback_summary(article, extraction)
        quality = _quality_checks(rewrite)
        factual = _factual_consistency_checks(article, extraction, rewrite)
        quality["factual"] = factual

    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return False

        article.ru_title = rewrite["ru_title"]
        article.ru_summary = rewrite["ru_summary"]
        article.short_hook = rewrite["short_hook"]
        article.status = ArticleStatus.READY

        version_no = int(
            session.scalar(select(func.coalesce(func.max(ContentVersion.version_no), 0)).where(ContentVersion.article_id == article_id))
            or 0
        ) + 1
        session.add(
            ContentVersion(
                article_id=article_id,
                version_no=version_no,
                ru_title=article.ru_title,
                ru_summary=article.ru_summary,
                short_hook=article.short_hook,
                extraction_json=extraction,
                quality_report=quality,
                image_path=article.generated_image_path,
                image_prompt=None,
                selected_by_editor=False,
            )
        )

    return True


def _extract_facts(article: Article) -> dict:
    cache_key = _cache_key("extract_facts", article)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not settings.openrouter_api_key:
        out = {
            "key_points": [article.subtitle[:140]],
            "dates": [],
            "numbers": [],
            "entities": [],
            "claims": [article.title],
        }
        _cache_set(cache_key, out)
        return out

    if not llm_budget_allows("content.extract_facts", feature="content"):
        out = {
            "key_points": [article.subtitle[:140]],
            "dates": [],
            "numbers": [],
            "entities": [],
            "claims": [article.title],
        }
        _cache_set(cache_key, out)
        return out

    prompt = f"""
Extract only factual information from this article.
Return JSON only:
{{
  "key_points": ["..."],
  "dates": ["..."],
  "numbers": ["..."],
  "entities": ["..."],
  "claims": ["..."]
}}

Title: {article.title}
Subtitle: {article.subtitle}
Text: {article.text[:9000]}
"""
    client = get_client()
    try:
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Extract facts only. No speculation."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        track_usage_from_response(resp, operation="content.extract_facts", model=settings.llm_text_model, kind="chat")
        raw = resp.choices[0].message.content or "{}"
        out = json.loads(raw)
        _cache_set(cache_key, out)
        return out
    except Exception:
        out = {"key_points": [article.subtitle[:140]], "dates": [], "numbers": [], "entities": [], "claims": [article.title]}
        _cache_set(cache_key, out)
        return out


def _rewrite_ru(article: Article, extraction: dict) -> dict:
    cache_key = _cache_key("rewrite_ru", article, {"extraction": extraction})
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    style_guide = """
Ты — редактор технологического AI-канала.

Твоя задача — не просто переводить новость, а делать качественный редакторский пересказ на русском языке.

Стиль:
— Чистый, структурный, без воды.
— Короткие абзацы (1–3 предложения).
— Простые предложения, но без упрощения смысла.
— Без кликбейта, эмоций и восторгов.
— Без оценочных эпитетов («шокирующий», «революционный», «невероятный»).
— Без маркетингового языка.

Тон:
— Умный.
— Спокойный.
— Деловой.
— Технологический.
— С ощущением масштаба и контекста.

Структура текста:
1. Первый абзац — что произошло (суть события).
2. Второй абзац — практическая ценность: где это применимо и чем полезно пользователю/команде/бизнесу.
3. Если уместно — краткий контекст (рынок, конкуренция, стратегический сдвиг).
4. Если есть важные цифры/бенчмарки/ограничения — обязательно кратко добавь их в текст.
5. Блок `Ключевое:` добавляй только если он действительно помогает: 3–5 коротких пунктов с фактами (префикс `• `).

Обязательные требования к пользе:
— Явно ответь на вопрос «чем это полезно на практике».
— Назови 2–4 прикладных сценария использования (без фантазий, только из фактов и разумных выводов из extraction).
— Обязательно укажи «что стало лучше по сравнению с раньше/предыдущей версией/старым подходом».
— Если практическая польза неочевидна, честно напиши это и объясни ограничение.
— Избегай абстрактных формулировок вроде «улучшает эффективность» без конкретики.
— Если в extraction есть релевантные цифры/бенчмарки, добавь их; если релевантных цифр нет, не выдумывай их.
— Указывай доступность инструмента (где доступно: продукт/план/API) только если это прямо подтверждено фактами текста.
— Текст должен давать читателю ключевые идеи без перехода по ссылке.
— Для статей-мнений/эссе/размышлений не притягивай «где доступно/API», если это не тема материала.
— Для исследований рынка труда, политики, регулирования и трендов НЕ используй шаблон «Где применять».
— Если это не релиз инструмента/функции, вместо этого дай короткий блок «Что это значит для читателя/команды» (1–3 практичных вывода без фантазий).
— Не используй пустые шаблонные подпункты: «Где применять», «Ключевые цифры», если они не добавляют конкретную ценность в этом материале.
— Не добавляй цифры, которых нет в extraction; спорные/неподтвержденные цифры опускай.

Важно:
— Сохраняй факты, цифры, названия моделей и компаний.
— Не добавляй информацию, которой нет в тексте.
— Не домысливай.
— Не используй сложные обороты и длинные конструкции.
— Не используй англицизмы, если есть точный русский аналог.
— Термины ИИ оставляй корректно (LLM, AGI, fine-tuning и т.п.).

Длина:
— 2–3 абзаца, опционально блок `Ключевое:`.
— 700–1400 символов.

В конце не добавляй выводов «И это только начало».
Никаких эмодзи.
""".strip()

    if not settings.openrouter_api_key:
        out = {
            "ru_title": article.title,
            "ru_summary": (article.subtitle or article.text[:300])[: get_runtime_int("max_summary_chars", default=1400)],
            "short_hook": (article.subtitle or article.title)[:100],
        }
        _cache_set(cache_key, out)
        return out

    if not llm_budget_allows("content.rewrite_ru", feature="content"):
        out = {
            "ru_title": article.title,
            "ru_summary": (article.subtitle or article.text[:300])[: get_runtime_int("max_summary_chars", default=1400)],
            "short_hook": (article.subtitle or article.title)[:100],
        }
        _cache_set(cache_key, out)
        return out

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    published_at = article.published_at.isoformat() if article.published_at else ""
    prompt = f"""
{style_guide}
Используй только факты из extraction.
Проверка времени и фактов:
- Сейчас: {now_utc}
- Published_at статьи: {published_at or "unknown"}
- Никогда не пиши будущие/планируемые события как уже свершившиеся.
- Если по тексту это анонс/план/бета/ожидание — используй соответствующие формулировки (\"планирует\", \"ожидается\", \"может\", \"анонсировала\").
- Если факт уже произошёл к моменту публикации — можно писать в прошедшем времени.
- Не придумывай точные даты, если их нет в фактах.
Верни JSON only:
{{"ru_title":"...", "ru_summary":"...", "short_hook":"..."}}

Требования к полям:
- ru_title: конкретный, без маркетинговых эпитетов, с намеком на практическую пользу.
- ru_summary: обязательно содержит практическую применимость и что стало лучше vs раньше.
- short_hook: 1 короткая фраза про практическую выгоду/применение.

Extraction:
{json.dumps(extraction, ensure_ascii=False)}
Original title: {article.title}
Source url: {article.canonical_url}
"""
    client = get_client()
    try:
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Russian editor. Follow style guide exactly."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        track_usage_from_response(resp, operation="content.rewrite_ru", model=settings.llm_text_model, kind="chat")
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception:
        data = {}

    out = {
        "ru_title": (data.get("ru_title") or article.title)[: get_runtime_int("max_title_chars", default=130)],
        "ru_summary": (data.get("ru_summary") or article.subtitle or article.text[:300])[: get_runtime_int("max_summary_chars", default=1400)],
        "short_hook": (data.get("short_hook") or article.title)[:100],
    }
    _cache_set(cache_key, out)
    return out


def _enforce_temporal_consistency(article: Article, rewrite: dict, extraction: dict) -> dict:
    cache_key = _cache_key("temporal_guard", article, {"rewrite": rewrite, "extraction": extraction})
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not settings.openrouter_api_key:
        return rewrite
    ru_title = str(rewrite.get("ru_title") or article.title).strip()
    ru_summary = str(rewrite.get("ru_summary") or article.subtitle or "").strip()
    short_hook = str(rewrite.get("short_hook") or "").strip()
    if not ru_title or not ru_summary:
        return rewrite
    if not llm_budget_allows("content.temporal_guard", feature="content"):
        return rewrite

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    published_at = article.published_at.isoformat() if article.published_at else ""
    prompt = f"""
Проверь временную согласованность текста и поправь только при необходимости.
Не меняй смысл и факты.

Контекст времени:
- Сейчас: {now_utc}
- Published_at статьи: {published_at or "unknown"}

Правила:
1) Будущие/планируемые события нельзя описывать как уже случившиеся.
2) Если событие уже произошло к published_at, оставляй прошедшее время.
3) Не добавляй факты и даты, которых нет в источнике.
4) Сохрани стиль и структуру, правь минимально.

Верни JSON only:
{{"ru_title":"...", "ru_summary":"...", "short_hook":"..."}}

Extraction:
{json.dumps(extraction, ensure_ascii=False)}

Candidate:
ru_title: {ru_title}
ru_summary: {ru_summary}
short_hook: {short_hook}
""".strip()
    client = get_client()
    try:
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Ты редактор-проверяющий факты и время. Исправляй только temporal-ошибки."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        track_usage_from_response(resp, operation="content.temporal_guard", model=settings.llm_text_model, kind="chat")
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        out = {
            "ru_title": (data.get("ru_title") or ru_title)[: get_runtime_int("max_title_chars", default=130)],
            "ru_summary": (data.get("ru_summary") or ru_summary)[: get_runtime_int("max_summary_chars", default=1400)],
            "short_hook": (data.get("short_hook") or short_hook or ru_title)[:100],
        }
        _cache_set(cache_key, out)
        return out
    except Exception:
        return rewrite


def translate_article_text(article_id: int) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return {"ok": False, "error": "article_not_found"}

    src = (article.text or article.subtitle or "").strip()
    if not src:
        return {"ok": False, "error": "empty_article_text"}

    if not settings.openrouter_api_key:
        return {
            "ok": True,
            "ru_title": article.title,
            "ru_translation": src[:6000],
        }
    cache_key = _cache_key("translate_preview", article)
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"ok": True, **cached}
    if not llm_budget_allows("content.translate_preview", feature="content"):
        return {"ok": True, "ru_title": article.title, "ru_translation": src[:6000]}

    prompt = f"""
Переведи текст на русский язык точно и нейтрально.
Сохраняй факты, цифры, имена, названия компаний и моделей.
Не добавляй интерпретации и оценку. Не сокращай смысл.

Верни JSON:
{{"ru_title":"...", "ru_translation":"..."}}

Title: {article.title}
Subtitle: {article.subtitle}
Text: {src[:12000]}
""".strip()

    client = get_client()
    try:
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Ты технический переводчик."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        track_usage_from_response(resp, operation="content.translate_preview", model=settings.llm_text_model, kind="chat")
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        out = {
            "ok": True,
            "ru_title": (data.get("ru_title") or article.title)[: get_runtime_int("max_title_chars", default=130)],
            "ru_translation": (data.get("ru_translation") or src)[:14000],
        }
        _cache_set(cache_key, {"ru_title": out["ru_title"], "ru_translation": out["ru_translation"]})
        return out
    except Exception:
        return {
            "ok": True,
            "ru_title": article.title,
            "ru_translation": src[:6000],
        }


def translate_article_full_style(article_id: int) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return {"ok": False, "error": "article_not_found"}

    src = (article.text or article.subtitle or "").strip()
    if not src:
        return {"ok": False, "error": "empty_article_text"}

    if not settings.openrouter_api_key:
        return {"ok": True, "ru_title": article.title, "ru_translation": src}
    cache_key = _cache_key("translate_full", article)
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"ok": True, **cached}
    if not llm_budget_allows("content.translate_full", feature="content"):
        return {"ok": True, "ru_title": article.title, "ru_translation": src}

    style_prompt = """
Ты — редактор технологического AI-канала.
Сделай полный перевод на русский язык без сокращений.
Стиль: чистый, структурный, деловой, без эмоций и маркетинговых оборотов.
Сохраняй все факты, цифры, имена компаний/моделей и терминологию ИИ.
Не добавляй информацию от себя. Не убирай существенные части текста.
""".strip()

    parts = _chunk_text(src, chunk_size=5000)
    translated_parts: list[str] = []
    client = get_client()
    for idx, part in enumerate(parts):
        prompt = f"""
{style_prompt}
Переведи фрагмент {idx + 1}/{len(parts)}.
Верни только перевод текста, без комментариев.

Title: {article.title}
Fragment:
{part}
""".strip()
        try:
            resp = client.chat.completions.create(
                model=settings.llm_text_model,
                messages=[
                    {"role": "system", "content": "Ты технический редактор-переводчик."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            track_usage_from_response(resp, operation="content.translate_full_chunk", model=settings.llm_text_model, kind="chat")
            translated_parts.append((resp.choices[0].message.content or "").strip())
        except Exception:
            translated_parts.append(part)

    ru_title = article.title
    try:
        title_resp = client.chat.completions.create(
            model=settings.llm_text_model,
            messages=[
                {"role": "system", "content": "Ты технический переводчик. Переведи заголовок на русский точно и кратко."},
                {"role": "user", "content": f"Переведи на русский заголовок (верни только перевод): {article.title}"},
            ],
            temperature=0.1,
        )
        track_usage_from_response(title_resp, operation="content.translate_full_title", model=settings.llm_text_model, kind="chat")
        maybe_title = (title_resp.choices[0].message.content or "").strip()
        if maybe_title:
            ru_title = maybe_title
    except Exception:
        pass

    out = {
        "ok": True,
        "ru_title": ru_title[: get_runtime_int("max_title_chars", default=130)],
        "ru_translation": "\n\n".join([p for p in translated_parts if p]),
    }
    _cache_set(cache_key, {"ru_title": out["ru_title"], "ru_translation": out["ru_translation"]})
    return out


def _chunk_text(text: str, chunk_size: int = 5000) -> list[str]:
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            split = text.rfind("\n", start, end)
            if split <= start:
                split = text.rfind(". ", start, end)
            if split > start:
                end = split + 1
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]


def _quality_checks(rewrite: dict) -> dict:
    banned = [x.strip().lower() for x in get_runtime_csv_list("banned_phrases_csv")]
    text = f"{rewrite.get('ru_title', '')}\n{rewrite.get('ru_summary', '')}".lower()
    banned_hits = [b for b in banned if b in text]

    date_or_number_count = len(re.findall(r"\d", rewrite.get("ru_summary", "")))
    too_long = len(rewrite.get("ru_summary", "")) > get_runtime_int("max_summary_chars", default=1400)
    empty = not rewrite.get("ru_summary") or not rewrite.get("ru_title")

    return {
        "is_valid": (not empty) and (not too_long) and (not banned_hits),
        "banned_hits": banned_hits,
        "numeric_signal": date_or_number_count,
        "summary_len": len(rewrite.get("ru_summary", "")),
        "title_len": len(rewrite.get("ru_title", "")),
    }


def _factual_consistency_checks(article: Article, extraction: dict, rewrite: dict) -> dict:
    """
    Lightweight anti-hallucination guard:
    - rewrite numbers should be sourced from extraction/article text
    - URL in rewrite must match article canonical URL if present
    """
    summary = str(rewrite.get("ru_summary") or "")
    src_pool = " ".join(
        [
            str(article.title or ""),
            str(article.subtitle or ""),
            str(article.text or ""),
            " ".join(str(x) for x in (extraction.get("numbers") or [])),
            " ".join(str(x) for x in (extraction.get("dates") or [])),
            " ".join(str(x) for x in (extraction.get("claims") or [])),
        ]
    )
    src_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", src_pool))
    out_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", summary))
    suspicious_numbers = sorted(x for x in out_numbers if x not in src_numbers)

    links = re.findall(r"https?://\S+", summary)
    canonical = str(article.canonical_url or "").strip()
    bad_links = [u for u in links if canonical and canonical not in u]

    return {
        "is_valid": not suspicious_numbers and not bad_links,
        "suspicious_numbers": suspicious_numbers[:10],
        "bad_links": bad_links[:5],
    }


def _ensure_key_takeaways_block(article: Article, rewrite: dict, extraction: dict) -> dict:
    """
    Conservative mode: do not auto-inject synthetic "Ключевое" bullets.
    LLM output should stay factual and avoid generic API/metrics add-ons.
    """
    return rewrite


def _safe_fallback_summary(article: Article, extraction: dict) -> str:
    pts = extraction.get("key_points") or []
    if isinstance(pts, list) and pts:
        first = " ".join(str(x) for x in pts[:2])
        second = "Подробнее: " + article.canonical_url
        return f"{first}\n\n{second}"[: get_runtime_int("max_summary_chars", default=1400)]
    return (article.subtitle or article.text[:400])[: get_runtime_int("max_summary_chars", default=1400)]


def generate_image_prompt(article_id: int) -> str:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return ""
        title = article.ru_title or article.title
        summary = article.ru_summary or article.subtitle or article.text[:700]

    if not settings.openrouter_api_key:
        return _image_prompt_scaffold(
            scene=f"Key news scene about: {title[:180]}. Story context: {summary[:280]}",
            mood="calm, technological, cinematic, serious",
            style="realistic editorial illustration, clean composition",
            camera="wide shot, clear focal subject, balanced negative space",
            lighting="soft key light, subtle rim light, moderate contrast",
            color_palette="deep blue, steel gray, neutral highlights",
            constraints="no logos, no watermark, no readable text, no brand names on screen",
        )
    cache_key = _cache_key("generate_image_prompt", article, {"title": title, "summary": summary})
    cached = _cache_get(cache_key)
    if cached is not None and cached.get("prompt"):
        return str(cached.get("prompt"))
    if not llm_budget_allows("content.generate_image_prompt", feature="image"):
        return _image_prompt_scaffold(
            scene=f"Key news scene about: {title[:180]}. Story context: {summary[:280]}",
            mood="calm, technological, cinematic, serious",
            style="realistic editorial illustration, clean composition",
            camera="wide shot, clear focal subject, balanced negative space",
            lighting="soft key light, subtle rim light, moderate contrast",
            color_palette="deep blue, steel gray, neutral highlights",
            constraints="no logos, no watermark, no readable text, no brand names on screen",
        )

    client = get_client()
    prompt = f"""
Create image prompt for a Telegram AI-news post.
You must return ONLY this exact 7-line template (same labels, one line each):

Generate a horizontal 16:9 editorial image for this story.
Scene: ...
Mood: ...
Style: ...
Camera: ...
Lighting: ...
Color palette: ...
Constraints: ...

Rules:
- Ground strictly in this story.
- No generic unrelated scene.
- No logos, no watermark, no readable text in image.
- Keep it concise and production-ready.

Story context:
Title: {title}
Short description: {summary}
Article excerpt: {(article.text or "")[:2600]}
Source URL: {article.canonical_url}
""".strip()
    try:
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an art director for AI news visuals. "
                        "Generate prompts that are tightly grounded in the provided story. "
                        "Do not reuse canned scenarios unless the story clearly requires them."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        track_usage_from_response(resp, operation="content.generate_image_prompt", model=settings.llm_text_model, kind="chat")
        out = (resp.choices[0].message.content or "").strip()
        # Enforce stable template if model drifts.
        parsed = _parse_image_prompt_lines(out)
        out = _image_prompt_scaffold(
            scene=parsed.get("scene") or f"Key scene about: {title[:180]}",
            mood=parsed.get("mood") or "calm, technological, cinematic, serious",
            style=parsed.get("style") or "realistic editorial illustration, clean composition",
            camera=parsed.get("camera") or "wide shot, clear focal subject, balanced negative space",
            lighting=parsed.get("lighting") or "soft key light, subtle rim light, moderate contrast",
            color_palette=parsed.get("color_palette") or "deep blue, steel gray, neutral highlights",
            constraints=parsed.get("constraints") or "no logos, no watermark, no readable text, no brand names on screen",
        )[:1500]
        _cache_set(cache_key, {"prompt": out})
        return out
    except Exception:
        return _image_prompt_scaffold(
            scene=f"Key news scene about: {title[:180]}. Story context: {summary[:280]}",
            mood="calm, technological, cinematic, serious",
            style="realistic editorial illustration, clean composition",
            camera="wide shot, clear focal subject, balanced negative space",
            lighting="soft key light, subtle rim light, moderate contrast",
            color_palette="deep blue, steel gray, neutral highlights",
            constraints="no logos, no watermark, no readable text, no brand names on screen",
        )


def _parse_image_prompt_lines(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for line in lines:
        low = line.lower()
        if low.startswith("scene:"):
            result["scene"] = line.split(":", 1)[1].strip()
        elif low.startswith("mood:"):
            result["mood"] = line.split(":", 1)[1].strip()
        elif low.startswith("style:"):
            result["style"] = line.split(":", 1)[1].strip()
        elif low.startswith("camera:"):
            result["camera"] = line.split(":", 1)[1].strip()
        elif low.startswith("lighting:"):
            result["lighting"] = line.split(":", 1)[1].strip()
        elif low.startswith("color palette:"):
            result["color_palette"] = line.split(":", 1)[1].strip()
        elif low.startswith("constraints:"):
            result["constraints"] = line.split(":", 1)[1].strip()
    return result


def _image_prompt_scaffold(
    scene: str,
    mood: str,
    style: str,
    camera: str,
    lighting: str,
    color_palette: str,
    constraints: str,
) -> str:
    return (
        "Generate a horizontal 16:9 editorial image for this story.\n"
        f"Scene: {scene}\n"
        f"Mood: {mood}\n"
        f"Style: {style}\n"
        f"Camera: {camera}\n"
        f"Lighting: {lighting}\n"
        f"Color palette: {color_palette}\n"
        f"Constraints: {constraints}"
    )


def generate_image_card(article_id: int, image_prompt: str | None = None) -> str | None:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return None

        title = article.ru_title or article.title
        hook = article.short_hook or "AI News"
        score = session.get(Score, article_id)

    ai_prompt = None
    high_impact = False
    if score and (score.scale >= 8 or score.significance >= 8):
        high_impact = True
        ai_prompt = _build_image_prompt(title, hook)

    final_prompt = (image_prompt or ai_prompt or "").strip()
    if final_prompt:
        ai_image_path = _generate_ai_landscape_image(article_id, final_prompt)
        if ai_image_path:
            return ai_image_path
    return None


def _generate_ai_landscape_image(article_id: int, prompt: str) -> str | None:
    if not settings.openrouter_api_key:
        return None
    client = get_client()
    out_dir = Path("app/static/generated")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"article_{article_id}_ai_landscape.png"

    try:
        resp = client.images.generate(
            model=settings.llm_image_model,
            prompt=prompt,
            size="1536x1024",
        )
        data = (resp.data or [None])[0]
        if not data:
            return None

        b64 = getattr(data, "b64_json", None)
        if b64:
            out_path.write_bytes(base64.b64decode(b64))
        else:
            url = getattr(data, "url", None)
            if not url:
                return None
            image_bytes = httpx.get(url, timeout=60.0).content
            if not image_bytes:
                return None
            out_path.write_bytes(image_bytes)
    except Exception:
        return None

    object_name = f"articles/{article_id}/{out_path.name}"
    uploaded = upload_generated_image(str(out_path), object_name=object_name)
    final_path = uploaded or str(out_path)

    with session_scope() as session:
        article = session.get(Article, article_id)
        if article:
            article.generated_image_path = final_path
            version_no = int(
                session.scalar(select(func.coalesce(func.max(ContentVersion.version_no), 0)).where(ContentVersion.article_id == article_id))
                or 0
            ) + 1
            session.add(
                ContentVersion(
                    article_id=article_id,
                    version_no=version_no,
                    ru_title=article.ru_title or article.title,
                    ru_summary=article.ru_summary or article.subtitle,
                    short_hook=article.short_hook or "",
                    extraction_json=None,
                    quality_report={"generator": "ai_image", "size": "1536x1024"},
                    image_path=final_path,
                    image_prompt=prompt,
                    selected_by_editor=False,
                )
            )

    return final_path


def _build_image_prompt(title: str, hook: str) -> str:
    if not settings.openrouter_api_key:
        return ""
    client = get_client()
    prompt = f"Create concise image prompt for social card. Title: {title}. Hook: {hook}."
    try:
        resp = client.chat.completions.create(
            model=settings.llm_image_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        track_usage_from_response(resp, operation="content.build_image_prompt", model=settings.llm_image_model, kind="image")
        return (resp.choices[0].message.content or "")[:1200]
    except Exception:
        return ""


def _brand_style(high_impact: bool) -> dict:
    if high_impact:
        return {
            "name": "impact",
            "bg": (20, 20, 28),
            "band": (162, 35, 38),
            "brand_text": (255, 238, 230),
            "hook_text": (255, 201, 186),
            "title_text": (255, 255, 255),
        }
    return {
        "name": "default",
        "bg": (14, 24, 40),
        "band": (32, 64, 128),
        "brand_text": (240, 240, 255),
        "hook_text": (180, 220, 255),
        "title_text": (255, 255, 255),
    }


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        trial = " ".join(cur + [w])
        if len(trial) <= width:
            cur.append(w)
        else:
            if cur:
                lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines
