"""子 Agent 定义：每个子 Agent 有专属的 system prompt 和工具子集。

SubAgent 封装了一个轻量级 ReAct 循环，由 Orchestrator 调度执行。
"""

import json

from openai import OpenAI

from app.prompts.agents import COMPLAINT_PROMPT, POSTSALE_PROMPT, PRESALE_PROMPT
from app.agent.tools.manager import ToolManager
from app.agent.tools.execution import tool_cache_key
from app.config.model_compat import chat_completion_options


AGENT_CONFIGS = {
    "presale": {
        "name": "小夕-售前",
        "prompt": PRESALE_PROMPT,
        "tools": {"query_product", "search_knowledge", "list_user_orders", "load_skill"},
    },
    "postsale": {
        "name": "小夕-售后",
        "prompt": POSTSALE_PROMPT,
        "tools": {
            "query_order", "query_logistics", "apply_refund",
            "list_user_orders", "search_knowledge", "load_skill",
        },
    },
    "complaint": {
        "name": "小夕-投诉",
        "prompt": COMPLAINT_PROMPT,
        "tools": {"query_order", "search_knowledge"},
    },
}


class SubAgent:
    """专业子 Agent：拥有独立的 prompt 和工具子集，执行 ReAct 循环。"""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        tool_manager: ToolManager,
        client: OpenAI,
        model: str,
        temperature: float,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.tool_manager = tool_manager
        self.client = client
        self.model = model
        self.temperature = temperature

    def handle(
        self, messages: list[dict], max_steps: int = 5,
    ) -> tuple[str, list[dict]]:
        """执行 ReAct 循环，返回 (最终文本, 新增消息列表)。"""
        new_messages: list[dict] = []
        working = list(messages)
        tool_cache: dict[str, str] = {}

        for _ in range(max_steps):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=working,
                temperature=self.temperature,
                tools=self.tool_manager.tool_definitions,
                **chat_completion_options(self.model),
            )
            assistant_msg = response.choices[0].message

            if assistant_msg.content:
                self._print_thought(assistant_msg.content)

            if not assistant_msg.tool_calls:
                content = assistant_msg.content or ""
                msg = {"role": "assistant", "content": content}
                new_messages.append(msg)
                return content, new_messages

            msg_dict: dict = {"role": "assistant", "content": assistant_msg.content}
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in assistant_msg.tool_calls
            ]
            new_messages.append(msg_dict)
            working.append(msg_dict)

            for tc in assistant_msg.tool_calls:
                func_name = tc.function.name
                func_args = json.loads(tc.function.arguments)

                cache_key = tool_cache_key(func_name, func_args)
                if cache_key is not None and cache_key in tool_cache:
                    self._print_cache_hit(func_name, func_args)
                    result_str = tool_cache[cache_key]
                else:
                    self._print_action(func_name, func_args)
                    result_str = self.tool_manager.execute_tool(func_name, func_args)
                    if cache_key is not None:
                        tool_cache[cache_key] = result_str
                    else:
                        tool_cache.clear()
                self._print_observation(result_str)

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                }
                new_messages.append(tool_msg)
                working.append(tool_msg)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=working,
            temperature=self.temperature,
            **chat_completion_options(self.model),
        )
        content = response.choices[0].message.content or ""
        new_messages.append({"role": "assistant", "content": content})
        return content, new_messages

    def _print_thought(self, text: str) -> None:
        print(f"\n  💭 [{self.name}·思考] {text}")

    def _print_action(self, func_name: str, func_args: dict) -> None:
        args_str = ", ".join(f"{k}={v!r}" for k, v in func_args.items())
        print(f"  🔧 [{self.name}·调用工具] {func_name}({args_str})")

    def _print_cache_hit(self, func_name: str, func_args: dict) -> None:
        args_str = ", ".join(f"{k}={v!r}" for k, v in func_args.items())
        print(f"  ♻️  [{self.name}·复用结果] {func_name}({args_str})")

    def _print_observation(self, result: str) -> None:
        display = result if len(result) <= 300 else result[:300] + "..."
        print(f"  📋 [{self.name}·工具结果] {display}")
