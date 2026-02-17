from __future__ import annotations

import json
import re
import base64
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func, select

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleStatus, ContentVersion, Score
from app.services.llm import get_client, track_usage_from_response
from app.services.object_storage import upload_generated_image
from app.services.runtime_settings import get_runtime_csv_list, get_runtime_int


def generate_ru_summary(article_id: int) -> bool:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return False

    extraction = _extract_facts(article)
    rewrite = _rewrite_ru(article, extraction)
    quality = _quality_checks(rewrite)

    if not quality["is_valid"]:
        rewrite["ru_summary"] = _safe_fallback_summary(article, extraction)
        quality = _quality_checks(rewrite)

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
    if not settings.openrouter_api_key:
        return {
            "key_points": [article.subtitle[:140]],
            "dates": [],
            "numbers": [],
            "entities": [],
            "claims": [article.title],
        }

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
        return json.loads(raw)
    except Exception:
        return {"key_points": [article.subtitle[:140]], "dates": [], "numbers": [], "entities": [], "claims": [article.title]}


def _rewrite_ru(article: Article, extraction: dict) -> dict:
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
2. Второй абзац — что это меняет и почему это важно.
3. Если уместно — краткий контекст (рынок, конкуренция, стратегический сдвиг).

Важно:
— Сохраняй факты, цифры, названия моделей и компаний.
— Не добавляй информацию, которой нет в тексте.
— Не домысливай.
— Не используй сложные обороты и длинные конструкции.
— Не используй англицизмы, если есть точный русский аналог.
— Термины ИИ оставляй корректно (LLM, AGI, fine-tuning и т.п.).

Длина:
— 2 абзаца.
— 700–1200 символов.

В конце не добавляй выводов «И это только начало».
Никаких эмодзи.
""".strip()

    if not settings.openrouter_api_key:
        return {
            "ru_title": article.title,
            "ru_summary": (article.subtitle or article.text[:300])[: get_runtime_int("max_summary_chars", default=1400)],
            "short_hook": (article.subtitle or article.title)[:100],
        }

    prompt = f"""
{style_guide}
Используй только факты из extraction.
Верни JSON only:
{{"ru_title":"...", "ru_summary":"...", "short_hook":"..."}}

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

    return {
        "ru_title": (data.get("ru_title") or article.title)[: get_runtime_int("max_title_chars", default=130)],
        "ru_summary": (data.get("ru_summary") or article.subtitle or article.text[:300])[: get_runtime_int("max_summary_chars", default=1400)],
        "short_hook": (data.get("short_hook") or article.title)[:100],
    }


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
        return {
            "ok": True,
            "ru_title": (data.get("ru_title") or article.title)[: get_runtime_int("max_title_chars", default=130)],
            "ru_translation": (data.get("ru_translation") or src)[:14000],
        }
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

    return {
        "ok": True,
        "ru_title": ru_title[: get_runtime_int("max_title_chars", default=130)],
        "ru_translation": "\n\n".join([p for p in translated_parts if p]),
    }


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
        return _image_prompt_scaffold(
            scene=parsed.get("scene") or f"Key scene about: {title[:180]}",
            mood=parsed.get("mood") or "calm, technological, cinematic, serious",
            style=parsed.get("style") or "realistic editorial illustration, clean composition",
            camera=parsed.get("camera") or "wide shot, clear focal subject, balanced negative space",
            lighting=parsed.get("lighting") or "soft key light, subtle rim light, moderate contrast",
            color_palette=parsed.get("color_palette") or "deep blue, steel gray, neutral highlights",
            constraints=parsed.get("constraints") or "no logos, no watermark, no readable text, no brand names on screen",
        )[:1500]
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
