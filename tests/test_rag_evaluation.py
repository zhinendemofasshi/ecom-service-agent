"""RAG 专项评估的离线测试（不调用外部 API）。"""

from pathlib import Path
from types import SimpleNamespace

from app.agent.rag.backends.base import RetrievedChunk
from app.agent.rag.chunker import Chunk, chunk_markdown_dir
from app.config.settings import settings
from app.evaluation.rag_dataset import RagEvalCase, load_rag_dataset
from app.evaluation.rag_evaluator import RagEvaluator
from app.evaluation.rag_metrics import mean, recall_at_k, reciprocal_rank


ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "app" / "evaluation" / "rag_cases.json"


def test_rag_dataset_has_30_cases_and_covers_all_chunks():
    cases = load_rag_dataset(DATASET)
    chunks = chunk_markdown_dir(ROOT / settings.kb_dir)
    chunk_ids = {chunk.chunk_id for chunk in chunks}
    labeled_ids = {item for case in cases for item in case.relevant_chunk_ids}

    assert len(cases) == 30
    assert len({case.id for case in cases}) == 30
    assert len(chunks) == 19
    assert labeled_ids == chunk_ids
    assert sum(len(case.relevant_chunk_ids) > 1 for case in cases) >= 3
    assert all(case.expected_keywords for case in cases)


def test_recall_at_k_and_reciprocal_rank_support_multi_relevant_cases():
    relevant = ["a", "b"]
    ranked = ["x", "a", "b", "y"]

    assert recall_at_k(relevant, ranked, 1) == 0.0
    assert recall_at_k(relevant, ranked, 2) == 0.5
    assert recall_at_k(relevant, ranked, 3) == 1.0
    assert reciprocal_rank(relevant, ranked) == 0.5
    assert mean([1.0, 0.5, 0.0]) == 0.5


def test_retrieval_only_evaluator_aggregates_recall_and_mrr():
    class FakeEmbedder:
        model = "fake-embedding"

    class FakeRetriever:
        size = 4
        backend = object()
        _embedder = FakeEmbedder()

        @staticmethod
        def search(query, top_k):
            assert query == "测试问题"
            assert top_k == 4
            ranked_ids = ["x", "a", "b", "y"]
            return [
                RetrievedChunk(
                    chunk=Chunk(
                        chunk_id=chunk_id,
                        doc="测试文档",
                        section="测试章节",
                        text=f"内容 {chunk_id}",
                    ),
                    score=1.0 / rank,
                )
                for rank, chunk_id in enumerate(ranked_ids, start=1)
            ]

    case = RagEvalCase(
        id="multi",
        query="测试问题",
        relevant_chunk_ids=["a", "b"],
        expected_keywords=["内容"],
    )
    evaluator = RagEvaluator(
        retriever=FakeRetriever(),
        client=None,
        model="fake-model",
        k_values=[1, 2, 3],
        use_judge=False,
    )
    report = evaluator.run_all([case])

    assert report["summary"]["recall_at_k"] == {
        "1": 0.0,
        "2": 0.5,
        "3": 1.0,
    }
    assert report["summary"]["full_recall_rate_at_k"] == {
        "1": 0.0,
        "2": 0.0,
        "3": 1.0,
    }
    assert report["summary"]["mrr"] == 0.5
    assert report["summary"]["faithfulness"] is None


def test_answer_and_judge_models_are_separated_without_inventing_judge_cost():
    class FakeEmbedder:
        model = "fake-embedding"

    class FakeRetriever:
        size = 1
        backend = object()
        _embedder = FakeEmbedder()

        @staticmethod
        def search(query, top_k):
            return [
                RetrievedChunk(
                    chunk=Chunk(
                        chunk_id="a",
                        doc="政策",
                        section="时效",
                        text="退货时效为 7 天。",
                    ),
                    score=0.99,
                )
            ]

    class FakeCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            content = (
                "退货时效为 7 天。"
                if kwargs["model"] == "gpt-4o-mini"
                else '{"faithful": true, "unsupported_claims": [], "reason": "有依据"}'
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
            )

    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    evaluator = RagEvaluator(
        retriever=FakeRetriever(),
        client=client,
        model="gpt-4o-mini",
        judge_model="gpt-5.6-luna",
        k_values=[1],
        use_judge=True,
        input_cost_per_million=0.15,
        output_cost_per_million=0.60,
    )
    report = evaluator.run_all([
        RagEvalCase(
            id="separate-models",
            query="退货时效？",
            relevant_chunk_ids=["a"],
            expected_keywords=["7天"],
        )
    ])

    assert [call["model"] for call in completions.calls] == [
        "gpt-4o-mini",
        "gpt-5.6-luna",
    ]
    assert "reasoning_effort" not in completions.calls[0]
    assert completions.calls[1]["reasoning_effort"] == "none"
    assert report["summary"]["faithfulness"] == 1.0
    assert report["summary"]["estimated_answer_cost_usd"] > 0
    assert report["summary"]["estimated_judge_cost_usd"] is None
    assert report["summary"]["estimated_cost_usd"] is None
