# -*- coding: utf-8 -*-
"""AI 代理模块 - 使用 LLM 将自然语言指令转化为浏览器/桌面操作序列。"""

import json
import logging
import time
from typing import Any, Optional, Union

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from browser_engine import BrowserEngine
from desktop_engine import DesktopEngine

logger = logging.getLogger(__name__)

# LLM API 调用重试配置
_LLM_MAX_RETRIES = 3
_LLM_RETRY_BASE_DELAY = 1.0  # 指数退避基础秒数
_LLM_REQUEST_TIMEOUT = 60  # 单次请求超时秒数

# 操作模式
MODE_BROWSER = "browser"
MODE_DESKTOP = "desktop"

SYSTEM_PROMPT_BROWSER = """你是一个浏览器自动化助手。用户会给你一个任务，你需要通过调用工具来操作浏览器完成任务。

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

SYSTEM_PROMPT_DESKTOP = """你是一个 Windows 桌面自动化助手。用户会给你一个任务，你需要通过调用工具来操作 Windows 桌面应用完成任务。

## 工作流程
1. 先用 desktop_screenshot 截取桌面截图，了解当前桌面状态
2. 用 list_windows 查看当前打开的窗口
3. 用 focus_window 聚焦目标窗口
4. 用 list_controls 查看窗口中的控件（按钮、输入框等）
5. 通过 click_control / type_in_control 操作控件，或通过 mouse_click / keyboard_type 进行坐标级操作
6. 操作后用 desktop_screenshot 截图确认结果
7. 任务完成后调用 done 工具报告结果

## 两种操作方式
### 控件级操作（推荐，更稳定）
- focus_window → list_controls → click_control / type_in_control
- 通过控件类型(Button/Edit/Text)、名称(name)、自动化ID(auto_id) 定位

### 坐标级操作（通用，任何应用都可用）
- desktop_screenshot 查看界面 → mouse_click(x, y) / keyboard_type(text)
- 适合没有标准 UI 控件的应用

## 注意事项
- 操作窗口控件前，必须先 focus_window 聚焦目标窗口
- 优先使用控件操作 (click_control)，如果找不到控件再用坐标点击 (mouse_click)
- 中文输入会自动通过剪贴板粘贴
- launch_app 可以启动新应用（如 notepad, calc, mspaint 或完整路径）
- keyboard_hotkey 可以按组合键（如 ['ctrl','s'] 保存、['alt','f4'] 关闭）
- 每一步操作后截图确认结果
"""

# 兼容旧代码
SYSTEM_PROMPT = SYSTEM_PROMPT_BROWSER

MAX_ITERATIONS = 20


class AIAgent:
    """AI 代理：接收自然语言任务，通过 LLM 驱动浏览器或桌面完成。"""

    # 已知支持 vision（多模态图片）的模型关键字
    _VISION_MODELS = {"gpt-4o", "gpt-4-vision", "gpt-4-turbo", "claude-3", "qwen-vl", "qwen3-omni", "glm-4v"}

    def __init__(
        self,
        browser: Optional[BrowserEngine] = None,
        desktop: Optional[DesktopEngine] = None,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        mode: str = MODE_BROWSER,
    ) -> None:
        self._browser = browser
        self._desktop = desktop
        self._mode = mode
        self._model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=_LLM_REQUEST_TIMEOUT)
        self._messages: list[dict[str, Any]] = []
        self._step_logs: list[dict[str, Any]] = []
        self._is_running = False
        self._should_stop = False

    @property
    def _supports_vision(self) -> bool:
        """判断当前模型是否支持多模态图片输入。"""
        model_lower = self._model.lower()
        return any(v in model_lower for v in self._VISION_MODELS)

    @property
    def step_logs(self) -> list[dict[str, Any]]:
        return list(self._step_logs)

    @property
    def is_running(self) -> bool:
        return self._is_running

    def stop(self) -> None:
        """请求停止当前任务。"""
        self._should_stop = True

    def _get_engine(self):
        """获取当前模式对应的操作引擎。"""
        if self._mode == MODE_DESKTOP:
            return self._desktop
        return self._browser

    def run_task(self, task: str, on_step: Optional[callable] = None, user_images: list = None) -> dict:
        """执行一个自然语言任务。

        Args:
            task: 用户的自然语言指令。
            on_step: 每步回调函数，签名 (step_info: dict) -> None。
            user_images: 用户提供的图片列表（data:image/xxx;base64,... 格式），可选。

        Returns:
            包含 success、summary、steps 的结果字典。
        """
        if user_images is None:
            user_images = []
        self._is_running = True
        self._should_stop = False
        self._step_logs = []

        # 根据模式选择提示词和工具定义
        if self._mode == MODE_DESKTOP:
            system_prompt = SYSTEM_PROMPT_DESKTOP
            from desktop_engine import DesktopEngine
            tools = DesktopEngine.get_tool_definitions()
        else:
            system_prompt = SYSTEM_PROMPT_BROWSER
            tools = BrowserEngine.get_tool_definitions()

        # 构建用户消息：纯文本 or 图文多模态
        if user_images and self._supports_vision:
            user_content = [
                {"type": "text", "text": f"请完成以下任务：{task}"},
            ]
            for img in user_images:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": img, "detail": "low"},
                })
        elif user_images:
            # 模型不支持 vision，忽略图片并提示
            logger.warning("当前模型 %s 不支持图片输入，已忽略 %d 张图片", self._model, len(user_images))
            if on_step:
                on_step({
                    "step": 0, "type": "warning",
                    "content": f"当前模型 {self._model} 不支持图片输入，已忽略 {len(user_images)} 张图片，仅处理文本指令",
                })
            user_content = f"请完成以下任务：{task}"
        else:
            user_content = f"请完成以下任务：{task}"

        self._messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        iteration = 0

        try:
            while iteration < MAX_ITERATIONS and not self._should_stop:
                iteration += 1
                logger.info("AI Agent 第 %d 轮推理...", iteration)

                for _attempt in range(1, _LLM_MAX_RETRIES + 1):
                    try:
                        response = self._client.chat.completions.create(
                            model=self._model,
                            messages=self._messages,
                            tools=tools,
                            tool_choice="auto",
                        )
                        break
                    except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
                        if _attempt == _LLM_MAX_RETRIES:
                            raise
                        delay = _LLM_RETRY_BASE_DELAY * (2 ** (_attempt - 1))
                        logger.warning(
                            "LLM API 调用失败（第 %d/%d 次），%.1fs 后重试: %s",
                            _attempt, _LLM_MAX_RETRIES, delay, exc,
                        )
                        time.sleep(delay)

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

                    # 执行操作（根据当前模式选择引擎）
                    logger.info("执行操作 [%s]: %s(%s)", self._mode, func_name, func_args)
                    engine = self._get_engine()
                    action_result = engine.execute_action(func_name, func_args)

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
                    screenshot_actions = {"screenshot", "desktop_screenshot", "window_screenshot"}
                    if has_screenshot and func_name in screenshot_actions and self._supports_vision:
                        # 将截图以多模态消息形式传给 LLM
                        tool_content = json.dumps(log_result, ensure_ascii=False)
                        self._messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": [
                                {"type": "text", "text": tool_content},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{screenshot_b64}",
                                        "detail": "low",
                                    },
                                },
                            ],
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
