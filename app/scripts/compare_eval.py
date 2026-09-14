"""在同一数据集上对比 baseline（全 LLM 路由）与 optimized（规则 + LLM 回退）。

默认不启用 LLM-as-judge，以保证路由、工具、token、延迟和成本指标
不被额外裁判调用混入。两轮都是真实 Agent/API 重跑，不使用伪造 trace。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402

from app.config.settings import settings  # noqa: E402
from app.evaluation.dataset import load_dataset  # noqa: E402
from app.evaluation.evaluator import Evaluator  # noqa: E402
from app.evaluation.sandbox import Sandbox  # noqa: E402
from app.agent.tools.knowledge import reset_retriever  # noqa: E402


def _run_variant(cases, variant: str, use_judge: bool) -> dict:
    # 避免后跑变体复用前一轮 RAG 单例缓存，导致延迟对比偏置。
    reset_retriever()
    judge_client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    try:
        with Sandbox(mode="multi", variant=variant) as sandbox:
            evaluator = Evaluator(
                sandbox=sandbox,
                client=judge_client,
                model=settings.model_name,
                use_judge=use_judge,
                pass_threshold=settings.eval_pass_threshold,
                input_cost_per_million=settings.eval_input_cost_per_million,
                output_cost_per_million=settings.eval_output_cost_per_million,
            )
            return evaluator.run_all(cases)
    finally:
        judge_client.close()


def _relative_reduction(before: float, after: float) -> float | None:
    if before == 0:
        return None
    return (before - after) / before


def build_comparison(baseline: dict, optimized: dict) -> dict:
    """生成仅基于两份原始报告的可审计差值摘要。"""
    before = baseline["summary"]
    after = optimized["summary"]
    metrics = {
        "pass_rate": {
            "baseline": before["pass_rate"],
            "optimized": after["pass_rate"],
            "delta_percentage_points": (after["pass_rate"] - before["pass_rate"]) * 100,
        },
        "route_accuracy": {
            "baseline": before["route_accuracy"],
            "optimized": after["route_accuracy"],
            "delta_percentage_points": (
                (after["route_accuracy"] - before["route_accuracy"]) * 100
                if before["route_accuracy"] is not None and after["route_accuracy"] is not None
                else None
            ),
        },
        "tool_precision": {
            "baseline": before["avg_tool_precision"],
            "optimized": after["avg_tool_precision"],
            "delta_percentage_points": (
                (after["avg_tool_precision"] - before["avg_tool_precision"]) * 100
                if before["avg_tool_precision"] is not None
                and after["avg_tool_precision"] is not None
                else None
            ),
        },
        "total_tokens": {
            "baseline": before["total_tokens"],
            "optimized": after["total_tokens"],
            "reduction": _relative_reduction(before["total_tokens"], after["total_tokens"]),
        },
        "total_llm_calls": {
            "baseline": before["total_llm_calls"],
            "optimized": after["total_llm_calls"],
            "reduction": _relative_reduction(
                before["total_llm_calls"], after["total_llm_calls"]
            ),
        },
        "estimated_cost_usd": {
            "baseline": before["estimated_cost_usd"],
            "optimized": after["estimated_cost_usd"],
            "reduction": _relative_reduction(
                before["estimated_cost_usd"], after["estimated_cost_usd"]
            ),
        },
        "avg_latency_ms": {
            "baseline": before["latency_ms"]["avg"],
            "optimized": after["latency_ms"]["avg"],
            "reduction": _relative_reduction(
                before["latency_ms"]["avg"], after["latency_ms"]["avg"]
            ),
        },
        "p95_latency_ms": {
            "baseline": before["latency_ms"]["p95"],
            "optimized": after["latency_ms"]["p95"],
            "reduction": _relative_reduction(
                before["latency_ms"]["p95"], after["latency_ms"]["p95"]
            ),
        },
    }
    return {
        "metadata": {
            "baseline_generated_at": baseline["metadata"]["generated_at"],
            "optimized_generated_at": optimized["metadata"]["generated_at"],
            "model": baseline["metadata"]["model"],
            "mode": "multi",
            "judge_enabled": baseline["metadata"]["judge_enabled"],
            "temperature": baseline["metadata"]["temperature"],
            "cases": baseline["summary"]["total"],
            "pricing": baseline["summary"]["pricing"],
            "cost_scope": (
                "agent Responses calls only; embeddings and evaluator judge excluded"
            ),
        },
        "metrics": metrics,
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _delta_pp(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1f} pp"


def _reduction(value: float | None) -> str:
    if value is None:
        return "-"
    direction = "↓" if value >= 0 else "↑"
    return f"{direction} {abs(value) * 100:.1f}%"


def _reduction_phrase(value: float | None) -> str:
    if value is None:
        return "变化不可计算"
    verb = "降低" if value >= 0 else "升高"
    return f"{verb} {abs(value) * 100:.1f}%"


def _report_path(path: Path) -> str:
    """仓库内数据集写相对路径，避免报告泄露本机用户名和目录。"""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def _print_comparison(comparison: dict) -> None:
    m = comparison["metrics"]
    print("\n" + "=" * 78)
    print("  Baseline vs Optimized 真实重跑对比")
    print("=" * 78)
    print(
        f"  通过率       : {_pct(m['pass_rate']['baseline'])} -> "
        f"{_pct(m['pass_rate']['optimized'])}"
    )
    print(
        f"  路由准确率   : {_pct(m['route_accuracy']['baseline'])} -> "
        f"{_pct(m['route_accuracy']['optimized'])}"
    )
    print(
        f"  工具 Precision : {_pct(m['tool_precision']['baseline'])} -> "
        f"{_pct(m['tool_precision']['optimized'])}"
    )
    print(
        f"  总 token     : {m['total_tokens']['baseline']} -> "
        f"{m['total_tokens']['optimized']} "
        f"({_reduction_phrase(m['total_tokens']['reduction'])})"
    )
    print(
        f"  LLM 调用次数  : {m['total_llm_calls']['baseline']} -> "
        f"{m['total_llm_calls']['optimized']} "
        f"({_reduction_phrase(m['total_llm_calls']['reduction'])})"
    )
    print(
        f"  API 成本      : ${m['estimated_cost_usd']['baseline']:.6f} -> "
        f"${m['estimated_cost_usd']['optimized']:.6f} "
        f"({_reduction_phrase(m['estimated_cost_usd']['reduction'])})"
    )
    print(
        f"  平均延迟     : {m['avg_latency_ms']['baseline']:.0f} ms -> "
        f"{m['avg_latency_ms']['optimized']:.0f} ms "
        f"({_reduction_phrase(m['avg_latency_ms']['reduction'])})"
    )
    print(
        f"  P95 延迟     : {m['p95_latency_ms']['baseline']:.0f} ms -> "
        f"{m['p95_latency_ms']['optimized']:.0f} ms "
        f"({_reduction_phrase(m['p95_latency_ms']['reduction'])})"
    )


def _comparison_markdown(comparison: dict) -> str:
    """生成可直接用于 README/简历素材的对比摘要。"""
    meta = comparison["metadata"]
    m = comparison["metrics"]
    return f"""# Agent 评估优化实验

