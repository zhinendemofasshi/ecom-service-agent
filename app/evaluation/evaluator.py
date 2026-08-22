"""评估执行器：编排 沙箱跑用例 → 双层评分 → 聚合报告（第9期）。

Evaluator 自己不碰 Agent，只负责：让 Sandbox 跑出 RunTrace，再用 metrics 对轨迹
打分（过程层 + 结果层，代码规则恒算、LLM judge 受 use_judge 控制），最后聚合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from openai import OpenAI

from app.evaluation import metrics
from app.evaluation.dataset import EvalCase
from app.evaluation.sandbox import Sandbox


@dataclass
class EvalResult:
    """单条用例的评估结果。所有评分维度 None 表示该用例未指定（不计入聚合）。"""

    case_id: str
    description: str
    category: str = "general"

    # ---------- 过程指标 ----------
    tool_accuracy: float | None = None  # 兼容字段，值等于 tool_recall
    tool_precision: float | None = None
    tool_recall: float | None = None
    tool_f1: float | None = None
    tool_efficiency: float | None = None
    token_cost: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    end_to_end_latency_ms: float = 0.0
    llm_latency_ms: float = 0.0
    token_pass: bool | None = None
    process_soundness: float | None = None  # judge 1-5 归一化到 0-1
    route_match: float | None = None

    # ---------- 结果指标 ----------
    intent_match: float | None = None
    keyword_coverage: float | None = None
    requires_human_match: float | None = None
    answer_quality: float | None = None  # judge 1-5 归一化到 0-1
    faithfulness: float | None = None  # 1.0 忠实 / 0.0 幻觉

    # ---------- 汇总 ----------
    process_score: float | None = None
    result_score: float | None = None
    passed: bool = False
    failure_reasons: list[str] = field(default_factory=list)

    trace: dict = field(default_factory=dict)  # RunTrace 精简快照
    judge_reasons: dict = field(default_factory=dict)  # judge 文字理由
    error: str | None = None


def _avg(values: list[float | None]) -> float | None:
    """对非 None 项求均值；全为 None 返回 None。"""
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


class Evaluator:
    """评估器：对一批用例执行沙箱运行 + 双层评分 + 聚合。"""

    def __init__(
        self,
        sandbox: Sandbox,
        client: OpenAI,
        model: str,
        use_judge: bool = True,
        pass_threshold: float = 0.6,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
    ):
        self.sandbox = sandbox
        self.client = client
        self.model = model
        self.use_judge = use_judge
        self.pass_threshold = pass_threshold
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million

    def run_case(self, case: EvalCase) -> EvalResult:
        trace = self.sandbox.run(case)
        res = EvalResult(
            case_id=case.id,
            description=case.description,
            category=case.category,
            trace=trace.to_dict(),
        )
        res.end_to_end_latency_ms = trace.end_to_end_latency_ms
        res.llm_latency_ms = trace.llm_latency_ms
        res.token_cost = trace.total_tokens
        res.prompt_tokens = trace.prompt_tokens
        res.completion_tokens = trace.completion_tokens
        res.estimated_cost_usd = metrics.estimated_cost_usd(
            trace.prompt_tokens,
            trace.completion_tokens,
            self.input_cost_per_million,
            self.output_cost_per_million,
        )

        if trace.error:
            res.error = trace.error
            return res

        try:
            resp = trace.final_response
            reply = resp.reply if resp else ""
            actual_intent = resp.intent.value if resp else ""
            actual_human = resp.requires_human if resp else False
            last_input = case.turns[-1]
            called = trace.tool_call_names

            # ---------- 过程指标（代码规则）----------
            res.tool_recall = metrics.tool_recall(case.expected_tools, called)
            res.tool_accuracy = res.tool_recall
            res.tool_precision = metrics.tool_precision(
                case.expected_tools, called, case.optional_tools
            )
            res.tool_f1 = metrics.tool_f1(res.tool_precision, res.tool_recall)
            allowed_calls = case.max_tool_calls
            if allowed_calls is None and case.min_tool_calls is not None:
                allowed_calls = case.min_tool_calls + len(case.optional_tools)
            res.tool_efficiency = metrics.tool_efficiency(
                allowed_calls, trace.num_tool_calls
            )
            res.token_pass = metrics.token_cost_pass(trace.total_tokens, case.max_tokens)
            res.route_match = metrics.route_match(case.expected_route, trace.route)

            # ---------- 结果指标（代码规则）----------
            res.intent_match = metrics.intent_match(case.expected_intent, actual_intent)
            res.keyword_coverage = metrics.keyword_coverage(case.expected_keywords, reply)
            res.requires_human_match = metrics.requires_human_match(
                case.expected_requires_human, actual_human
            )

            # ---------- LLM judge ----------
            if self.use_judge:
                q_score, q_reason = metrics.judge_answer_quality(
                    self.client,
                    self.model,
                    last_input,
                    reply,
                    [
                        " / ".join(item) if isinstance(item, list) else item
                        for item in case.expected_keywords
                    ],
                )
                res.answer_quality = q_score / 5.0 if q_score else None
                res.judge_reasons["answer_quality"] = q_reason

                f_score, f_reason = metrics.judge_faithfulness(
                    self.client, self.model, reply, trace.tool_observations
                )
                res.faithfulness = f_score
                res.judge_reasons["faithfulness"] = f_reason

                p_score, p_reason = metrics.judge_process_soundness(
                    self.client, self.model, last_input, called
                )
                res.process_soundness = p_score / 5.0 if p_score else None
                res.judge_reasons["process_soundness"] = p_reason

            # ---------- 汇总 ----------
            res.process_score = _avg([
                res.tool_precision, res.tool_recall, res.tool_efficiency,
                res.process_soundness, res.route_match,
            ])
            res.result_score = _avg([
                res.intent_match, res.keyword_coverage,
                res.requires_human_match, res.answer_quality, res.faithfulness,
            ])
            res.failure_reasons = self._failure_reasons(res)
            res.passed = not res.failure_reasons

        except Exception as e:  # noqa: BLE001 —— 评分异常隔离到单条用例
            res.error = f"评分异常 {type(e).__name__}: {e}"

        return res

    def run_all(self, cases: list[EvalCase]) -> dict:
        results = [self.run_case(c) for c in cases]
        return self._aggregate(results)

    def _failure_reasons(self, res: EvalResult) -> list[str]:
        """返回可审计失败原因；空列表代表所有已指定门槛均通过。"""
        if res.error:
            return ["error"]
        reasons: list[str] = []
        if res.token_pass is False:
            reasons.append("token_budget")
        dims = {
            "tool_precision": res.tool_precision,
            "tool_recall": res.tool_recall,
            "tool_efficiency": res.tool_efficiency,
            "process_soundness": res.process_soundness,
            "route": res.route_match,
            "intent": res.intent_match,
            "keywords": res.keyword_coverage,
            "requires_human": res.requires_human_match,
            "answer_quality": res.answer_quality,
            "faithfulness": res.faithfulness,
        }
        reasons.extend(
            name for name, value in dims.items()
            if value is not None and value < self.pass_threshold
        )
        return reasons

    def _decide_pass(self, res: EvalResult) -> bool:
        """兼容旧调用：所有已指定评分门槛都通过才返回 True。"""
        return not self._failure_reasons(res)

    def _aggregate(self, results: list[EvalResult]) -> dict:
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        total_tokens = sum(r.token_cost for r in results)
        total_prompt_tokens = sum(r.prompt_tokens for r in results)
        total_completion_tokens = sum(r.completion_tokens for r in results)
        total_cost = sum(r.estimated_cost_usd for r in results)
        latencies = [r.end_to_end_latency_ms for r in results]
        summary = {
            "total": total,
            "passed": passed,
            "pass_rate": passed / total if total else 0.0,
            "failure_reasons": {
                reason: sum(reason in r.failure_reasons for r in results)
                for reason in sorted({reason for r in results for reason in r.failure_reasons})
            },
            "avg_process_score": _avg([r.process_score for r in results]),
            "avg_result_score": _avg([r.result_score for r in results]),
            "avg_tool_precision": _avg([r.tool_precision for r in results]),
            "avg_tool_recall": _avg([r.tool_recall for r in results]),
            "avg_tool_f1": _avg([r.tool_f1 for r in results]),
            "route_accuracy": _avg([r.route_match for r in results]),
            "total_tokens": total_tokens,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "avg_tokens_per_case": total_tokens / total if total else 0,
            "total_llm_calls": sum(r.trace.get("num_llm_calls", 0) for r in results),
            "avg_llm_calls_per_case": (
                sum(r.trace.get("num_llm_calls", 0) for r in results) / total if total else 0
            ),
            "route_decisions": {
                "rule": sum(1 for r in results if r.trace.get("route_source") == "rule"),
                "llm": sum(1 for r in results if r.trace.get("route_source") == "llm"),
            },
            "estimated_cost_usd": total_cost,
            "avg_cost_per_case_usd": total_cost / total if total else 0,
            "pricing": {
                "input_cost_per_million_usd": self.input_cost_per_million,
                "output_cost_per_million_usd": self.output_cost_per_million,
            },
            "latency_ms": {
                "avg": _avg(latencies),
                "p50": self._percentile(latencies, 0.50),
                "p95": self._percentile(latencies, 0.95),
                "max": max(latencies) if latencies else None,
                "total_llm": sum(r.llm_latency_ms for r in results),
            },
        }
        categories = sorted({r.category for r in results})
        summary["by_category"] = {
            category: {
                "total": len(category_results),
                "passed": sum(1 for r in category_results if r.passed),
                "pass_rate": (
                    sum(1 for r in category_results if r.passed) / len(category_results)
                ),
                "avg_tool_precision": _avg([r.tool_precision for r in category_results]),
                "avg_tool_recall": _avg([r.tool_recall for r in category_results]),
            }
            for category in categories
            for category_results in [[r for r in results if r.category == category]]
        }
        return {
            "metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": self.model,
                "mode": self.sandbox.mode,
                "variant": self.sandbox.variant,
                "judge_enabled": self.use_judge,
                "temperature": getattr(self.sandbox, "temperature", None),
                "cost_scope": "agent chat completions only; embeddings and evaluator judge excluded",
            },
            "summary": summary,
            "cases": [self._result_to_dict(r) for r in results],
        }

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float | None:
        """线性插值分位数，避免为一个小指标引入 numpy 依赖。"""
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * quantile
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    @staticmethod
    def _result_to_dict(r: EvalResult) -> dict:
        return {
            "case_id": r.case_id,
            "description": r.description,
            "category": r.category,
            "passed": r.passed,
            "failure_reasons": r.failure_reasons,
            "process": {
                "tool_accuracy": r.tool_accuracy,
                "tool_precision": r.tool_precision,
                "tool_recall": r.tool_recall,
                "tool_f1": r.tool_f1,
                "tool_efficiency": r.tool_efficiency,
                "token_cost": r.token_cost,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "estimated_cost_usd": r.estimated_cost_usd,
                "end_to_end_latency_ms": r.end_to_end_latency_ms,
                "llm_latency_ms": r.llm_latency_ms,
                "token_pass": r.token_pass,
                "process_soundness": r.process_soundness,
                "route_match": r.route_match,
                "process_score": r.process_score,
            },
            "result": {
                "intent_match": r.intent_match,
                "keyword_coverage": r.keyword_coverage,
                "requires_human_match": r.requires_human_match,
                "answer_quality": r.answer_quality,
                "faithfulness": r.faithfulness,
                "result_score": r.result_score,
            },
            "judge_reasons": r.judge_reasons,
            "trace": r.trace,
            "error": r.error,
        }
