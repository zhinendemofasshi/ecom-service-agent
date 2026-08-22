"""RAG 检索层确定性指标。"""

from __future__ import annotations


def recall_at_k(
    relevant_chunk_ids: list[str], ranked_chunk_ids: list[str], k: int
) -> float:
    """Top-K 中召回的相关 Chunk 数 / 全部相关 Chunk 数。"""
    if not relevant_chunk_ids:
        raise ValueError("relevant_chunk_ids 不能为空")
    if k <= 0:
        raise ValueError("k 必须大于 0")
    relevant = set(relevant_chunk_ids)
    retrieved = set(ranked_chunk_ids[:k])
    return len(relevant.intersection(retrieved)) / len(relevant)


def reciprocal_rank(
    relevant_chunk_ids: list[str], ranked_chunk_ids: list[str]
) -> float:
    """首个相关 Chunk 的排名倒数；未召回返回 0。"""
    if not relevant_chunk_ids:
        raise ValueError("relevant_chunk_ids 不能为空")
    relevant = set(relevant_chunk_ids)
    for rank, chunk_id in enumerate(ranked_chunk_ids, start=1):
        if chunk_id in relevant:
            return 1.0 / rank
    return 0.0


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