- 模型：`{meta['model']}`
- 用例：{meta['cases']} 条
- 模式：Multi-Agent，baseline 全 LLM 路由 vs optimized 高置信规则 + LLM 回退
- 采样温度：`{meta['temperature']}`
- 数据集 SHA-256：`{meta['dataset_sha256']}`
- 成本口径：{meta['cost_scope']}

| 指标 | Baseline | Optimized | 变化 |
|---|---:|---:|---:|
| 通过率 | {_pct(m['pass_rate']['baseline'])} | {_pct(m['pass_rate']['optimized'])} | {_delta_pp(m['pass_rate']['delta_percentage_points'])} |
| 路由准确率 | {_pct(m['route_accuracy']['baseline'])} | {_pct(m['route_accuracy']['optimized'])} | {_delta_pp(m['route_accuracy']['delta_percentage_points'])} |
| 工具 Precision | {_pct(m['tool_precision']['baseline'])} | {_pct(m['tool_precision']['optimized'])} | {_delta_pp(m['tool_precision']['delta_percentage_points'])} |
| LLM 调用次数 | {m['total_llm_calls']['baseline']} | {m['total_llm_calls']['optimized']} | {_reduction(m['total_llm_calls']['reduction'])} |
| 总 token | {m['total_tokens']['baseline']} | {m['total_tokens']['optimized']} | {_reduction(m['total_tokens']['reduction'])} |
| 估算 API 成本 | ${m['estimated_cost_usd']['baseline']:.6f} | ${m['estimated_cost_usd']['optimized']:.6f} | {_reduction(m['estimated_cost_usd']['reduction'])} |
| 平均延迟 | {m['avg_latency_ms']['baseline']:.0f} ms | {m['avg_latency_ms']['optimized']:.0f} ms | {_reduction(m['avg_latency_ms']['reduction'])} |
| P95 延迟 | {m['p95_latency_ms']['baseline']:.0f} ms | {m['p95_latency_ms']['optimized']:.0f} ms | {_reduction(m['p95_latency_ms']['reduction'])} |

> 以上数值由 `app/scripts/compare_eval.py` 对同一黄金集连续真实重跑生成；原始逐用例 trace 见同目录 JSON 报告。
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="对比 baseline/optimized Agent 评估")
    parser.add_argument("--dataset", default=settings.eval_dataset_path)
    parser.add_argument("--output-dir", default="app/sessions/eval_comparison")
    parser.add_argument("--judge", action="store_true", help="启用 LLM-as-judge（默认关闭）")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.is_absolute():
        dataset_path = ROOT / dataset_path
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir

    if not settings.openai_api_key:
        print("❌ 未配置 OPENAI_API_KEY：请复制 .env.example 为 .env 并填入密钥")
        sys.exit(2)

    cases = load_dataset(dataset_path)
    dataset_sha256 = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    dataset_report_path = _report_path(dataset_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"开始真实重跑：{len(cases)} 条用例，模型 {settings.model_name}")
    print("\n[1/2] baseline：全 LLM 路由")
    baseline = _run_variant(cases, "baseline", args.judge)
    baseline["metadata"]["dataset_path"] = dataset_report_path
    baseline["metadata"]["dataset_sha256"] = dataset_sha256
    (output_dir / "baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n[2/2] optimized：高置信规则 + LLM 回退")
    optimized = _run_variant(cases, "optimized", args.judge)

    for report in (baseline, optimized):
        report["metadata"]["dataset_path"] = dataset_report_path
        report["metadata"]["dataset_sha256"] = dataset_sha256

    comparison = build_comparison(baseline, optimized)
    comparison["metadata"]["dataset_path"] = dataset_report_path
    comparison["metadata"]["dataset_sha256"] = dataset_sha256

    for name, payload in (
        ("baseline.json", baseline),
        ("optimized.json", optimized),
        ("comparison.json", comparison),
    ):
        (output_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (output_dir / "comparison.md").write_text(
        _comparison_markdown(comparison), encoding="utf-8"
    )

    _print_comparison(comparison)
    print(f"\n原始报告与差值已写入: {output_dir}")


if __name__ == "__main__":
    main()
