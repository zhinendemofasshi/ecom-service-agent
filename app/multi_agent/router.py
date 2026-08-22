"""意图路由器：分析用户消息，决定分发给哪个子 Agent。"""

from typing import List, Optional

from openai import OpenAI

from app.prompts.agents import ROUTER_PROMPT
from app.config.model_compat import chat_completion_options

VALID_AGENTS = ("presale", "postsale", "complaint")
DEFAULT_AGENT = "postsale"

# 高置信业务词典：优化版路由先用确定性规则，模糊输入才调 LLM。
# 优先级是 complaint > postsale > presale，避免“尺码不合适要退货”
# 被“尺码”误分到售前。
COMPLAINT_SIGNALS = (
    "投诉", "消协", "举报", "维权", "差评", "赔偿", "欺骗",
    "破服务", "垃圾", "太差", "很差", "愤怒", "生气", "不满",
)
POSTSALE_SIGNALS = (
    "订单", "下的单", "物流", "快递", "运单", "发货", "派送", "签收", "到哪",
    "退款", "退货", "换货", "售后", "取消订单", "退换", "到账", "无理由",
)
PRESALE_SIGNALS = (
    "有货", "库存", "售罄", "价格", "多少钱", "规格", "参数", "推荐",
    "适合", "颜色", "尺码", "优惠", "活动", "商品", "购买", "买哪",
    "会员", "积分", "包邮", "运费",
    "nike", "airpods", "小米14", "levi", "戴森", "运动鞋", "牛仔裤",
    "吸尘器", "耳机", "手机", "保护壳",
)
DEFAULT_POSTSALE_SIGNALS = (
    "你好", "在吗", "嗨", "hello", "hi", "谢谢", "再见",
    "客服", "账号", "账户", "登录", "密码",
)


class Router:
    """使用 LLM 对用户意图分类，路由到对应的子 Agent。"""

    def __init__(self, client: OpenAI, model: str, strategy: str = "llm"):
        if strategy not in {"llm", "hybrid"}:
            raise ValueError("strategy 必须是 'llm' 或 'hybrid'")
        self.client = client
        self.model = model
        self.strategy = strategy
        self.last_decision_source = "not_routed"

    def route(self, user_input: str, history: Optional[List[dict]] = None) -> str:
        """返回子 Agent 标识: "presale" / "postsale" / "complaint"。"""
        if self.strategy == "hybrid":
            rule_result = self._route_by_rule(user_input)
            if rule_result is not None:
                self.last_decision_source = "rule"
                return rule_result

        self.last_decision_source = "llm"
        return self._route_by_llm(user_input, history)

    @staticmethod
    def _route_by_rule(user_input: str) -> str | None:
        """返回高置信规则结果；不确定时返回 None 交给 LLM。"""
        text = user_input.strip().lower()
        if any(signal in text for signal in COMPLAINT_SIGNALS):
            return "complaint"
        if "ord-" in text or any(signal in text for signal in POSTSALE_SIGNALS):
            return "postsale"
        if any(signal in text for signal in DEFAULT_POSTSALE_SIGNALS):
            return DEFAULT_AGENT
        if any(signal in text for signal in PRESALE_SIGNALS):
            return "presale"
        return None

    def _route_by_llm(
        self, user_input: str, history: Optional[List[dict]] = None,
    ) -> str:
        recent_context = ""
        if history:
            recent = [
                m for m in history[-4:]
                if m.get("role") in ("user", "assistant")
            ]
            if recent:
                lines = []
                for m in recent:
                    role = "用户" if m["role"] == "user" else "客服"
                    content = m.get("content", "")
                    if content and len(content) < 200:
                        lines.append(f"{role}: {content}")
                if lines:
                    recent_context = "\n最近对话：\n" + "\n".join(lines) + "\n"

        prompt = ROUTER_PROMPT.format(user_input=user_input)
        if recent_context:
            prompt = recent_context + "\n" + prompt

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=10,
            **chat_completion_options(self.model),
        )

        raw = (response.choices[0].message.content or "").strip().lower()

        for agent_key in VALID_AGENTS:
            if agent_key in raw:
                return agent_key

        return DEFAULT_AGENT
