"""用当前确定性指标离线重评分历史评估 trace，不重新调用模型。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.config.settings import settings  # noqa: E402
from app.evaluation import metrics  # noqa: E402
from app.evaluation.dataset import load_dataset  # noqa: E402


def _score_case(case, historical: dict, threshold: float) -> dict:
    trace = historical["trace"]
    called = trace.get("tool_calls", [])
    allowed_calls = case.max_tool_calls
    if allowed_calls is None and case.min_tool_calls is not None:
        allowed_calls = case.min_tool_calls + len(case.optional_tools)

    scores = {
        "tool_precision": metrics.tool_precision(
            case.expected_tools, called, case.optional_tools
        ),
        "tool_recall": metrics.tool_recall(case.expected_tools, called),
        "tool_efficiency": metrics.tool_efficiency(
            allowed_calls, trace.get("num_tool_calls", len(called))
        ),
        "route": metrics.route_match(case.expected_route, trace.get("route")),
        "intent": metrics.intent_match(
            case.expected_intent, trace.get("intent") or ""
        ),
        "keywords": metrics.keyword_coverage(
            case.expected_keywords, trace.get("reply") or ""
        ),
        "requires_human": metrics.requires_human_match(
            case.expected_requires_human,
            bool(trace.get("requires_human")),
        ),
    }
    failure_reasons = [
        name for name, value in scores.items()
        if value is not None and value < threshold
    ]
    token_pass = metrics.token_cost_pass(
        trace.get("total_tokens", 0), case.max_tokens
    )
    if token_pass is False:
        failure_reasons.insert(0, "token_budget")

    return {
        "case_id": case.id,
        "passed": not failure_reasons,
        "failure_reasons": failure_reasons,
        "scores": scores,
        "token_pass": token_pass,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="使用当前规则指标离线重评分历史 no-judge 报告"
    )
    parser.add_argument("report", help="历史评估 JSON 报告")
    parser.add_argument(
        "--dataset", default=settings.eval_dataset_path, help="当前黄金集 JSON"
    )
    parser.add_argument("--output", default=None, help="可选的 JSON 输出路径")
    args = parser.parse_args()

    report_path = Path(args.report)
    dataset_path = Path(args.dataset)
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    if not dataset_path.is_absolute():
        dataset_path = ROOT / dataset_path

    historical = json.loads(report_path.read_text(encoding="utf-8"))
    historical_by_id = {row["case_id"]: row for row in historical["cases"]}
    cases = load_dataset(dataset_path)
    missing = [case.id for case in cases if case.id not in historical_by_id]
    if missing:
        raise ValueError(f"历史报告缺少用例: {', '.join(missing)}")

    results = [
        _score_case(case, historical_by_id[case.id], settings.eval_pass_threshold)
        for case in cases
    ]
    total = len(results)
    passed = sum(result["passed"] for result in results)
    reasons = sorted(
        {reason for result in results for reason in result["failure_reasons"]}
    )
    output = {
        "metadata": {
            "source_report": str(report_path.relative_to(ROOT)),
            "source_generated_at": historical.get("metadata", {}).get("generated_at"),
            "source_model": historical.get("metadata", {}).get("model"),
            "dataset_path": str(dataset_path.relative_to(ROOT)),
            "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
            "note": "offline rescore only; no model or tool calls were executed",
        },
        "summary": {
            "total": total,
            "passed": passed,
            "pass_rate": passed / total if total else 0.0,
            "failure_reasons": {
                reason: sum(
                    reason in result["failure_reasons"] for result in results
                )
                for reason in reasons
            },
        },
        "cases": results,
    }

    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
