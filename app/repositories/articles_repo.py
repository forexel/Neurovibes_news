from __future__ import annotations

from sqlalchemy import func, select

from app.models import ArticlePreview, Score, Source


def apply_preview_sort(query, *, sort_by: str, sort_dir: str):
    if sort_by == "score":
        query = query.join(Score, Score.article_id == ArticlePreview.id, isouter=True)
        order_col = Score.final_score
    elif sort_by == "source":
        query = query.join(Source, Source.id == ArticlePreview.source_id, isouter=True)
        order_col = Source.name
    elif sort_by in {"published_at", "published", "date"}:
        order_col = func.coalesce(ArticlePreview.published_at, ArticlePreview.created_at)
    else:
        order_col = ArticlePreview.created_at

    reverse = str(sort_dir or "desc").lower() != "asc"
    if reverse:
        return query.order_by(order_col.desc().nullslast(), ArticlePreview.id.desc())
    return query.order_by(order_col.asc().nullslast(), ArticlePreview.id.asc())


def count_from_query(session, query) -> int:
    return int(session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)


def fetch_preview_page(session, query, *, page: int, page_size: int, include_total: bool) -> tuple[list, int]:
    offset_value = max(page - 1, 0) * page_size
    rows = session.scalars(query.offset(offset_value).limit(page_size)).all()
    total = count_from_query(session, query) if include_total else len(rows)
    return rows, int(total)
