import json
from pathlib import Path
from typing import Optional

from openai import OpenAI

from app.agent.storage import delete_session, load_session, save_session
from app.agent.summarizer import summarize
from app.config.settings import settings
from app.config.model_compat import chat_completion_options
from app.prompts.customer_service import SYSTEM_PROMPT, STRUCTURED_EXTRACTION_PROMPT
from app.schemas.response import CustomerServiceResponse, IntentType
from app.agent.tools.manager import ToolManager
from app.agent.tools.execution import tool_cache_key


class EcomAgent:
    """电商客服 Agent —— 第八期：Skill 可复用能力模块"""

    def __init__(
        self,
        session_path: Optional[str] = None,
        *,
        client: Optional[OpenAI] = None,
        memory_enabled: Optional[bool] = None,
        mcp_enabled: Optional[bool] = None,
        skills_enabled: Optional[bool] = None,
        temperature: Optional[float] = None,
    ):
        """Create the agent.

        Optional dependency/configuration overrides are primarily used by the
        evaluation sandbox.  They avoid mutating the process-wide ``settings``
        singleton while an isolated case is being constructed.
        """
        self.client = client or OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
        self.model = settings.model_name
        self.temperature = settings.temperature if temperature is None else temperature
        self.session_path = session_path or settings.session_path
        self.history_threshold = settings.history_threshold
        self.history_keep_recent = settings.history_keep_recent
        self.max_react_steps = settings.max_react_steps
        self.runtime_name = settings.agent_runtime

        effective_mcp_enabled = settings.mcp_enabled if mcp_enabled is None else mcp_enabled
        effective_memory_enabled = (
            settings.memory_enabled if memory_enabled is None else memory_enabled
        )
        effective_skills_enabled = (
            settings.skills_enabled if skills_enabled is None else skills_enabled
        )

        self.tool_manager = ToolManager(
            use_mcp=effective_mcp_enabled,
            mcp_server_url=settings.mcp_server_url,
        )

        from app.agent.memory import MemoryManager
        self.memory_manager = MemoryManager(
            client=self.client,
            model=self.model,
            user_id=settings.memory_user_id,
            memory_dir=settings.memory_dir,
            memory_enabled=effective_memory_enabled,
            max_ltm_facts=settings.max_ltm_facts,
        )

        if effective_memory_enabled:
            from app.agent.tools.memory_tool import set_memory_manager
            set_memory_manager(self.memory_manager)

        from app.agent.skills import SkillManager
        self.skill_manager = SkillManager(
            skills_dir=settings.skills_dir,
            enabled=effective_skills_enabled,
        )
        if effective_skills_enabled:
            from app.agent.tools.skill_tool import set_skill_manager
            set_skill_manager(self.skill_manager)

        self.graph_runtime = None
        if self.runtime_name == "langgraph":
            from app.agent.langgraph_runtime import LangGraphAgentRuntime
            self.graph_runtime = LangGraphAgentRuntime(
                client=self.client,
                model=self.model,
                temperature=self.temperature,
                tool_manager=self.tool_manager,
                max_steps=self.max_react_steps,
                checkpoint_path=settings.langgraph_checkpoint_path,
                thread_key=str(Path(self.session_path).resolve()),
                hitl_enabled=settings.hitl_enabled,
            )
        elif self.runtime_name != "legacy":
            raise ValueError("AGENT_RUNTIME 必须是 'langgraph' 或 'legacy'")

        self.raw_messages: list[dict] = []
        self.summary: Optional[str] = None

        loaded = load_session(self.session_path)
        if loaded:
            self.summary = loaded["summary"]
            self.raw_messages = loaded["messages"]
            if loaded.get("short_term_memory"):
                self.memory_manager.restore_stm(loaded["short_term_memory"])

    @property
    def history_size(self) -> int:
        return len(self.raw_messages)

    def chat(self, user_input: str) -> CustomerServiceResponse:
        """处理用户输入：ReAct 循环 → 结构化提取 → 返回结果"""
        if self.has_pending_approval:
            return self._pending_approval_response()

        self.raw_messages.append({"role": "user", "content": user_input})

        if self.graph_runtime is not None:
            graph_result = self.graph_runtime.invoke(self._build_messages())
            if graph_result.get("__interrupt__"):
                save_session(
                    self.session_path, self.raw_messages, self.summary,
                    short_term_memory=self.memory_manager.stm_to_dict(),
                )
                return self._pending_approval_response()
            final_text = graph_result.get("final_text", "")
            self._append_graph_messages(graph_result)
        else:
            final_text = self._react_loop()

        return self._finish_chat(final_text)

    @property
    def has_pending_approval(self) -> bool:
        return bool(
            self.graph_runtime is not None
            and self.graph_runtime.has_pending_interrupt()
        )

    def resume_approval(
        self,
        approved: bool,
        *,
        arguments: Optional[dict] = None,
        message: Optional[str] = None,
    ) -> CustomerServiceResponse:
        """Resume a graph paused before a sensitive tool call."""
        if not self.has_pending_approval or self.graph_runtime is None:
            raise RuntimeError("当前没有待审核操作")
        if arguments is not None:
            decision = {"type": "edit", "arguments": arguments}
        elif approved:
            decision = {"type": "approve"}
        else:
            decision = {"type": "reject", "message": message or "用户拒绝了该操作"}
        graph_result = self.graph_runtime.resume([decision])
        if graph_result.get("__interrupt__"):
            return self._pending_approval_response()
        final_text = graph_result.get("final_text", "")
        self._append_graph_messages(graph_result)
        return self._finish_chat(final_text)

    def _finish_chat(self, final_text: str) -> CustomerServiceResponse:
        """Apply the existing response, memory, compression, and persistence path."""

        result = self._extract_structured_response(final_text)

        self.memory_manager.update_short_term(self.raw_messages[-6:])

        self.raw_messages.append(
            {"role": "assistant", "content": result.model_dump_json(ensure_ascii=False)}
        )

        if len(self.raw_messages) > self.history_threshold:
            self._compress_history()

        save_session(
            self.session_path, self.raw_messages, self.summary,
            short_term_memory=self.memory_manager.stm_to_dict(),
        )
        return result

    def _append_graph_messages(self, graph_result: dict) -> None:
        """Persist graph output in the legacy message format used by memory/evals."""
        for item in graph_result.get("tool_history", []):
            self.raw_messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item["call_id"],
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": item["arguments"],
                    },
                }],
            })
            self.raw_messages.append({
                "role": "tool",
                "tool_call_id": item["call_id"],
                "content": item["output"],
            })
        if graph_result.get("final_text"):
            self.raw_messages.append({
                "role": "assistant",
                "content": graph_result["final_text"],
            })

    @staticmethod
    def _pending_approval_response() -> CustomerServiceResponse:
        return CustomerServiceResponse(
            intent=IntentType.RETURN_REQUEST,
            confidence=1.0,
            reply="退款申请已准备好，执行前需要人工确认。请输入 approve 批准或 reject 拒绝。",
            requires_human=True,
            follow_up_question="是否批准执行退款操作？",
        )

    def reset(self):
        self.raw_messages = []
        self.summary = None
        self.memory_manager.reset_short_term()
        if self.graph_runtime is not None:
            self.graph_runtime.reset()
        delete_session(self.session_path)

    def save(self) -> None:
        save_session(
            self.session_path, self.raw_messages, self.summary,
            short_term_memory=self.memory_manager.stm_to_dict(),
        )

    def close(self):
        self.memory_manager.consolidate_to_long_term(self.raw_messages, self.summary)
        if self.graph_runtime is not None:
            self.graph_runtime.close()
        self.tool_manager.close()

    def _react_loop(self) -> str:
        """ReAct 循环：调用 LLM → 执行工具 → 观察结果 → 重复，直到模型给出最终回答。"""
        tool_cache: dict[str, str] = {}
        for step in range(self.max_react_steps):
            messages = self._build_messages()

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                tools=self.tool_manager.tool_definitions,
                **chat_completion_options(self.model),
            )
            choice = response.choices[0]
            assistant_msg = choice.message

            if assistant_msg.content:
                self._print_thought(assistant_msg.content)

            if not assistant_msg.tool_calls:
                content = assistant_msg.content or ""
                self.raw_messages.append({"role": "assistant", "content": content})
                return content

            msg_dict = {"role": "assistant", "content": assistant_msg.content}
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
            self.raw_messages.append(msg_dict)

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

                self.raw_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

        messages = self._build_messages()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            **chat_completion_options(self.model),
        )
        content = response.choices[0].message.content or ""
        self.raw_messages.append({"role": "assistant", "content": content})
        return content

    def _extract_structured_response(self, text: str) -> CustomerServiceResponse:
        """从最终文本中提取结构化元数据（意图、置信度等）。"""
        try:
            response = self.client.beta.chat.completions.parse(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": STRUCTURED_EXTRACTION_PROMPT,
                    },
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                response_format=CustomerServiceResponse,
                **chat_completion_options(self.model),
            )
            return response.choices[0].message.parsed
        except Exception:
            return self._extract_structured_fallback(text)

    def _extract_structured_fallback(self, text: str) -> CustomerServiceResponse:
        """当 response_format 不被 API 支持时，用 prompt 引导 JSON 输出。"""
        intent_values = ", ".join(f'"{e.value}"' for e in IntentType)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{STRUCTURED_EXTRACTION_PROMPT}\n\n输出 JSON。\n\n"
                        "必须严格按照以下 JSON 格式输出（不要加 markdown 代码块）：\n"
                        "{\n"
                        f'  "intent": <从以下选择: {intent_values}>,\n'
                        '  "confidence": <0.0到1.0的浮点数>,\n'
                        '  "reply": <原文回复内容>,\n'
                        '  "requires_human": <true或false>,\n'
                        '  "follow_up_question": <追问问题或null>\n'
                        "}"
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            **chat_completion_options(self.model),
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return CustomerServiceResponse.model_validate_json(raw)

    def _build_messages(self) -> list[dict]:
        system_content = SYSTEM_PROMPT
        if self.skill_manager and self.skill_manager.enabled:
            system_content += self.skill_manager.build_catalog_prompt()

        messages: list[dict] = [
            {"role": "system", "content": system_content}
        ]
        messages.extend(self.memory_manager.build_memory_prompt_sections())
        if self.summary:
            messages.append(
                {
                    "role": "system",
                    "content": f"以下是此前对话的摘要，用于延续上下文记忆：\n{self.summary}",
                }
            )
        messages.extend(self.raw_messages)
        return messages

    def _compress_history(self) -> None:
        keep = self.history_keep_recent
        split = len(self.raw_messages) - keep
        while split > 0 and self.raw_messages[split].get("role") in ("tool",):
            split -= 1
        if split <= 0:
            return
        old_messages = self.raw_messages[:split]
        recent = self.raw_messages[split:]

        new_summary = summarize(
            client=self.client,
            model=self.model,
            old_messages=old_messages,
            prev_summary=self.summary,
        )
        self.summary = new_summary
        self.raw_messages = recent
        print(
            f"\n💾 [已压缩 {len(old_messages)} 条老消息 → summary "
            f"({len(new_summary)} 字)]\n"
        )

    def _print_thought(self, text: str) -> None:
        print(f"\n💭 [思考] {text}")

    def _print_action(self, func_name: str, func_args: dict) -> None:
        args_str = ", ".join(f"{k}={v!r}" for k, v in func_args.items())
        print(f"🔧 [调用工具] {func_name}({args_str})")

    def _print_cache_hit(self, func_name: str, func_args: dict) -> None:
        args_str = ", ".join(f"{k}={v!r}" for k, v in func_args.items())
        print(f"♻️  [复用结果] {func_name}({args_str})")

    def _print_observation(self, result: str) -> None:
        display = result if len(result) <= 300 else result[:300] + "..."
        print(f"📋 [工具结果] {display}")
