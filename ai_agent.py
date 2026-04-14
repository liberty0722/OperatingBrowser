# -*- coding: utf-8 -*-
"""AI 代理模块 - 使用 LLM 将自然语言指令转化为浏览器操作序列。"""

import json
import logging
from typing import Any, Optional

from openai import OpenAI

from browser_engine import BrowserEngine

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个浏览器自动化助手。用户会给你一个任务，你需要通过调用工具来操作浏览器完成任务。

## 工作流程
1. 先用 screenshot 或 get_page_info 了解当前页面状态
2. 用 get_elements 获取页面上可交互的元素
3. 根据任务需要，调用 goto / click / type_text / press_key / scroll 等工具操作页面
4. 操作后用 screenshot 确认结果
5. 任务完成后调用 done 工具报告结果

## 注意事项
- CSS 选择器尽量用 id、name、aria-label 等稳定属性
- 如果选择器不确定，先用 get_elements 查看可用元素
- 操作后适当等待页面加载
- 如果操作失败，尝试其他选择器或方法
- 每一步都要确认操作是否成功
"""

MAX_ITERATIONS = 20


class AIAgent:
    """AI 代理：接收自然语言任务，通过 LLM 驱动浏览器完成。"""

    def __init__(
        self,
        browser: BrowserEngine,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
    ) -> None:
        self._browser = browser
        self._model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._messages: list[dict[str, Any]] = []
        self._step_logs: list[dict[str, Any]] = []
        self._is_running = False
        self._should_stop = False

    @property
    def step_logs(self) -> list[dict[str, Any]]:
        return list(self._step_logs)

    @property
    def is_running(self) -> bool:
        return self._is_running

    def stop(self) -> None:
        """请求停止当前任务。"""
        self._should_stop = True

    def run_task(self, task: str, on_step: Optional[callable] = None) -> dict:
        """执行一个自然语言任务。

        Args:
            task: 用户的自然语言指令。
            on_step: 每步回调函数，签名 (step_info: dict) -> None。

        Returns:
            包含 success、summary、steps 的结果字典。
        """
        self._is_running = True
        self._should_stop = False
        self._step_logs = []
        self._messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请完成以下任务：{task}"},
        ]

        tools = BrowserEngine.get_tool_definitions()
        iteration = 0

        try:
            while iteration < MAX_ITERATIONS and not self._should_stop:
                iteration += 1
                logger.info("AI Agent 第 %d 轮推理...", iteration)

                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=self._messages,
                    tools=tools,
                    tool_choice="auto",
                )

                message = response.choices[0].message

                # 无工具调用 — AI 认为任务完成或需要反馈
                if not message.tool_calls:
                    content = message.content or ""
                    self._messages.append({"role": "assistant", "content": content})
                    step_info = {
                        "step": iteration,
                        "type": "message",
                        "content": content,
                    }
                    self._step_logs.append(step_info)
                    if on_step:
                        on_step(step_info)
                    # 没有工具调用说明 AI 认为完成了
                    return {
                        "success": True,
                        "summary": content,
                        "steps": self._step_logs,
                    }

                # 处理工具调用
                self._messages.append(message.model_dump())

                for tool_call in message.tool_calls:
                    func_name = tool_call.function.name
                    try:
                        func_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        func_args = {}

                    # done 工具 —— 任务结束
                    if func_name == "done":
                        summary = func_args.get("summary", "任务完成")
                        tool_result = json.dumps({"status": "done", "summary": summary}, ensure_ascii=False)
                        self._messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_result,
                        })
                        step_info = {
                            "step": iteration,
                            "type": "done",
                            "summary": summary,
                        }
                        self._step_logs.append(step_info)
                        if on_step:
                            on_step(step_info)
                        return {
                            "success": True,
                            "summary": summary,
                            "steps": self._step_logs,
                        }

                    # 执行浏览器操作
                    logger.info("执行操作: %s(%s)", func_name, func_args)
                    action_result = self._browser.execute_action(func_name, func_args)

                    # 截图结果不传 base64 到日志（太大），但传给 LLM
                    log_result = dict(action_result)
                    has_screenshot = False
                    if "base64" in log_result:
                        screenshot_b64 = log_result.pop("base64")
                        has_screenshot = True

                    step_info = {
                        "step": iteration,
                        "type": "action",
                        "action": func_name,
                        "params": func_args,
                        "result": log_result,
                    }
                    self._step_logs.append(step_info)
                    if on_step:
                        on_step(step_info)

                    # 构建工具返回消息
                    if has_screenshot and func_name == "screenshot":
                        # 使用多模态消息将截图传给 LLM
                        tool_content = [
                            {"type": "text", "text": json.dumps(log_result, ensure_ascii=False)},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{screenshot_b64}",
                                    "detail": "low",
                                },
                            },
                        ]
                        self._messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(tool_content, ensure_ascii=False),
                        })
                    else:
                        self._messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(log_result, ensure_ascii=False),
                        })

            # 超出最大迭代次数
            return {
                "success": False,
                "summary": f"任务在 {MAX_ITERATIONS} 步内未完成",
                "steps": self._step_logs,
            }

        except Exception as exc:
            logger.exception("AI Agent 运行出错")
            return {
                "success": False,
                "summary": f"执行出错: {str(exc)}",
                "steps": self._step_logs,
            }
        finally:
            self._is_running = False
