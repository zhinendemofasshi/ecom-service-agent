"""评估数据集：测试用例的数据结构与加载（第9期）。

一条 EvalCase 描述「输入是什么」+「期望表现是什么」。期望分两类：
- 结果期望：末轮回复应识别的意图、应命中的关键词、是否该转人工。
- 过程期望：应调用哪些工具、理论最少调用次数、token 预算、（多 Agent）期望路由。

留空的期望项在评分时会被跳过（不计分），而不是判 0，避免无工具/无关键词的
用例（如问候、投诉）拖垮聚合均值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EvalCase:
    """单条评估用例。turns 支持多轮，按顺序喂给同一个 agent 实例。"""

    id: str
    description: str
    turns: list[str]  # 多轮输入，单轮即 len==1
    category: str = "general"  # 用于覆盖率与分类汇总

    # ---------- 结果期望 ----------
    # 混合诉求可列出多个合理意图，命中任一即通过。
    expected_intent: str | list[str] | None = None
    # 每项可以是字符串，也可以是一组等价表述；组内命中任意一个即算该事实命中。
    expected_keywords: list[str | list[str]] = field(default_factory=list)
    expected_requires_human: bool | None = None  # 是否应转人工，None=不校验

    # ---------- 过程期望 ----------
    expected_tools: list[str] = field(default_factory=list)  # 整个会话应调用过的工具
    optional_tools: list[str] = field(default_factory=list)  # 允许但不强制调用，不降低 precision
    min_tool_calls: int | None = None  # 理论最少工具调用次数（算效率用），None=不校验
    max_tool_calls: int | None = None  # 允许的调用次数上限；用于复合问题/可选工具
    max_tokens: int | None = None  # token 预算上限，None=不设上限
    expected_route: str | None = None  # 多 Agent 期望路由（presale/postsale/complaint），可选


def load_dataset(path: str | Path) -> list[EvalCase]:
    """从 JSON 文件加载用例列表，文件格式为 {"cases": [ {EvalCase 字段}, ... ]}。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [EvalCase(**item) for item in data["cases"]]
    ids = [case.id for case in cases]
    duplicate_ids = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicate_ids:
        raise ValueError(f"评估用例 id 重复: {', '.join(duplicate_ids)}")
    for case in cases:
        if not case.id.strip() or not case.turns or any(not turn.strip() for turn in case.turns):
            raise ValueError(f"评估用例缺少有效 id/turns: {case.id!r}")
    return cases
