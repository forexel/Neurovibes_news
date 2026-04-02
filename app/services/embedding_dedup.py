from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

from sqlalchemy import and_, select

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleEmbedding, ArticleStatus, Source
from app.services.llm import get_client, track_usage_from_response
from app.services.utils import stable_hash


EMBEDDING_SIZE = 1536
_TITLE_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "for", "with", "of", "in", "on", "at", "by",
    "is", "are", "as", "after", "into", "from", "over", "under", "new", "latest",
    "says", "say", "will", "would", "could", "should", "that", "this",
}


def _embed_text(text: str) -> list[float] | None:
    if not settings.openrouter_api_key:
        return None
    client = get_client()
    resp = client.embeddings.create(model=settings.embedding_model, input=text[:8000])
    track_usage_from_response(resp, operation="embedding.dedup", model=settings.embedding_model, kind="embedding")
    vec = resp.data[0].embedding
    if len(vec) != EMBEDDING_SIZE:
        vec = (vec + [0.0] * EMBEDDING_SIZE)[:EMBEDDING_SIZE]
    return vec


def _article_embed_payload(article: Article) -> str:
    return f"{article.title}\n{article.subtitle}\n{article.text[:1500]}"


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)


def _title_tokens(title: str) -> set[str]:
    raw = re.findall(r"[a-zA-Z0-9]{3,}", (title or "").lower())
    out = []
    for tok in raw:
        if tok in _TITLE_STOPWORDS:
            continue
        if tok.endswith("s") and len(tok) > 4:
            tok = tok[:-1]
        if tok.endswith("ing") and len(tok) > 6:
            tok = tok[:-3]
        out.append(tok)
    return set(out)


def _title_overlap_ratio(left: str, right: str) -> float:
    lt = _title_tokens(left)
    rt = _title_tokens(right)
    if not lt or not rt:
        return 0.0
    inter = len(lt & rt)
    base = max(1, min(len(lt), len(rt)))
    return float(inter / base)


def process_embeddings_and_dedup(limit: int = 200) -> int:
    processed = 0
    window_start = datetime.utcnow() - timedelta(hours=72)

    with session_scope() as session:
        # Repair inconsistent state: DOUBLE without a pointer can't be handled by UI/pipeline.
        # Put it back to INBOX so it can be re-embedded and re-deduped.
        orphans = session.scalars(
            select(Article)
            .where(Article.status == ArticleStatus.DOUBLE, Article.double_of_article_id.is_(None))
            .order_by(Article.created_at.desc())
            .limit(500)
        ).all()
        for a in orphans:
            a.status = ArticleStatus.INBOX
            a.updated_at = datetime.utcnow()

        articles = session.scalars(
            select(Article)
            .where(Article.status.in_([ArticleStatus.NEW, ArticleStatus.INBOX]))
            .order_by(Article.created_at.asc())
            .limit(limit)
        ).all()

        for article in articles:
            payload = _article_embed_payload(article)
            embedding = _embed_text(payload)
            if embedding is None:
                if not article.cluster_key:
                    article.cluster_key = stable_hash(article.canonical_url)
                processed += 1
                continue

            existing_emb = session.get(ArticleEmbedding, article.id)
            if existing_emb is None:
                session.add(ArticleEmbedding(article_id=article.id, embedding=embedding))

            article_source = session.get(Source, article.source_id)
            if article_source is None:
                article.status = ArticleStatus.ARCHIVED
                processed += 1
                continue

            candidate_rows = session.execute(
                select(ArticleEmbedding, Article, Source)
                .join(Article, ArticleEmbedding.article_id == Article.id)
                .join(Source, Article.source_id == Source.id)
                .where(
                    and_(
                        Article.id != article.id,
                        Article.created_at >= window_start,
                        Article.status.in_(
                            [
                                ArticleStatus.NEW,
                                ArticleStatus.INBOX,
                                ArticleStatus.SCORED,
                                ArticleStatus.READY,
                            ]
                        ),
                    )
                )
                .order_by(ArticleEmbedding.embedding.cosine_distance(embedding))
                .limit(5)
            ).all()

            if candidate_rows:
                best_row = None
                best_similarity = 0.0
                best_overlap = 0.0
                for candidate_emb, candidate_article, candidate_source in candidate_rows:
                    similarity = _cosine_similarity(embedding, candidate_emb.embedding)
                    overlap = _title_overlap_ratio(article.title or "", candidate_article.title or "")
                    if similarity > best_similarity or (math.isclose(similarity, best_similarity) and overlap > best_overlap):
                        best_row = (candidate_emb, candidate_article, candidate_source)
                        best_similarity = similarity
                        best_overlap = overlap

                if best_row is not None and (
                    best_similarity >= settings.dedup_similarity_threshold
                    or (best_similarity >= max(0.78, settings.dedup_similarity_threshold - 0.08) and best_overlap >= 0.60)
                    or best_overlap >= 0.75
                ):
                    candidate_emb, candidate_article, candidate_source = best_row
                    if article_source.priority_rank > candidate_source.priority_rank:
                        article.status = ArticleStatus.DOUBLE
                        article.double_of_article_id = candidate_article.id
                        article.cluster_key = candidate_article.cluster_key or stable_hash(candidate_article.canonical_url)
                    else:
                        candidate_article.status = ArticleStatus.DOUBLE
                        candidate_article.double_of_article_id = article.id
                        article.cluster_key = candidate_article.cluster_key or stable_hash(candidate_article.canonical_url)

            if not article.cluster_key:
                article.cluster_key = stable_hash(article.canonical_url)

            processed += 1

    return processed
