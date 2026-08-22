"""RAG 专项评估数据结构与加载校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RagEvalCase:
    """一条检索问题及其相关 Chunk、回答关键事实标注。"""

    id: str
    query: str
    relevant_chunk_ids: list[str]
    expected_keywords: list[str | list[str]] = field(default_factory=list)
    category: str = "general"


def load_rag_dataset(path: str | Path) -> list[RagEvalCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [RagEvalCase(**item) for item in data["cases"]]

    ids = [case.id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise ValueError(f"RAG 评估用例 id 重复: {', '.join(duplicates)}")

    for case in cases:
        if not case.id.strip() or not case.query.strip():
            raise ValueError(f"RAG 用例缺少有效 id/query: {case.id!r}")
        if not case.relevant_chunk_ids:
            raise ValueError(f"RAG 用例未标注相关 Chunk: {case.id}")
        if len(case.relevant_chunk_ids) != len(set(case.relevant_chunk_ids)):
            raise ValueError(f"RAG 用例包含重复相关 Chunk: {case.id}")

    return cases
