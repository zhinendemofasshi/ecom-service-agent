"""运行 30 条 RAG 专项评估并输出 Recall@K、MRR、Faithfulness。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402

from app.agent.rag.backends import create_backend  # noqa: E402
from app.agent.rag.chunker import chunk_markdown_dir  # noqa: E402
from app.agent.rag.embedder import Embedder  # noqa: E402
from app.agent.rag.retriever import KnowledgeRetriever  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.evaluation.rag_dataset import load_rag_dataset  # noqa: E402
from app.evaluation.rag_evaluator import RagEvaluator  # noqa: E402


def _build_backend(name: str):
    if name == "numpy":
        return create_backend("numpy", index_path=ROOT / settings.kb_index_path)
    if name == "chroma":
        return create_backend(
            "chroma",
            persist_dir=ROOT / settings.chroma_persist_dir,
            collection_name=settings.chroma_collection,
        )
    raise ValueError(f"未知后端: {name}")


def _format_percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _print_summary(report: dict) -> None:
    summary = report["summary"]
    print("\n" + "=" * 72)
    print("  RAG 专项评估汇总")
    print("=" * 72)
    recall = summary["recall_at_k"]
    print("  " + " / ".join(
        f"Recall@{k} {_format_percent(value)}" for k, value in recall.items()
    ))
    print(f"  MRR                         : {summary['mrr']:.4f}")
    print(
        "  Faithfulness                : "
        f"{_format_percent(summary['faithfulness'])} "
        f"({summary['faithfulness_judged']}/{summary['total']} 条已评审)"
    )
    print(
        "  回答关键事实覆盖率          : "
        f"{_format_percent(summary['answer_keyword_coverage'])}"
    )
    latency = summary["latency_ms"]
    print(
        "  检索延迟 avg / P95          : "
        f"{latency['retrieval']['avg']:.0f} / "
        f"{latency['retrieval']['p95']:.0f} ms"
    )
    if summary["faithfulness_judged"]:
        print(
            "  端到端延迟 avg / P95        : "
            f"{latency['end_to_end']['avg']:.0f} / "
            f"{latency['end_to_end']['p95']:.0f} ms"
        )
        print(
            "  Chat tokens input / output  : "
            f"{summary['total_prompt_tokens']} / "
            f"{summary['total_completion_tokens']}"
        )
        total_cost = summary["estimated_cost_usd"]
        if total_cost is not None:
            print(f"  估算 Chat 成本              : ${total_cost:.6f}")
        else:
            print(
                "  已知回答生成成本            : "
                f"${summary['estimated_answer_cost_usd']:.6f}"
            )
            print("  Judge 成本                  : 未配置该模型单价，不估算")
    print(f"  异常用例                    : {summary['errors']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 RAG 专项评估")
    parser.add_argument(
        "--dataset",
        default="app/evaluation/rag_cases.json",
        help="RAG 评估集 JSON 路径",
    )
    parser.add_argument(
        "--backend",
        choices=["numpy", "chroma"],
        default=settings.rag_backend,
        help=f"向量后端（默认: {settings.rag_backend}）",
    )
    parser.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=[1, 3, 5],
        help="Recall@K 列表（默认: 1 3 5）",
    )
    parser.add_argument(
        "--model",
        default=settings.model_name,
        help=f"回答生成模型（默认: {settings.model_name}）",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Faithfulness 裁判模型（默认与 --model 相同）",
    )
    parser.add_argument(
        "--judge-input-cost-per-million",
        type=float,
        default=None,
        help="Judge 输入单价（USD/1M tokens；不同模型且未提供时不估算 Judge 成本）",
    )
    parser.add_argument(
        "--judge-output-cost-per-million",
        type=float,
        default=None,
        help="Judge 输出单价（USD/1M tokens）",
    )
    parser.add_argument(
        "--judge",
        dest="judge",
        action="store_true",
        default=settings.eval_use_judge,
        help="生成回答并评估 Faithfulness（默认开启）",
    )
    parser.add_argument(
        "--no-judge",
        dest="judge",
        action="store_false",
        help="只评估检索 Recall@K / MRR",
    )
    parser.add_argument("--output", default=None, help="完整 JSON 报告输出路径")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="只跑指定 case id，可重复传入",
    )
    args = parser.parse_args()

    if not settings.openai_api_key:
        print("❌ 未配置 OPENAI_API_KEY")
        raise SystemExit(2)
    if any(k <= 0 for k in args.k):
        print("❌ --k 必须全部为正整数")
        raise SystemExit(2)

    dataset_path = Path(args.dataset)
    if not dataset_path.is_absolute():
        dataset_path = ROOT / dataset_path
    if not dataset_path.exists():
        print(f"❌ 测试集不存在: {dataset_path}")
        raise SystemExit(1)

    cases = load_rag_dataset(dataset_path)
    if args.case_id:
        requested = set(args.case_id)
        cases = [case for case in cases if case.id in requested]
        missing = requested.difference(case.id for case in cases)
        if missing:
            print(f"❌ 未找到用例: {', '.join(sorted(missing))}")
            raise SystemExit(1)
    if args.limit is not None:
        if args.limit <= 0:
            print("❌ --limit 必须大于 0")
            raise SystemExit(2)
        cases = cases[:args.limit]
    if not cases:
        print("❌ 测试集为空")
        raise SystemExit(1)

    current_chunks = chunk_markdown_dir(ROOT / settings.kb_dir)
    current_ids = {chunk.chunk_id for chunk in current_chunks}
    labeled_ids = {
        chunk_id for case in cases for chunk_id in case.relevant_chunk_ids
    }
    unknown_ids = sorted(labeled_ids - current_ids)
    if unknown_ids:
        print(f"❌ 数据集引用了不存在的 Chunk: {', '.join(unknown_ids)}")
        raise SystemExit(1)

    print("=" * 72)
    print("  并夕夕 · RAG 专项评估")
    print(f"  数据集       : {dataset_path}")
    print(f"  用例数       : {len(cases)}")
    print(f"  知识块       : {len(current_ids)}（本次标注覆盖 {len(labeled_ids)}）")
    print(f"  后端         : {args.backend}")
    print(f"  Embedding    : {settings.embedding_model}")
    print(f"  回答模型     : {args.model if args.judge else '关闭'}")
    print(
        "  Judge 模型   : "
        f"{(args.judge_model or args.model) if args.judge else '关闭'}"
    )
    print(f"  Recall K     : {', '.join(str(k) for k in sorted(set(args.k)))}")
    print("=" * 72)

    embedder = Embedder(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.embedding_model,
    )
    retriever = KnowledgeRetriever(embedder, _build_backend(args.backend))
    client = (
        OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        if args.judge
        else None
    )
    try:
        retriever.load()
        evaluator = RagEvaluator(
            retriever=retriever,
            client=client,
            model=args.model,
            judge_model=args.judge_model,
            k_values=args.k,
            use_judge=args.judge,
            input_cost_per_million=settings.eval_input_cost_per_million,
            output_cost_per_million=settings.eval_output_cost_per_million,
            judge_input_cost_per_million=args.judge_input_cost_per_million,
            judge_output_cost_per_million=args.judge_output_cost_per_million,
        )

        def show_progress(index, total, result):
            recalls = "/".join(
                f"{result.recall_at_k.get(str(k), 0):.2f}"
                for k in evaluator.k_values
            )
            judge = (
                "-" if result.faithfulness is None
                else ("✓" if result.faithfulness == 1.0 else "✗")
            )
            status = f" ERROR {result.error}" if result.error else ""
            print(
                f"[{index:02d}/{total:02d}] {result.case_id:<30} "
                f"R={recalls} RR={result.reciprocal_rank:.2f} F={judge}{status}",
                flush=True,
            )

        report = evaluator.run_all(cases, progress=show_progress)
    finally:
        retriever.close()
        if client is not None:
            client.close()

    report["metadata"]["dataset_path"] = str(dataset_path)
    report["metadata"]["dataset_sha256"] = hashlib.sha256(
        dataset_path.read_bytes()
    ).hexdigest()
    report["metadata"]["knowledge_chunk_count"] = len(current_ids)
    report["metadata"]["labeled_chunk_count"] = len(labeled_ids)

    _print_summary(report)
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n报告已写入: {output_path}")


if __name__ == "__main__":
    main()
