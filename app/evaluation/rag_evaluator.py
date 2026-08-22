"""RAG 专项评估：检索排序、回答完整性与上下文忠实度。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from openai import OpenAI

from app.config.model_compat import chat_completion_options
from app.evaluation.metrics import estimated_cost_usd, keyword_coverage
from app.evaluation.rag_dataset import RagEvalCase
from app.evaluation.rag_metrics import mean, recall_at_k, reciprocal_rank
from app.prompts.evaluation import RAG_ANSWER_PROMPT, RAG_FAITHFULNESS_PROMPT


@dataclass
class RagCaseResult:
    case_id: str
    query: str
    category: str
    relevant_chunk_ids: list[str]
    ranked_chunk_ids: list[str] = field(default_factory=list)
    retrieved_chunks: list[dict] = field(default_factory=list)
    recall_at_k: dict[str, float] = field(default_factory=dict)
    reciprocal_rank: float = 0.0
    answer: str | None = None
    answer_keyword_coverage: float | None = None
    faithfulness: float | None = None
    unsupported_claims: list[str] = field(default_factory=list)
    faithfulness_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    answer_prompt_tokens: int = 0
    answer_completion_tokens: int = 0
    judge_prompt_tokens: int = 0
    judge_completion_tokens: int = 0
    estimated_answer_cost_usd: float = 0.0
    estimated_judge_cost_usd: float | None = None
    estimated_cost_usd: float | None = 0.0
    retrieval_latency_ms: float = 0.0
    answer_latency_ms: float = 0.0
    judge_latency_ms: float = 0.0
    end_to_end_latency_ms: float = 0.0
    error: str | None = None


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


class RagEvaluator:
    """对固定 RAG 数据集进行可复现的检索与生成评估。"""

    def __init__(
        self,
        retriever,
        client: OpenAI | None,
        model: str,
        judge_model: str | None = None,
        k_values: list[int] | None = None,
        use_judge: bool = True,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        judge_input_cost_per_million: float | None = None,
        judge_output_cost_per_million: float | None = None,
    ):
        values = sorted(set(k_values or [1, 3, 5]))
        if not values or any(k <= 0 for k in values):
            raise ValueError("k_values 必须是非空正整数列表")
        if use_judge and client is None:
            raise ValueError("启用回答/Faithfulness 评估时必须提供 OpenAI client")

        self.retriever = retriever
        self.client = client
        self.model = model
        self.judge_model = judge_model or model
        self.k_values = values
        self.use_judge = use_judge
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        if self.judge_model == self.model:
            self.judge_input_cost_per_million = (
                input_cost_per_million
                if judge_input_cost_per_million is None
                else judge_input_cost_per_million
            )
            self.judge_output_cost_per_million = (
                output_cost_per_million
                if judge_output_cost_per_million is None
                else judge_output_cost_per_million
            )
        else:
            self.judge_input_cost_per_million = judge_input_cost_per_million
            self.judge_output_cost_per_million = judge_output_cost_per_million

    def _chat(self, prompt: str, model: str) -> tuple[str, int, int, float]:
        started = time.perf_counter()
        response = self.client.chat.completions.create(
            model=model,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
            **chat_completion_options(model),
        )
        latency_ms = (time.perf_counter() - started) * 1000
        usage = response.usage
        return (
            response.choices[0].message.content or "",
            int(usage.prompt_tokens or 0) if usage else 0,
            int(usage.completion_tokens or 0) if usage else 0,
            latency_ms,
        )

    @staticmethod
    def _contexts_text(retrieved: list) -> str:
        return "\n\n".join(
            f"[上下文 {rank}｜{item.chunk.chunk_id}]\n{item.chunk.text}"
            for rank, item in enumerate(retrieved, start=1)
        )

    def run_case(self, case: RagEvalCase) -> RagCaseResult:
        result = RagCaseResult(
            case_id=case.id,
            query=case.query,
            category=case.category,
            relevant_chunk_ids=list(case.relevant_chunk_ids),
        )
        case_started = time.perf_counter()
        try:
            # 全库排序后计算 MRR；Recall@K 仍严格截断到各自 K。
            retrieval_started = time.perf_counter()
            ranked = self.retriever.search(case.query, top_k=self.retriever.size)
            result.retrieval_latency_ms = (
                time.perf_counter() - retrieval_started
            ) * 1000
            result.ranked_chunk_ids = [item.chunk.chunk_id for item in ranked]
            result.retrieved_chunks = [
                {
                    "rank": rank,
                    "chunk_id": item.chunk.chunk_id,
                    "doc": item.chunk.doc,
                    "section": item.chunk.section,
                    "score": item.score,
                }
                for rank, item in enumerate(ranked, start=1)
            ]
            result.recall_at_k = {
                str(k): recall_at_k(
                    case.relevant_chunk_ids, result.ranked_chunk_ids, k
                )
                for k in self.k_values
            }
            result.reciprocal_rank = reciprocal_rank(
                case.relevant_chunk_ids, result.ranked_chunk_ids
            )

            if self.use_judge:
                context_k = max(self.k_values)
                contexts = self._contexts_text(ranked[:context_k])
                answer_prompt = RAG_ANSWER_PROMPT.format(
                    query=case.query,
                    contexts=contexts,
                )
                answer, p_tokens, c_tokens, answer_latency = self._chat(
                    answer_prompt, self.model
                )
                result.answer = answer.strip()
                result.prompt_tokens += p_tokens
                result.completion_tokens += c_tokens
                result.answer_prompt_tokens = p_tokens
                result.answer_completion_tokens = c_tokens
                result.answer_latency_ms = answer_latency
                result.estimated_answer_cost_usd = estimated_cost_usd(
                    p_tokens,
                    c_tokens,
                    self.input_cost_per_million,
                    self.output_cost_per_million,
                )
                if (
                    self.judge_input_cost_per_million is None
                    or self.judge_output_cost_per_million is None
                ):
                    result.estimated_cost_usd = None
                result.answer_keyword_coverage = keyword_coverage(
                    case.expected_keywords, result.answer
                )

                judge_prompt = RAG_FAITHFULNESS_PROMPT.format(
                    query=case.query,
                    contexts=contexts,
                    answer=result.answer,
                )
                raw, p_tokens, c_tokens, judge_latency = self._chat(
                    judge_prompt, self.judge_model
                )
                result.prompt_tokens += p_tokens
                result.completion_tokens += c_tokens
                result.judge_prompt_tokens = p_tokens
                result.judge_completion_tokens = c_tokens
                result.judge_latency_ms = judge_latency
                if (
                    self.judge_input_cost_per_million is not None
                    and self.judge_output_cost_per_million is not None
                ):
                    result.estimated_judge_cost_usd = estimated_cost_usd(
                        p_tokens,
                        c_tokens,
                        self.judge_input_cost_per_million,
                        self.judge_output_cost_per_million,
                    )
                    result.estimated_cost_usd = (
                        result.estimated_answer_cost_usd
                        + result.estimated_judge_cost_usd
                    )
                else:
                    result.estimated_cost_usd = None
                verdict = _parse_json(raw)
                faithful = verdict.get("faithful")
                if not isinstance(faithful, bool):
                    raise ValueError("Faithfulness judge 的 faithful 字段不是布尔值")
                unsupported = verdict.get("unsupported_claims", [])
                if not isinstance(unsupported, list):
                    raise ValueError(
                        "Faithfulness judge 的 unsupported_claims 字段不是列表"
                    )
                result.faithfulness = 1.0 if faithful else 0.0
                result.unsupported_claims = [str(item) for item in unsupported]
                result.faithfulness_reason = str(verdict.get("reason", ""))
        except Exception as exc:  # noqa: BLE001 - 单条错误不应中断整批评估
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.end_to_end_latency_ms = (time.perf_counter() - case_started) * 1000
        return result

    def run_all(
        self,
        cases: list[RagEvalCase],
        progress: Callable[[int, int, RagCaseResult], None] | None = None,
    ) -> dict:
        results: list[RagCaseResult] = []
        for index, case in enumerate(cases, start=1):
            result = self.run_case(case)
            results.append(result)
            if progress:
                progress(index, len(cases), result)
        return self._aggregate(results)

    def _aggregate(self, results: list[RagCaseResult]) -> dict:
        valid_retrieval = [item for item in results if item.ranked_chunk_ids]
        judged = [item for item in results if item.faithfulness is not None]
        answered = [item for item in results if item.answer is not None]
        covered = [
            item for item in results if item.answer_keyword_coverage is not None
        ]
        recall_summary = {
            str(k): mean([item.recall_at_k[str(k)] for item in valid_retrieval])
            for k in self.k_values
        }
        full_recall_rate = {
            str(k): mean([
                1.0 if item.recall_at_k[str(k)] == 1.0 else 0.0
                for item in valid_retrieval
            ])
            for k in self.k_values
        }
        categories = sorted({item.category for item in results})

        return {
            "metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "answer_model": self.model,
                "judge_model": self.judge_model if self.use_judge else None,
                "embedding_model": getattr(
                    getattr(self.retriever, "_embedder", None), "model", None
                ),
                "backend": type(self.retriever.backend).__name__,
                "judge_enabled": self.use_judge,
                "k_values": self.k_values,
                "answer_context_top_k": max(self.k_values),
                "ranking_depth": self.retriever.size,
                "cost_scope": (
                    "answer generation and faithfulness judge chat completions; "
                    "query embeddings excluded; total is null when judge pricing is not configured"
                ),
            },
            "summary": {
                "total": len(results),
                "retrieval_evaluated": len(valid_retrieval),
                "errors": sum(item.error is not None for item in results),
                "recall_at_k": recall_summary,
                "full_recall_rate_at_k": full_recall_rate,
                "mrr": mean([item.reciprocal_rank for item in valid_retrieval]),
                "faithfulness": mean([item.faithfulness for item in judged]),
                "faithfulness_judged": len(judged),
                "answer_keyword_coverage": mean([
                    item.answer_keyword_coverage for item in covered
                    if item.answer_keyword_coverage is not None
                ]),
                "fully_covered_answer_rate": mean([
                    1.0 if item.answer_keyword_coverage == 1.0 else 0.0
                    for item in covered
                ]),
                "total_prompt_tokens": sum(item.prompt_tokens for item in results),
                "total_completion_tokens": sum(
                    item.completion_tokens for item in results
                ),
                "answer_prompt_tokens": sum(
                    item.answer_prompt_tokens for item in results
                ),
                "answer_completion_tokens": sum(
                    item.answer_completion_tokens for item in results
                ),
                "judge_prompt_tokens": sum(
                    item.judge_prompt_tokens for item in results
                ),
                "judge_completion_tokens": sum(
                    item.judge_completion_tokens for item in results
                ),
                "estimated_answer_cost_usd": sum(
                    item.estimated_answer_cost_usd for item in results
                ),
                "estimated_judge_cost_usd": (
                    sum(item.estimated_judge_cost_usd or 0.0 for item in judged)
                    if judged
                    and all(item.estimated_judge_cost_usd is not None for item in judged)
                    else None
                ),
                "estimated_cost_usd": (
                    sum(item.estimated_cost_usd or 0.0 for item in results)
                    if all(item.estimated_cost_usd is not None for item in results)
                    else None
                ),
                "pricing": {
                    "answer": {
                        "input_cost_per_million_usd": self.input_cost_per_million,
                        "output_cost_per_million_usd": self.output_cost_per_million,
                    },
                    "judge": {
                        "input_cost_per_million_usd": self.judge_input_cost_per_million,
                        "output_cost_per_million_usd": self.judge_output_cost_per_million,
                    },
                },
                "latency_ms": {
                    name: {
                        "avg": mean(values),
                        "p50": _percentile(values, 0.50),
                        "p95": _percentile(values, 0.95),
                    }
                    for name, values in {
                        "retrieval": [
                            item.retrieval_latency_ms for item in valid_retrieval
                        ],
                        "answer": [item.answer_latency_ms for item in answered],
                        "judge": [item.judge_latency_ms for item in judged],
                        "end_to_end": [
                            item.end_to_end_latency_ms for item in results
                        ],
                    }.items()
                },
                "by_category": {
                    category: {
                        "total": len(category_results),
                        "recall_at_k": {
                            str(k): mean([
                                item.recall_at_k[str(k)]
                                for item in category_results
                                if item.ranked_chunk_ids
                            ])
                            for k in self.k_values
                        },
                        "mrr": mean([
                            item.reciprocal_rank
                            for item in category_results
                            if item.ranked_chunk_ids
                        ]),
                        "faithfulness": mean([
                            item.faithfulness
                            for item in category_results
                            if item.faithfulness is not None
                        ]),
                    }
                    for category in categories
                    for category_results in [[
                        item for item in results if item.category == category
                    ]]
                },
            },
            "cases": [asdict(item) for item in results],
        }
