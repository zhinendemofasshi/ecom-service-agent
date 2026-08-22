"""工具注册表：OpenAI function calling schema + 分发执行"""

import json
from typing import Callable

from app.agent.tools.order import query_order
from app.agent.tools.product import query_product
from app.agent.tools.logistics import query_logistics
from app.agent.tools.refund import apply_refund
from app.agent.tools.knowledge import search_knowledge
from app.agent.tools.user_orders import list_user_orders
from app.agent.tools.memory_tool import recall_user_memory
from app.agent.tools.skill_tool import load_skill

_TOOL_MAP: dict[str, Callable] = {
    "query_order": query_order,
    "query_product": query_product,
    "query_logistics": query_logistics,
    "apply_refund": apply_refund,
    "search_knowledge": search_knowledge,
    "list_user_orders": list_user_orders,
    "recall_user_memory": recall_user_memory,
    "load_skill": load_skill,
}

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_order",
            "description": "根据订单号查询订单详情，包括订单状态、商品信息、金额、物流单号等",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "订单号，例如 ORD-20240115-001",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_product",
            "description": "根据商品名称关键词或商品ID查询商品信息，包括价格、库存、规格等。支持模糊搜索",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "商品名称关键词或商品ID，例如「耳机」「运动鞋」「SHOE-270-BK-42」",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_logistics",
            "description": "仅用于用户明确询问快递、运单、配送位置或物流轨迹；根据订单号返回物流信息及未发货/不存在错误",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "订单号，例如 ORD-20240115-001",
                    }
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "检索并夕夕的政策与帮助文档（退换货、配送、会员积分、退款渠道时效、账户安全和密码找回 FAQ）。"
                "当顾客询问规则、流程、时效、是否支持等政策类问题时使用，"
                "比如「能退货吗」「多久到账」「钻石会员有什么权益」「偏远地区包邮吗」。"
                "已有具体订单号的当前订单/退款进度应使用 query_order，不使用本工具。"
                "返回 Top-K 命中片段及来源文档，请基于检索结果回答，不要编造政策"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "用顾客的原问题或一句简洁中文描述要查的政策点",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回片段数，默认 5，最大 5",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_orders",
            "description": (
                "查询当前用户的所有订单概要列表（订单号、状态、商品、金额、下单时间）。"
                "当用户想查订单但未提供订单号，或提供的订单号查不到时，"
                "调用此工具列出订单供用户确认"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_refund",
            "description": (
                "为指定订单申请退款。这是敏感写操作：必须已在此前轮次查询并复述订单，"
                "且用户在新的消息中明确确认订单号和退款原因；首次提出退款的同一轮禁止调用"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "要退款的订单号",
                    },
                    "reason": {
                        "type": "string",
                        "description": "退款原因，例如「尺码不合适」「质量问题」「不想要了」",
                    },
                },
                "required": ["order_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_user_memory",
            "description": (
                "查询当前用户的记忆信息，包括本次对话提取的短期记忆和跨会话的长期记忆。"
                "当需要回顾用户的偏好、历史问题、会员信息等时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "可选的查询关键词，用于过滤记忆内容",
                        "default": "",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "仅为复杂、多步骤且现有系统规则不足的任务加载一份操作指令。"
                "简单订单、物流、商品、FAQ 查询不要调用；同一会话不要重复加载。"
                "可用技能会在系统提示中列出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要加载的技能名称，如 process-return、track-order、product-recommend",
                    }
                },
                "required": ["skill_name"],
            },
        },
    },
]


def execute_tool(name: str, arguments: dict) -> str:
    """根据工具名称分发执行，返回 JSON 字符串结果。"""
    func = _TOOL_MAP.get(name)
    if not func:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    try:
        result = func(**arguments)
    except Exception as e:
        result = {"error": f"工具执行出错: {e}"}
    return json.dumps(result, ensure_ascii=False)
