# -*- coding: utf-8 -*-
"""工作流管理器 — 录制、保存、加载、回放浏览器操作序列。"""

import copy
import io
import json
import logging
import os
import re
import threading
import time
import urllib.request
import uuid
import zipfile
from typing import Any, Callable, Optional

import chardet
from openai import OpenAI

logger = logging.getLogger(__name__)

# 工作流存储目录
WORKFLOW_DIR = os.path.join(os.path.dirname(__file__), "workflows")

# 执行历史存储目录
EXECUTION_HISTORY_DIR = os.path.join(os.path.dirname(__file__), "execution_history")

# 文件读写锁，防止并发操作冲突（使用 RLock 以支持同一线程重入）
_file_lock = threading.RLock()

# 回放时需要执行的操作类型（过滤掉 screenshot / get_elements / get_page_info 等只读操作）
PLAYABLE_ACTIONS = frozenset({
    # 浏览器操作
    "goto", "click", "type_text", "press_key", "scroll",
    "execute_js", "wait", "back", "forward",
    "hover", "select_option",
    "new_tab", "switch_tab", "close_tab",
    # 桌面操作
    "mouse_click", "mouse_double_click", "mouse_right_click",
    "mouse_move", "mouse_drag", "mouse_scroll",
    "keyboard_type", "keyboard_press", "keyboard_hotkey",
    "launch_app", "focus_window", "maximize_window", "minimize_window",
    "close_window", "resize_window", "move_window",
    "click_control", "type_in_control",
    "desktop_wait", "click_image",
})

# 回放时跳过的操作（信息采集类，不产生副作用）
SKIP_ACTIONS = frozenset({
    "screenshot", "get_elements", "get_page_info", "get_text",
    "desktop_screenshot", "list_windows", "list_controls",
    "find_control", "get_control_text", "get_screen_size",
    "get_mouse_position", "get_pixel_color", "locate_on_screen",
})


def _ensure_dir() -> None:
    os.makedirs(WORKFLOW_DIR, exist_ok=True)


def _ensure_history_dir() -> None:
    os.makedirs(EXECUTION_HISTORY_DIR, exist_ok=True)


def _workflow_path(workflow_id: str) -> str:
    return os.path.join(WORKFLOW_DIR, f"{workflow_id}.json")


def _history_path(execution_id: str) -> str:
    return os.path.join(EXECUTION_HISTORY_DIR, f"{execution_id}.json")


# ------------------------------------------------------------------
# 执行历史 CRUD
# ------------------------------------------------------------------

def save_execution_record(
    workflow_id: str,
    workflow_name: str,
    status: str,
    steps: list[dict[str, Any]],
    start_time: float,
    end_time: float,
    error: Optional[str] = None,
    replay_type: str = "simple",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """保存一条执行记录。"""
    _ensure_history_dir()
    execution_id = uuid.uuid4().hex[:12]
    record = {
        "id": execution_id,
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "status": status,
        "replay_type": replay_type,
        "steps_count": len(steps),
        "start_time": start_time,
        "end_time": end_time,
        "duration": round(end_time - start_time, 2),
        "error": error,
        "steps": steps,
    }
    if extra:
        record.update(extra)
    with _file_lock:
        with open(_history_path(execution_id), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    logger.info("执行记录已保存: %s (%s) — %s", workflow_name, execution_id, status)
    return record


def list_execution_history(
    workflow_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出执行历史（不含 steps 详情）。"""
    _ensure_history_dir()
    records: list[dict[str, Any]] = []
    with _file_lock:
        for filename in os.listdir(EXECUTION_HISTORY_DIR):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(EXECUTION_HISTORY_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    rec = json.load(f)
                if workflow_id and rec.get("workflow_id") != workflow_id:
                    continue
                records.append({
                    "id": rec["id"],
                    "workflow_id": rec.get("workflow_id", ""),
                    "workflow_name": rec.get("workflow_name", ""),
                    "status": rec.get("status", "unknown"),
                    "replay_type": rec.get("replay_type", "simple"),
                    "steps_count": rec.get("steps_count", 0),
                    "start_time": rec.get("start_time", 0),
                    "end_time": rec.get("end_time", 0),
                    "duration": rec.get("duration", 0),
                    "error": rec.get("error"),
                })
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("跳过损坏的执行记录 %s: %s", filename, exc)
    records.sort(key=lambda r: r["end_time"], reverse=True)
    return records[:limit]


def get_execution_record(execution_id: str) -> Optional[dict[str, Any]]:
    """获取单条执行记录的完整信息（含 steps）。"""
    filepath = _history_path(execution_id)
    if not os.path.isfile(filepath):
        return None
    with _file_lock:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)


def delete_execution_record(execution_id: str) -> bool:
    """删除一条执行记录。"""
    filepath = _history_path(execution_id)
    with _file_lock:
        if not os.path.isfile(filepath):
            return False
        os.remove(filepath)
    logger.info("执行记录已删除: %s", execution_id)
    return True


def clear_execution_history(workflow_id: Optional[str] = None) -> int:
    """清空执行历史，返回删除数量。可选按 workflow_id 过滤。"""
    _ensure_history_dir()
    deleted = 0
    with _file_lock:
        for filename in os.listdir(EXECUTION_HISTORY_DIR):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(EXECUTION_HISTORY_DIR, filename)
            if workflow_id:
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                    if rec.get("workflow_id") != workflow_id:
                        continue
                except (json.JSONDecodeError, KeyError):
                    pass
            os.remove(filepath)
            deleted += 1
    logger.info("已清空执行历史: %d 条", deleted)
    return deleted


# ------------------------------------------------------------------
# 智能等待策略 — 根据操作类型选择最优等待方式
# ------------------------------------------------------------------

# 操作类型 → 等待策略映射
_WAIT_STRATEGIES: dict[str, dict[str, Any]] = {
    "goto": {"method": "networkidle", "timeout": 10000, "fallback": 2.0},
    "click": {"method": "domcontentloaded", "timeout": 3000, "fallback": 0.5},
    "type_text": {"method": "fixed", "delay": 0.1},
    "press_key": {"method": "fixed", "delay": 0.2},
    "scroll": {"method": "fixed", "delay": 0.3},
    "select_option": {"method": "domcontentloaded", "timeout": 2000, "fallback": 0.3},
    "hover": {"method": "fixed", "delay": 0.2},
    "execute_js": {"method": "fixed", "delay": 0.3},
    "back": {"method": "networkidle", "timeout": 5000, "fallback": 1.0},
    "forward": {"method": "networkidle", "timeout": 5000, "fallback": 1.0},
}
_DEFAULT_WAIT = {"method": "fixed", "delay": 0.3}


def smart_wait(browser_engine: Any, action: str) -> None:
    """根据操作类型执行智能等待，替代固定 time.sleep。

    - goto/back/forward → 等待 networkidle（页面网络空闲）
    - click/select_option → 等待 domcontentloaded（DOM 更新完成）
    - type_text/press_key/scroll → 短固定延迟
    """
    strategy = _WAIT_STRATEGIES.get(action, _DEFAULT_WAIT)
    method = strategy.get("method", "fixed")

    if method == "fixed":
        time.sleep(strategy.get("delay", 0.3))
        return

    # 尝试使用 Playwright 的页面等待方法
    try:
        page = getattr(browser_engine, "_page", None) or getattr(browser_engine, "page", None)
        if page is None:
            time.sleep(strategy.get("fallback", 0.3))
            return
        timeout = strategy.get("timeout", 3000)
        page.wait_for_load_state(method, timeout=timeout)
    except Exception:
        # 等待超时或页面无法访问，使用 fallback 延迟
        time.sleep(strategy.get("fallback", 0.3))


# ------------------------------------------------------------------
# 从 AI 任务步骤中提取可回放的操作序列
# ------------------------------------------------------------------

def extract_playable_steps(step_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 AI Agent 的 step_logs 中提取可回放的操作步骤。

    只保留会产生副作用的操作（goto、click、type_text 等），
    跳过 screenshot、get_elements 等纯读取操作。

    Args:
        step_logs: AIAgent.run_task 返回的 steps 列表。

    Returns:
        精简后的可回放步骤列表。
    """
    playable = []
    for step in step_logs:
        if step.get("type") != "action":
            continue
        action = step.get("action", "")
        if action not in PLAYABLE_ACTIONS:
            continue
        # 检查操作是否成功（有 error 字段说明失败了，不录入）
        result = step.get("result", {})
        if result.get("error"):
            continue
        playable.append({
            "action": action,
            "params": step.get("params", {}),
        })
    return playable


# ------------------------------------------------------------------
# CRUD
# ------------------------------------------------------------------

def save_workflow(
    name: str,
    task: str,
    steps: list[dict[str, Any]],
    description: str = "",
    replay_config: Optional[dict[str, Any]] = None,
    mode: str = "browser",
) -> dict[str, Any]:
    """保存一个工作流。"""
    _ensure_dir()
    workflow_id = uuid.uuid4().hex[:12]
    workflow = {
        "id": workflow_id,
        "name": name,
        "task": task,
        "description": description,
        "steps": steps,
        "replay_config": replay_config,
        "mode": mode,
        "created_at": time.time(),
        "updated_at": time.time(),
        "run_count": 0,
    }
    with _file_lock:
        with open(_workflow_path(workflow_id), "w", encoding="utf-8") as f:
            json.dump(workflow, f, ensure_ascii=False, indent=2)
    logger.info("工作流已保存: %s (%s)", name, workflow_id)
    return workflow


def list_workflows() -> list[dict[str, Any]]:
    """列出所有已保存的工作流（不含 steps 详情）。"""
    _ensure_dir()
    workflows = []
    with _file_lock:
        for filename in os.listdir(WORKFLOW_DIR):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(WORKFLOW_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    wf = json.load(f)
                workflows.append({
                    "id": wf["id"],
                    "name": wf["name"],
                    "task": wf["task"],
                    "description": wf.get("description", ""),
                    "steps_count": len(wf.get("steps", [])),
                    "created_at": wf.get("created_at", 0),
                    "updated_at": wf.get("updated_at", 0),
                    "run_count": wf.get("run_count", 0),
                    "replay_config": wf.get("replay_config"),
                })
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("跳过损坏的工作流文件 %s: %s", filename, exc)
    workflows.sort(key=lambda w: w["updated_at"], reverse=True)
    return workflows


def get_workflow(workflow_id: str) -> Optional[dict[str, Any]]:
    """获取单个工作流的完整信息（含 steps）。"""
    filepath = _workflow_path(workflow_id)
    if not os.path.isfile(filepath):
        return None
    with _file_lock:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)


def delete_workflow(workflow_id: str) -> bool:
    """删除一个工作流。"""
    filepath = _workflow_path(workflow_id)
    with _file_lock:
        if not os.path.isfile(filepath):
            return False
        os.remove(filepath)
    logger.info("工作流已删除: %s", workflow_id)
    return True


def update_workflow(workflow_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    """更新工作流的可编辑字段（name、description、steps、replay_config）。"""
    wf = get_workflow(workflow_id)
    if wf is None:
        return None
    for key in ("name", "description", "steps", "replay_config"):
        if key in updates:
            wf[key] = updates[key]
    if "steps" in updates:
        wf["steps_count"] = len(updates["steps"])
    wf["updated_at"] = time.time()
    with _file_lock:
        with open(_workflow_path(workflow_id), "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False, indent=2)
    return wf


def clone_workflow(workflow_id: str) -> Optional[dict[str, Any]]:
    """克隆一个工作流（深拷贝，生成新 ID，名称加"(副本)"后缀）。"""
    wf = get_workflow(workflow_id)
    if wf is None:
        return None
    _ensure_dir()
    new_id = uuid.uuid4().hex[:12]
    cloned = {
        "id": new_id,
        "name": wf["name"] + "（副本）",
        "task": wf.get("task", ""),
        "description": wf.get("description", ""),
        "steps": json.loads(json.dumps(wf.get("steps", []))),
        "replay_config": json.loads(json.dumps(wf.get("replay_config"))) if wf.get("replay_config") else None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "run_count": 0,
    }
    with _file_lock:
        with open(_workflow_path(new_id), "w", encoding="utf-8") as f:
            json.dump(cloned, f, ensure_ascii=False, indent=2)
    logger.info("工作流已克隆: %s → %s (%s)", wf["name"], cloned["name"], new_id)
    return cloned


def _increment_run_count(workflow_id: str) -> None:
    """回放成功后增加运行次数。"""
    wf = get_workflow(workflow_id)
    if wf is None:
        return
    wf["run_count"] = wf.get("run_count", 0) + 1
    wf["updated_at"] = time.time()
    with _file_lock:
        with open(_workflow_path(workflow_id), "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------
# 回放引擎
# ------------------------------------------------------------------

# 需要 CSS 选择器的操作类型
_SELECTOR_ACTIONS = frozenset({"click", "type_text", "get_text", "hover", "select_option"})

# 智能降级时的 LLM 系统提示
_FALLBACK_SYSTEM_PROMPT = """你是一个浏览器自动化修复助手。回放录制的工作流时，某个步骤的 CSS 选择器失效了。
你需要根据当前页面的可交互元素列表，找到与原始操作意图最匹配的新选择器。

规则：
1. 优先使用 id 选择器（如 #searchInput）
2. 其次使用 name 属性（如 [name="q"]）
3. 再用 aria-label、placeholder 等语义属性
4. 最后使用 tag + text 组合
5. 只返回一个 JSON 对象，不要解释

返回格式（严格 JSON）：
{"selector": "新的CSS选择器", "confidence": 0.9, "reason": "简短说明"}

如果找不到匹配元素，返回：
{"selector": null, "confidence": 0, "reason": "未找到匹配元素"}
"""


def replay_workflow(
    workflow_id: str,
    browser_engine: Any,
    variables: Optional[dict[str, str]] = None,
    on_step: Optional[Callable[[dict], None]] = None,
    ai_config: Optional[dict[str, str]] = None,
    stop_event: Optional[threading.Event] = None,
    max_retries: int = 2,
    retry_delay: float = 1.0,
) -> dict[str, Any]:
    """回放一个已保存的工作流。

    Args:
        max_retries: 每个步骤失败后的最大重试次数（默认 2）。
        retry_delay: 两次重试之间的等待秒数（默认 1.0）。
    """
    wf = get_workflow(workflow_id)
    if wf is None:
        return {"success": False, "error": "工作流不存在"}

    steps = wf.get("steps", [])
    if not steps:
        return {"success": False, "error": "工作流没有可回放的步骤"}

    # 从 replay_config 读取重试配置（如有）
    rc = wf.get("replay_config") or {}
    max_retries = rc.get("max_retries", max_retries)
    retry_delay = rc.get("retry_delay", retry_delay)

    variables = variables or {}
    results = []
    total = len(steps)
    ai_fix_count = 0
    start_time = time.time()

    for i, step in enumerate(steps):
        # 检查停止信号
        if stop_event and stop_event.is_set():
            end_time = time.time()
            result_data = {
                "success": False,
                "error": "回放已被用户手动停止",
                "steps": results,
                "stopped_at": i + 1,
                "ai_fix_count": ai_fix_count,
                "cancelled": True,
            }
            save_execution_record(
                workflow_id=workflow_id,
                workflow_name=wf.get("name", ""),
                status="cancelled",
                steps=results,
                start_time=start_time,
                end_time=end_time,
                error="用户手动停止",
            )
            return result_data

        action = step["action"]
        params = _apply_variables(step.get("params", {}), variables)

        step_info = {
            "step": i + 1,
            "total": total,
            "type": "replay_action",
            "action": action,
            "params": params,
        }

        # 重试循环
        for attempt in range(1 + max_retries):
            if attempt > 0:
                step_info["retry"] = attempt
                time.sleep(retry_delay)

            # 执行操作
            try:
                result = browser_engine.execute_action(action, params)
                step_info["result"] = result
                step_info["success"] = "error" not in result
            except Exception as exc:
                step_info["result"] = {"error": str(exc)}
                step_info["success"] = False

            # 成功 → 结束重试
            if step_info["success"]:
                break

            # 失败 + 有选择器 + 有 AI 配置 → 智能降级（仅在最后一轮重试时触发）
            if (
                attempt == max_retries
                and action in _SELECTOR_ACTIONS
                and "selector" in params
                and ai_config
            ):
                fix_result = _ai_fallback_fix(
                    browser_engine=browser_engine,
                    action=action,
                    original_params=params,
                    error_msg=step_info["result"].get("error", ""),
                    ai_config=ai_config,
                )

                if fix_result and fix_result.get("new_selector"):
                    new_params = dict(params)
                    new_params["selector"] = fix_result["new_selector"]
                    step_info["ai_fix"] = {
                        "original_selector": params["selector"],
                        "new_selector": fix_result["new_selector"],
                        "confidence": fix_result.get("confidence", 0),
                        "reason": fix_result.get("reason", ""),
                    }

                    try:
                        result = browser_engine.execute_action(action, new_params)
                        step_info["result"] = result
                        step_info["success"] = "error" not in result
                        step_info["params"] = new_params
                        if step_info["success"]:
                            ai_fix_count += 1
                            logger.info(
                                "智能降级成功: 步骤 %d/%d, %s → %s",
                                i + 1, total,
                                params["selector"],
                                fix_result["new_selector"],
                            )
                    except Exception as exc:
                        step_info["result"] = {"error": str(exc)}
                        step_info["success"] = False

        results.append(step_info)
        if on_step:
            on_step(step_info)

        # 仍然失败 → 停止回放
        if not step_info["success"]:
            end_time = time.time()
            error_msg = "步骤 {}/{} ({}) 执行失败: {}".format(
                i + 1, total, action, step_info["result"].get("error", ""),
            )
            save_execution_record(
                workflow_id=workflow_id,
                workflow_name=wf.get("name", ""),
                status="failed",
                steps=results,
                start_time=start_time,
                end_time=end_time,
                error=error_msg,
                extra={"ai_fix_count": ai_fix_count, "stopped_at": i + 1},
            )
            return {
                "success": False,
                "error": error_msg,
                "steps": results,
                "stopped_at": i + 1,
                "ai_fix_count": ai_fix_count,
            }

        # 智能等待 — 根据操作类型选择最优等待方式
        smart_wait(browser_engine, action)

    end_time = time.time()
    _increment_run_count(workflow_id)
    summary = "工作流回放完成，共执行 {} 步".format(total)
    if ai_fix_count:
        summary += "（其中 {} 步由 AI 智能修复）".format(ai_fix_count)

    save_execution_record(
        workflow_id=workflow_id,
        workflow_name=wf.get("name", ""),
        status="success",
        steps=results,
        start_time=start_time,
        end_time=end_time,
        extra={"ai_fix_count": ai_fix_count},
    )

    return {
        "success": True,
        "summary": summary,
        "steps": results,
        "ai_fix_count": ai_fix_count,
    }


def _ai_fallback_fix(
    browser_engine: Any,
    action: str,
    original_params: dict,
    error_msg: str,
    ai_config: dict[str, str],
) -> Optional[dict[str, Any]]:
    """当回放步骤失败时，调用 LLM 分析页面元素并给出新的选择器。"""
    try:
        elements_result = browser_engine.execute_action("get_elements", {})
        elements = elements_result.get("elements", [])
        if not elements:
            logger.warning("智能降级: 页面无可交互元素")
            return None

        page_info = browser_engine.execute_action("get_page_info", {})

        user_message = (
            "回放步骤失败，请帮我找到正确的选择器。\n\n"
            "## 原始操作\n"
            "- 操作类型: {action}\n"
            "- 原始选择器: {selector}\n"
            "- 错误信息: {error}\n"
            "{extra_info}"
            "\n## 当前页面\n"
            "- URL: {url}\n"
            "- 标题: {title}\n"
            "\n## 页面可交互元素\n"
            "```json\n{elements}\n```\n"
            "\n请返回最匹配的新选择器（JSON 格式）。"
        ).format(
            action=action,
            selector=original_params.get("selector", ""),
            error=error_msg,
            extra_info=(
                "- 输入文本: {}\n".format(original_params["text"])
                if action == "type_text" and "text" in original_params
                else ""
            ),
            url=page_info.get("url", ""),
            title=page_info.get("title", ""),
            elements=json.dumps(elements[:30], ensure_ascii=False, indent=2),
        )

        client = OpenAI(
            api_key=ai_config["api_key"],
            base_url=ai_config.get("base_url", "https://api.openai.com/v1"),
        )
        response = client.chat.completions.create(
            model=ai_config.get("model", "gpt-4o"),
            messages=[
                {"role": "system", "content": _FALLBACK_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0,
            max_tokens=200,
        )

        content = response.choices[0].message.content or ""
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1])
        result = json.loads(content)

        new_selector = result.get("selector")
        if not new_selector:
            return None
        return {
            "new_selector": new_selector,
            "confidence": result.get("confidence", 0),
            "reason": result.get("reason", ""),
        }

    except Exception as exc:
        logger.warning("智能降级 LLM 调用失败: %s", exc)
        return None


def _apply_variables(params: dict, variables: dict[str, str]) -> dict:
    """对参数中的字符串值进行变量替换。"""
    if not variables:
        return dict(params)
    replaced = {}
    for key, value in params.items():
        if isinstance(value, str):
            for var_name, var_value in variables.items():
                value = value.replace(var_name, var_value)
        replaced[key] = value
    return replaced


# ------------------------------------------------------------------
# 循环回放引擎 — "教一行，自动处理整个列表"
# ------------------------------------------------------------------

_LOOP_CONDITION_SYSTEM_PROMPT = """你是一个浏览器自动化助手。用户录制了一个对列表中某一行的操作流程。
现在需要你判断：当前这一行是否满足用户描述的执行条件。

你会收到：
1. 用户描述的条件（自然语言）
2. 当前行的文本内容和 HTML 属性信息

请严格按 JSON 格式返回判断结果：
{"match": true, "reason": "简短说明为什么匹配/不匹配"}

如果条件是"所有行都执行"或"无条件执行"，直接返回 {"match": true, "reason": "无条件执行"}。
"""

_LOOP_SELECTOR_SYSTEM_PROMPT = """你是一个浏览器自动化助手。用户录制了对列表中某一行的操作步骤。
现在需要你把这些步骤中的选择器"平移"到列表的另一行。

你会收到：
1. 原始步骤列表（JSON）— 每步包含 action、selector 等
2. 原始操作针对的行元素信息
3. 当前目标行的元素信息（tag、文本、属性、子元素）

请返回调整后的步骤列表（JSON 数组），格式与输入完全相同，只修改 selector 字段。
如果某步不涉及 selector（如 wait、press_key），保持不变。

返回格式（严格 JSON 数组）：
[{"action": "click", "params": {"selector": "新选择器"}}, ...]
"""


def loop_replay_workflow(
    workflow_id: str,
    browser_engine: Any,
    row_selector: str,
    condition: str = "",
    max_rows: int = 100,
    on_step: Optional[Callable[[dict], None]] = None,
    ai_config: Optional[dict[str, str]] = None,
    stop_event: Optional[threading.Event] = None,
) -> dict[str, Any]:
    """循环回放：对列表中每一行执行已录制的操作。"""
    start_time = time.time()
    wf = get_workflow(workflow_id)
    if wf is None:
        return {"success": False, "error": "工作流不存在"}

    template_steps = wf.get("steps", [])
    if not template_steps:
        return {"success": False, "error": "工作流没有可回放的步骤"}

    _llm_client = None
    _llm_model = ""

    def get_llm_client() -> Optional[tuple]:
        nonlocal _llm_client, _llm_model
        if _llm_client is not None:
            return (_llm_client, _llm_model)
        if not ai_config:
            return None
        _llm_client = OpenAI(
            api_key=ai_config["api_key"],
            base_url=ai_config.get("base_url", "https://api.openai.com/v1"),
        )
        _llm_model = ai_config.get("model", "gpt-4o")
        return (_llm_client, _llm_model)

    first_row_index = _detect_template_row_index(template_steps, row_selector)

    results = []
    processed = 0
    skipped = 0
    failed = 0

    use_ai_condition = condition and not _is_simple_condition(condition)
    overview = {
        "type": "loop_overview",
        "row_selector": row_selector,
        "condition": condition or "（无条件，处理所有行）",
        "workflow_name": wf.get("name", ""),
        "template_steps_count": len(template_steps),
        "mode": "AI 条件判断" if use_ai_condition else "本地快速模式",
    }
    if on_step:
        on_step(overview)

    for row_index in range(max_rows):
        # 检查停止信号
        if stop_event and stop_event.is_set():
            summary = "循环回放已被用户手动停止，已处理 {} 行".format(processed)
            end_time = time.time()
            save_execution_record(
                workflow_id=workflow_id,
                workflow_name=wf.get("name", ""),
                status="cancelled",
                steps=results,
                start_time=start_time,
                end_time=end_time,
                replay_type="loop",
                extra={
                    "processed": processed,
                    "skipped": skipped,
                    "failed": failed,
                    "row_selector": row_selector,
                },
            )
            return {
                "success": False,
                "summary": summary,
                "processed": processed,
                "skipped": skipped,
                "failed": failed,
                "results": results,
                "cancelled": True,
            }

        rows_info = _get_rows_info(browser_engine, row_selector)
        if not rows_info:
            if row_index == 0:
                end_time = time.time()
                save_execution_record(
                    workflow_id=workflow_id,
                    workflow_name=wf.get("name", ""),
                    status="failed",
                    steps=[],
                    start_time=start_time,
                    end_time=end_time,
                    replay_type="loop",
                    error="未找到匹配 '{}' 的行元素".format(row_selector),
                    extra={"row_selector": row_selector},
                )
                return {
                    "success": False,
                    "error": "未找到匹配 '{}' 的行元素".format(row_selector),
                }
            break

        if row_index >= len(rows_info):
            break

        row = rows_info[row_index]

        row_step = {
            "type": "loop_row",
            "row_index": row_index + 1,
            "row_text": row.get("text", "")[:120],
        }

        # --- 条件判断（本地优先） ---
        if condition:
            local_result = _check_condition_local(condition, row)

            if local_result is not None:
                row_step["condition_match"] = local_result["match"]
                row_step["condition_reason"] = local_result["reason"]
                row_step["condition_mode"] = "local"
            else:
                llm = get_llm_client()
                if llm is None:
                    row_step["condition_match"] = True
                    row_step["condition_reason"] = "无 AI 配置，默认执行"
                    row_step["condition_mode"] = "default"
                else:
                    ai_result = _check_row_condition(
                        client=llm[0],
                        model=llm[1],
                        condition=condition,
                        row_info=row,
                    )
                    row_step["condition_match"] = ai_result.get("match", False)
                    row_step["condition_reason"] = ai_result.get("reason", "")
                    row_step["condition_mode"] = "ai"

            if not row_step.get("condition_match", True):
                row_step["status"] = "skipped"
                skipped += 1
                results.append(row_step)
                if on_step:
                    on_step(row_step)
                continue

        # --- 选择器适配（纯代码优先） ---
        adapted_steps = _adapt_steps_local(
            template_steps=template_steps,
            row_selector=row_selector,
            row_index=row_index,
            first_row_index=first_row_index,
        )

        if adapted_steps is None:
            llm = get_llm_client()
            if llm is None:
                row_step["status"] = "failed"
                row_step["error"] = "选择器适配失败且无 AI 配置"
                failed += 1
                results.append(row_step)
                if on_step:
                    on_step(row_step)
                continue

            adapted_steps = _adapt_steps_to_row(
                client=llm[0],
                model=llm[1],
                template_steps=template_steps,
                row_info=row,
                row_selector=row_selector,
                row_index=row_index,
            )

        if not adapted_steps:
            row_step["status"] = "failed"
            row_step["error"] = "无法适配步骤到当前行"
            failed += 1
            results.append(row_step)
            if on_step:
                on_step(row_step)
            continue

        # --- 执行步骤 ---
        row_step["adapted_steps"] = adapted_steps
        step_results = []
        row_success = True

        for si, astep in enumerate(adapted_steps):
            action = astep.get("action", "")
            params = astep.get("params", {})

            s_info = {
                "type": "loop_action",
                "row_index": row_index + 1,
                "step_index": si + 1,
                "action": action,
                "params": params,
            }

            try:
                result = browser_engine.execute_action(action, params)
                s_info["result"] = result
                s_info["success"] = "error" not in result
            except Exception as exc:
                s_info["result"] = {"error": str(exc)}
                s_info["success"] = False

            step_results.append(s_info)
            if on_step:
                on_step(s_info)

            if not s_info["success"]:
                row_success = False
                break

            smart_wait(browser_engine, action)

        row_step["step_results"] = step_results
        row_step["status"] = "done" if row_success else "failed"
        if row_success:
            processed += 1
        else:
            failed += 1

        results.append(row_step)
        if on_step:
            on_step(row_step)

        smart_wait(browser_engine, "click")

    summary = "循环回放完成: 处理 {} 行, 跳过 {} 行, 失败 {} 行".format(
        processed, skipped, failed,
    )

    end_time = time.time()
    save_execution_record(
        workflow_id=workflow_id,
        workflow_name=wf.get("name", ""),
        status="success" if failed == 0 else "partial",
        steps=results,
        start_time=start_time,
        end_time=end_time,
        replay_type="loop",
        extra={
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
            "row_selector": row_selector,
            "condition": condition,
        },
    )

    return {
        "success": True,
        "summary": summary,
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "rows": results,
    }


# ------------------------------------------------------------------
# 本地快速处理（不调 AI）
# ------------------------------------------------------------------


def _is_simple_condition(condition: str) -> bool:
    """判断条件是否可以本地处理（不需要 AI）。"""
    simple_patterns = [
        r"^包含.+$",
        r"^不包含.+$",
        r"^含有.+$",
        r"^有.+$",
        r"^没有.+$",
        r"^等于.+$",
        r"^不等于.+$",
        r"^.+等于.+$",
        r"^文本(是|为|包含|含有).+$",
        r"^状态(是|为|等于).+$",
        r"^contains?\s+.+$",
        r"^not\s+contains?\s+.+$",
        r"^equals?\s+.+$",
        r"^text\s+(is|contains|equals)\s+.+$",
    ]
    condition_lower = condition.strip().lower()
    return any(re.match(p, condition_lower) for p in simple_patterns)


def _check_condition_local(condition: str, row_info: dict) -> Optional[dict]:
    """用本地规则判断行是否匹配条件。"""
    text = row_info.get("text", "").strip()
    cells_text = " ".join(
        c.get("text", "") for c in row_info.get("cells", [])
    ).strip()
    full_text = "{} {}".format(text, cells_text).lower()

    cond = condition.strip()

    _q = '["\'\u201c\u201d\u2018\u2019]'

    # "包含XXX" / "含有XXX"
    m = re.match(r'^(?:包含|含有|有)\s*' + _q + r'?(.+?)' + _q + r'?\s*$', cond)
    if m:
        keyword = m.group(1).lower()
        is_match = keyword in full_text
        return {
            "match": is_match,
            "reason": "包含'{}'".format(m.group(1)) if is_match
            else "不包含'{}'".format(m.group(1)),
        }

    # "不包含XXX" / "没有XXX"
    m = re.match(r'^(?:不包含|没有|不含)\s*' + _q + r'?(.+?)' + _q + r'?\s*$', cond)
    if m:
        keyword = m.group(1).lower()
        is_match = keyword not in full_text
        return {
            "match": is_match,
            "reason": "不包含'{}'".format(m.group(1)) if is_match
            else "包含了'{}'".format(m.group(1)),
        }

    # "状态为/是XXX"
    m = re.match(r'^(?:状态|state)\s*(?:为|是|等于|=)\s*' + _q + r'?(.+?)' + _q + r'?\s*$', cond)
    if m:
        keyword = m.group(1).lower()
        is_match = keyword in full_text
        return {
            "match": is_match,
            "reason": "状态匹配'{}'".format(m.group(1)) if is_match
            else "状态不匹配'{}'".format(m.group(1)),
        }

    # "等于XXX"
    m = re.match(r'^等于\s*' + _q + r'?(.+?)' + _q + r'?\s*$', cond)
    if m:
        keyword = m.group(1).lower()
        is_match = keyword == text.lower()
        return {
            "match": is_match,
            "reason": "等于'{}'".format(m.group(1)) if is_match
            else "不等于'{}'".format(m.group(1)),
        }

    # English: "contains XXX"
    m = re.match(r"^contains?\s+[\"']?(.+?)[\"']?\s*$", cond, re.IGNORECASE)
    if m:
        keyword = m.group(1).lower()
        is_match = keyword in full_text
        return {
            "match": is_match,
            "reason": "contains '{}'".format(m.group(1)) if is_match
            else "does not contain '{}'".format(m.group(1)),
        }

    # English: "not contain XXX"
    m = re.match(r"^not\s+contains?\s+[\"']?(.+?)[\"']?\s*$", cond, re.IGNORECASE)
    if m:
        keyword = m.group(1).lower()
        is_match = keyword not in full_text
        return {
            "match": is_match,
            "reason": "does not contain '{}'".format(m.group(1)) if is_match
            else "contains '{}'".format(m.group(1)),
        }

    return None


def _detect_template_row_index(
    template_steps: list[dict],
    row_selector: str,
) -> Optional[int]:
    """从模板步骤的选择器中推断录制时操作的是第几行。"""
    for step in template_steps:
        selector = step.get("params", {}).get("selector", "")
        if not selector:
            continue
        m = re.search(r":nth-(?:child|of-type)\((\d+)\)", selector)
        if m:
            return int(m.group(1)) - 1
    return None


def _adapt_steps_local(
    template_steps: list[dict],
    row_selector: str,
    row_index: int,
    first_row_index: Optional[int],
) -> Optional[list[dict]]:
    """纯代码选择器适配 — 用 :nth-child 替换，不调 AI。"""
    no_selector_actions = frozenset({"goto", "wait", "press_key", "back", "forward"})
    nth_target = row_index + 1

    adapted = []
    for step in template_steps:
        action = step.get("action", "")
        params = dict(step.get("params", {}))

        if action in no_selector_actions or "selector" not in params:
            adapted.append({"action": action, "params": params})
            continue

        selector = params["selector"]

        new_selector, count = re.subn(
            r":nth-child\(\d+\)",
            ":nth-child({})".format(nth_target),
            selector,
        )
        if count > 0:
            params["selector"] = new_selector
            adapted.append({"action": action, "params": params})
            continue

        new_selector, count = re.subn(
            r":nth-of-type\(\d+\)",
            ":nth-of-type({})".format(nth_target),
            selector,
        )
        if count > 0:
            params["selector"] = new_selector
            adapted.append({"action": action, "params": params})
            continue

        if row_selector in selector:
            new_selector = selector.replace(
                row_selector,
                "{}:nth-child({})".format(row_selector, nth_target),
                1,
            )
            params["selector"] = new_selector
            adapted.append({"action": action, "params": params})
            continue

        params["selector"] = "{}:nth-child({}) {}".format(
            row_selector, nth_target, selector,
        )
        adapted.append({"action": action, "params": params})

    return adapted


def _get_rows_info(browser_engine: Any, row_selector: str) -> list[dict]:
    """获取列表中所有行的信息。"""
    try:
        result = browser_engine.execute_action("execute_js", {
            "script": """
            (() => {{
                const rows = document.querySelectorAll('{selector}');
                return Array.from(rows).slice(0, 100).map((row, i) => {{
                    const rect = row.getBoundingClientRect();
                    const cells = Array.from(row.children).map(c => ({{
                        tag: c.tagName.toLowerCase(),
                        text: (c.innerText || '').trim().substring(0, 100),
                        className: c.className || '',
                        id: c.id || '',
                    }}));
                    return {{
                        index: i,
                        tag: row.tagName.toLowerCase(),
                        text: (row.innerText || '').trim().substring(0, 200),
                        id: row.id || '',
                        className: row.className || '',
                        cells: cells,
                        visible: rect.height > 0 && rect.width > 0,
                    }};
                }});
            }})()
            """.format(selector=row_selector.replace("'", "\\'")),
        })
        rows = result.get("return_value", [])
        if isinstance(rows, list):
            return [r for r in rows if r.get("visible", True)]
        return []
    except Exception as exc:
        logger.warning("获取行信息失败: %s", exc)
        return []


def _check_row_condition(
    client: OpenAI,
    model: str,
    condition: str,
    row_info: dict,
) -> dict:
    """用 LLM 判断当前行是否满足条件。"""
    try:
        user_msg = (
            "请判断这一行是否满足条件。\n\n"
            "## 条件\n{condition}\n\n"
            "## 行内容\n"
            "- 文本: {text}\n"
            "- 单元格: {cells}\n"
        ).format(
            condition=condition,
            text=row_info.get("text", ""),
            cells=json.dumps(row_info.get("cells", []), ensure_ascii=False),
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _LOOP_CONDITION_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=150,
        )
        content = response.choices[0].message.content or ""
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1])
        return json.loads(content)
    except Exception as exc:
        logger.warning("条件判断 LLM 调用失败: %s", exc)
        return {"match": False, "reason": "LLM 调用失败: {}".format(exc)}


def _adapt_steps_to_row(
    client: OpenAI,
    model: str,
    template_steps: list[dict],
    row_info: dict,
    row_selector: str,
    row_index: int,
) -> Optional[list[dict]]:
    """让 LLM 把模板步骤的选择器适配到当前行。"""
    try:
        user_msg = (
            "请把以下操作步骤的选择器适配到列表的第 {row_num} 行。\n\n"
            "## 原始步骤\n"
            "```json\n{steps}\n```\n\n"
            "## 行定位\n"
            "- 行选择器: {row_selector}\n"
            "- 目标行索引: {row_index}（从 0 开始）\n\n"
            "## 目标行信息\n"
            "- 文本: {text}\n"
            "- ID: {id}\n"
            "- class: {cls}\n"
            "- 单元格: {cells}\n\n"
            "请返回调整后的完整步骤列表（JSON 数组）。"
        ).format(
            row_num=row_index + 1,
            steps=json.dumps(template_steps, ensure_ascii=False, indent=2),
            row_selector=row_selector,
            row_index=row_index,
            text=row_info.get("text", "")[:200],
            id=row_info.get("id", ""),
            cls=row_info.get("className", ""),
            cells=json.dumps(row_info.get("cells", []), ensure_ascii=False),
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _LOOP_SELECTOR_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=2000,
        )

        content = response.choices[0].message.content or ""
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1])

        adapted = json.loads(content)
        if isinstance(adapted, list):
            return adapted
        return None

    except Exception as exc:
        logger.warning("步骤适配 LLM 调用失败: %s", exc)
        return None


# ------------------------------------------------------------------
# 脚本导入 — 解析外部自动化脚本 → 内部 steps
# ------------------------------------------------------------------

# AI 脚本解析提示
_SCRIPT_PARSE_SYSTEM_PROMPT = """你是一个浏览器自动化脚本解析器。将用户提供的自动化脚本/指令转换为标准步骤列表。

每个步骤必须是以下格式之一：
- {"action": "goto", "params": {"url": "..."}}
- {"action": "click", "params": {"selector": "CSS选择器"}}
- {"action": "type_text", "params": {"selector": "CSS选择器", "text": "输入内容"}}
- {"action": "press_key", "params": {"key": "Enter/Tab/Escape等"}}
- {"action": "scroll", "params": {"direction": "down/up/left/right", "amount": 500}}
- {"action": "execute_js", "params": {"script": "JavaScript代码"}}
- {"action": "wait", "params": {"seconds": 1.0}}
- {"action": "back", "params": {}}
- {"action": "forward", "params": {}}
- {"action": "hover", "params": {"selector": "CSS选择器"}}
- {"action": "select_option", "params": {"selector": "CSS选择器", "value": "选项值"}}

规则：
1. 只输出 JSON 数组，不要任何解释
2. 保留原脚本的等待逻辑（wait_for_selector → wait）
3. Selenium 的 find_element(By.ID, "x") → selector "#x"
4. Selenium 的 find_element(By.CSS_SELECTOR, "x") → selector "x"
5. Selenium 的 find_element(By.XPATH, "//div[@id='x']") → 尽量转为 CSS selector
6. Selenium 的 find_element(By.NAME, "x") → selector "[name='x']"
7. Selenium 的 find_element(By.CLASS_NAME, "x") → selector ".x"
8. Selenium 的 find_element(By.LINK_TEXT, "x") → selector "a" + 添加注释
9. Playwright 的 page.click("selector") → {"action":"click","params":{"selector":"selector"}}
10. Playwright 的 page.fill("selector","text") → {"action":"type_text","params":{"selector":"selector","text":"text"}}
11. Playwright 的 page.goto("url") → {"action":"goto","params":{"url":"url"}}
12. 自然语言指令如「点击登录按钮」→ 推断合理的 CSS selector
13. 如果无法确定 selector，使用描述性的注释占位并标注 needs_fix: true

返回格式（严格 JSON）：
[
  {"action": "goto", "params": {"url": "https://example.com"}},
  {"action": "click", "params": {"selector": "#login-btn"}, "note": "点击登录"},
  ...
]

如果某个步骤无法确定选择器，标记为：
{"action": "click", "params": {"selector": "UNKNOWN"}, "needs_fix": true, "note": "原始: find_element(By.XPATH, '复杂xpath')"}
"""


def parse_script_local(content: str, format_hint: str = "auto") -> list[dict[str, Any]]:
    """尝试本地解析脚本，不调用 AI。"""
    if format_hint == "auto":
        format_hint = _detect_format(content)

    if format_hint == "json":
        return _parse_json_workflow(content)
    if format_hint == "selenium":
        return _parse_selenium_script(content)
    if format_hint == "playwright":
        return _parse_playwright_script(content)
    if format_hint == "text":
        return _parse_text_instructions(content)
    return []


# 导入文件大小限制（5MB）
_MAX_IMPORT_FILE_SIZE = 5 * 1024 * 1024
# 导入 ZIP 包最大解压后总大小（20MB）
_MAX_ZIP_TOTAL_SIZE = 20 * 1024 * 1024
# 允许导入的文件扩展名
_ALLOWED_EXTENSIONS = frozenset({".py", ".json", ".txt", ".js", ".ts"})


def safe_decode(raw_bytes: bytes) -> str:
    """安全解码文件内容 — 自动检测编码。"""
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        pass

    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    detected = chardet.detect(raw_bytes)
    encoding = detected.get("encoding")
    if encoding:
        try:
            return raw_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass

    for fallback in ("gbk", "gb2312", "latin-1"):
        try:
            return raw_bytes.decode(fallback)
        except UnicodeDecodeError:
            continue

    raise ValueError("无法检测文件编码，请确保文件是文本文件")


def validate_import_file(raw_bytes: bytes, filename: str) -> tuple[bool, str]:
    """验证导入文件的安全性和有效性。"""
    if len(raw_bytes) > _MAX_IMPORT_FILE_SIZE:
        return False, "文件大小超过限制（最大 5MB）"

    ext = os.path.splitext(filename)[1].lower()
    if ext == ".zip":
        return True, ""

    if ext not in _ALLOWED_EXTENSIONS:
        return False, "不支持的文件类型: {}，支持 {}".format(
            ext, ", ".join(sorted(_ALLOWED_EXTENSIONS)) + ", .zip",
        )

    try:
        text = safe_decode(raw_bytes)
    except ValueError as exc:
        return False, str(exc)

    _dangerous_patterns = [
        r'\bos\.system\s*\(',
        r'\bsubprocess\.\w+\s*\(',
        r'\b__import__\s*\(',
        r'\beval\s*\(',
        r'\bexec\s*\(',
        r'\bshutil\.rmtree\s*\(',
        r'\bos\.remove\s*\(',
    ]
    for pat in _dangerous_patterns:
        if re.search(pat, text):
            return False, "脚本包含潜在危险操作（{}），请检查后重试".format(
                pat.replace(r'\b', '').replace(r'\s*\(', '()'),
            )

    return True, ""


def parse_multi_files(
    files: dict[str, bytes],
    format_hint: str = "auto",
    ai_config: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """解析多个文件，自动追踪 import 依赖并合并步骤。"""
    warnings: list[str] = []
    all_steps: list[dict[str, Any]] = []
    files_parsed: list[str] = []

    decoded_files: dict[str, str] = {}
    for filename, raw_bytes in files.items():
        is_valid, err_msg = validate_import_file(raw_bytes, filename)
        if not is_valid:
            warnings.append("跳过文件 {}: {}".format(filename, err_msg))
            continue
        try:
            decoded_files[filename] = safe_decode(raw_bytes)
        except ValueError as exc:
            warnings.append("跳过文件 {}: {}".format(filename, exc))

    if not decoded_files:
        return {
            "steps": [],
            "format_detected": format_hint,
            "ai_used": False,
            "warnings": warnings,
            "files_parsed": [],
        }

    entry_file = _find_entry_file(decoded_files)

    if entry_file and entry_file in decoded_files:
        parse_order = _resolve_import_order(entry_file, decoded_files)
    else:
        parse_order = sorted(decoded_files.keys())

    for filename in parse_order:
        content = decoded_files.get(filename, "")
        if not content.strip():
            continue
        result = parse_script_with_ai(content, format_hint, ai_config)
        steps = result.get("steps", [])
        if steps:
            for step in steps:
                step["_source_file"] = filename
            all_steps.extend(steps)
            files_parsed.append(filename)
            if result.get("warnings"):
                for w in result["warnings"]:
                    warnings.append("{}: {}".format(filename, w))
        else:
            warnings.append("文件 {} 未解析出任何步骤".format(filename))

    deduped = _deduplicate_steps(all_steps)

    return {
        "steps": deduped,
        "format_detected": format_hint if format_hint != "auto" else "multi-file",
        "ai_used": False,
        "warnings": warnings,
        "files_parsed": files_parsed,
    }


def parse_zip_file(
    zip_bytes: bytes,
    format_hint: str = "auto",
    ai_config: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """解析 ZIP 包中的脚本文件。"""
    warnings: list[str] = []
    files: dict[str, bytes] = {}

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            total_size = sum(info.file_size for info in zf.infolist())
            if total_size > _MAX_ZIP_TOTAL_SIZE:
                return {
                    "steps": [],
                    "format_detected": format_hint,
                    "ai_used": False,
                    "warnings": ["ZIP 解压后总大小超过限制（最大 20MB）"],
                    "files_parsed": [],
                }

            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if any(part.startswith(".") or part == "__pycache__"
                       for part in name.split("/")):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext not in _ALLOWED_EXTENSIONS:
                    continue
                if info.file_size > _MAX_IMPORT_FILE_SIZE:
                    warnings.append("跳过大文件: {}".format(name))
                    continue
                files[name] = zf.read(name)

    except zipfile.BadZipFile:
        return {
            "steps": [],
            "format_detected": format_hint,
            "ai_used": False,
            "warnings": ["无效的 ZIP 文件"],
            "files_parsed": [],
        }

    if not files:
        return {
            "steps": [],
            "format_detected": format_hint,
            "ai_used": False,
            "warnings": ["ZIP 包中没有可解析的脚本文件"],
            "files_parsed": [],
        }

    warnings.append("从 ZIP 中发现 {} 个脚本文件".format(len(files)))
    result = parse_multi_files(files, format_hint, ai_config)
    result["warnings"] = warnings + result.get("warnings", [])
    return result


def _find_entry_file(files: dict[str, str]) -> Optional[str]:
    """从文件集合中推断入口文件。"""
    for name_pattern in ("main.py", "run.py", "app.py", "test.py", "script.py"):
        for filename in files:
            if os.path.basename(filename).lower() == name_pattern:
                return filename

    for filename, content in files.items():
        if re.search(r'if\s+__name__\s*==\s*["\']__main__', content):
            return filename

    for filename, content in files.items():
        if re.search(r'(?:driver|browser)\s*=\s*', content):
            return filename

    py_files = [(f, len(c)) for f, c in files.items() if f.endswith(".py")]
    if py_files:
        py_files.sort(key=lambda x: x[1], reverse=True)
        return py_files[0][0]

    return None


def _resolve_import_order(
    entry_file: str,
    files: dict[str, str],
) -> list[str]:
    """从入口文件出发，解析 import 依赖顺序（拓扑排序）。"""
    module_map: dict[str, str] = {}
    for filename in files:
        basename = os.path.splitext(filename)[0]
        module_name = basename.replace("/", ".").replace("\\", ".")
        module_map[module_name] = filename
        short_name = os.path.splitext(os.path.basename(filename))[0]
        if short_name not in module_map:
            module_map[short_name] = filename

    def get_imports(content: str) -> list[str]:
        imports = []
        for line in content.splitlines():
            line = line.strip()
            m = re.match(r'from\s+(\S+)\s+import', line)
            if m:
                imports.append(m.group(1))
                continue
            m = re.match(r'import\s+(\S+)', line)
            if m:
                mod = m.group(1).split(".")[0]
                imports.append(mod)
        return imports

    visited: set[str] = set()
    order: list[str] = []

    def dfs(filename: str) -> None:
        if filename in visited:
            return
        visited.add(filename)
        content = files.get(filename, "")
        for imp in get_imports(content):
            dep_file = module_map.get(imp)
            if dep_file and dep_file != filename:
                dfs(dep_file)
        order.append(filename)

    dfs(entry_file)

    for filename in sorted(files.keys()):
        if filename not in visited:
            order.append(filename)

    return order


def _deduplicate_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去除明显重复的步骤。"""
    if not steps:
        return steps

    deduped: list[dict[str, Any]] = [steps[0]]
    for i in range(1, len(steps)):
        prev = deduped[-1]
        curr = steps[i]

        if (prev["action"] == "goto" and curr["action"] == "goto"
                and prev.get("params", {}).get("url") == curr.get("params", {}).get("url")):
            continue

        if (prev["action"] == curr["action"]
                and prev.get("params") == curr.get("params")):
            continue

        deduped.append(curr)

    return deduped


def parse_script_with_ai(
    content: str,
    format_hint: str = "auto",
    ai_config: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """解析外部脚本为内部步骤格式。先尝试本地解析，失败则调 AI。"""
    if format_hint == "auto":
        format_hint = _detect_format(content)

    warnings: list[str] = []

    steps = parse_script_local(content, format_hint)

    if steps:
        needs_fix = [i for i, s in enumerate(steps) if s.get("needs_fix")]
        if not needs_fix:
            return {
                "steps": steps,
                "format_detected": format_hint,
                "ai_used": False,
                "warnings": warnings,
            }
        warnings.append("本地解析完成，但有 {} 个步骤需要 AI 修复".format(len(needs_fix)))

    if not ai_config or not ai_config.get("api_key"):
        if not steps:
            warnings.append("本地解析失败且未配置 AI，无法解析")
        return {
            "steps": steps,
            "format_detected": format_hint,
            "ai_used": False,
            "warnings": warnings,
        }

    try:
        client = OpenAI(
            api_key=ai_config["api_key"],
            base_url=ai_config.get("base_url", "https://api.openai.com/v1"),
        )
        model = ai_config.get("model", "gpt-4o")

        user_msg = "格式: {}\n\n脚本内容:\n```\n{}\n```".format(format_hint, content[:8000])

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SCRIPT_PARSE_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=4000,
        )

        raw = resp.choices[0].message.content.strip()
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if json_match:
            ai_steps = json.loads(json_match.group())
            valid_steps = []
            for s in ai_steps:
                if isinstance(s, dict) and "action" in s and "params" in s:
                    if s["action"] in PLAYABLE_ACTIONS:
                        valid_steps.append({
                            "action": s["action"],
                            "params": s["params"],
                            **({"needs_fix": True, "note": s.get("note", "")}
                               if s.get("needs_fix") else {}),
                            **({"note": s["note"]} if s.get("note") and not s.get("needs_fix") else {}),
                        })
            if valid_steps:
                return {
                    "steps": valid_steps,
                    "format_detected": format_hint,
                    "ai_used": True,
                    "warnings": warnings,
                }

        warnings.append("AI 返回结果无法解析为有效步骤")

    except Exception as exc:
        logger.warning("AI 脚本解析失败: %s", exc)
        warnings.append("AI 解析失败: {}".format(exc))

    return {
        "steps": steps,
        "format_detected": format_hint,
        "ai_used": True,
        "warnings": warnings,
    }


def validate_and_fix_workflow(
    steps: list[dict[str, Any]],
    browser_engine: Any,
    ai_config: Optional[dict[str, str]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict[str, Any]:
    """验证导入的工作流步骤，尝试自动修复失败的步骤。"""
    if not steps:
        return {"steps": [], "total": 0, "valid": 0, "fixed": 0, "failed": 0, "details": []}

    fixed_steps = []
    details = []
    valid_count = 0
    fixed_count = 0
    failed_count = 0

    for i, step in enumerate(steps):
        action = step.get("action", "")
        params = step.get("params", {})
        detail = {"index": i, "action": action, "status": "unknown"}

        if on_progress:
            on_progress({
                "phase": "validate",
                "index": i,
                "total": len(steps),
                "action": action,
            })

        if action == "goto":
            url = params.get("url", "")
            if url and (url.startswith("http://") or url.startswith("https://")):
                detail["status"] = "valid"
                valid_count += 1
                fixed_steps.append(step)
            else:
                detail["status"] = "warning"
                detail["message"] = "URL 格式可疑: {}".format(url)
                valid_count += 1
                fixed_steps.append(step)

        elif action in ("click", "type_text", "get_text"):
            selector = params.get("selector", "")
            if not selector or selector == "UNKNOWN":
                if ai_config and ai_config.get("api_key") and browser_engine:
                    fix_result = _ai_fix_unknown_selector(
                        browser_engine, action, params, step.get("note", ""), ai_config,
                    )
                    if fix_result:
                        new_step = {
                            "action": action,
                            "params": {**params, "selector": fix_result["selector"]},
                        }
                        fixed_steps.append(new_step)
                        detail["status"] = "fixed"
                        detail["message"] = "AI 修复: {}".format(fix_result['reason'])
                        detail["old_selector"] = selector
                        detail["new_selector"] = fix_result["selector"]
                        fixed_count += 1
                    else:
                        fixed_steps.append(step)
                        detail["status"] = "failed"
                        detail["message"] = "选择器无效且 AI 无法修复"
                        failed_count += 1
                else:
                    fixed_steps.append(step)
                    detail["status"] = "failed"
                    detail["message"] = "选择器未知，需要 AI 修复"
                    failed_count += 1
            else:
                if browser_engine and browser_engine.is_connected:
                    try:
                        count = browser_engine.page.locator(selector).count()
                        if count > 0:
                            detail["status"] = "valid"
                            valid_count += 1
                        else:
                            detail["status"] = "pending"
                            detail["message"] = "选择器在当前页面未找到（可能需要先导航）"
                            valid_count += 1
                    except Exception:
                        detail["status"] = "pending"
                        detail["message"] = "选择器语法有效但当前无法验证"
                        valid_count += 1
                else:
                    detail["status"] = "pending"
                    detail["message"] = "浏览器未启动，跳过验证"
                    valid_count += 1
                fixed_steps.append(step)

        elif action in ("press_key", "scroll", "execute_js", "wait", "back", "forward"):
            detail["status"] = "valid"
            valid_count += 1
            fixed_steps.append(step)

        else:
            detail["status"] = "warning"
            detail["message"] = "未知操作类型: {}".format(action)
            fixed_steps.append(step)

        details.append(detail)

    return {
        "steps": fixed_steps,
        "total": len(steps),
        "valid": valid_count,
        "fixed": fixed_count,
        "failed": failed_count,
        "details": details,
    }


def _ai_fix_unknown_selector(
    browser_engine: Any,
    action: str,
    original_params: dict,
    note: str,
    ai_config: dict,
) -> Optional[dict]:
    """AI 修复未知选择器。"""
    try:
        elements = browser_engine.execute_action("get_elements", {})
        page_info = browser_engine.execute_action("get_page_info", {})

        element_list = elements.get("elements", [])[:30]
        elements_text = json.dumps(element_list, ensure_ascii=False, indent=1)

        prompt = (
            "操作类型: {}\n"
            "原始参数: {}\n"
            "操作描述/备注: {}\n"
            "当前页面: {} - {}\n"
            "页面可交互元素:\n{}"
        ).format(
            action,
            json.dumps(original_params, ensure_ascii=False),
            note,
            page_info.get('url', ''),
            page_info.get('title', ''),
            elements_text,
        )

        client = OpenAI(
            api_key=ai_config["api_key"],
            base_url=ai_config.get("base_url", "https://api.openai.com/v1"),
        )

        resp = client.chat.completions.create(
            model=ai_config.get("model", "gpt-4o"),
            messages=[
                {"role": "system", "content": _FALLBACK_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=500,
        )

        raw = resp.choices[0].message.content.strip()
        result = json.loads(raw)
        if result.get("selector"):
            return result
        return None

    except Exception as exc:
        logger.warning("AI 修复未知选择器失败: %s", exc)
        return None


# ------------------------------------------------------------------
# 格式检测和本地解析器
# ------------------------------------------------------------------

def _detect_format(content: str) -> str:
    """自动检测脚本格式。"""
    stripped = content.strip()

    if stripped.startswith("[") or stripped.startswith("{"):
        return "json"

    selenium_patterns = [
        r'from\s+selenium',
        r'import\s+selenium',
        r'webdriver\.',
        r'find_element\(',
        r'By\.\w+',
        r'driver\.',
    ]
    for pat in selenium_patterns:
        if re.search(pat, content):
            return "selenium"

    playwright_patterns = [
        r'from\s+playwright',
        r'import\s+playwright',
        r'page\.\w+\(',
        r'browser\.new_page',
        r'chromium\.launch',
        r'\.fill\(',
        r'\.locator\(',
    ]
    for pat in playwright_patterns:
        if re.search(pat, content):
            return "playwright"

    return "text"


def _parse_json_workflow(content: str) -> list[dict[str, Any]]:
    """解析 JSON 格式工作流。"""
    try:
        data = json.loads(content.strip())

        if isinstance(data, list):
            return _validate_steps_format(data)

        if isinstance(data, dict):
            if "steps" in data:
                return _validate_steps_format(data["steps"])
            if "action" in data and "params" in data:
                return _validate_steps_format([data])
            # 兼容导出的工作流 JSON（含 exported_at 等元数据）
            if "exported_at" in data and "name" in data:
                return _validate_steps_format(data.get("steps", []))

    except json.JSONDecodeError:
        pass
    return []


def _validate_steps_format(steps: list) -> list[dict[str, Any]]:
    """验证并规范化步骤格式。"""
    valid = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        action = s.get("action", "")
        if action not in PLAYABLE_ACTIONS:
            continue
        params = s.get("params", {})
        if not isinstance(params, dict):
            continue
        entry = {"action": action, "params": params}
        if s.get("needs_fix"):
            entry["needs_fix"] = True
        if s.get("note"):
            entry["note"] = s["note"]
        valid.append(entry)
    return valid


def _parse_selenium_script(content: str) -> list[dict[str, Any]]:
    """从 Selenium Python 脚本中提取步骤（保持源码顺序）。

    支持: driver.get, find_element(By.XXX/字符串), find_element_by_xxx,
    click/send_keys/clear, WebDriverWait, ActionChains, switch_to.frame,
    Select, execute_script, back/forward/refresh 等。
    """
    found: list[tuple[int, dict[str, Any]]] = []

    def _resolve_locator(by_type: str, value: str) -> str:
        """将各种定位方式转为 CSS selector（兼容 By.XXX 和字符串格式）。"""
        by_map = {"id": "ID", "class name": "CLASS_NAME", "name": "NAME",
                  "css selector": "CSS_SELECTOR", "tag name": "TAG_NAME",
                  "xpath": "XPATH", "link text": "LINK_TEXT",
                  "partial link text": "PARTIAL_LINK_TEXT"}
        normalized = by_map.get(by_type.lower(), by_type.upper())
        return _selenium_locator_to_css(normalized, value)

    # driver.get("url") — 限定 driver 变量名，避免匹配 dict.get()
    for m in re.finditer(r'(?:driver|wd|browser|self\.driver)\.get\(\s*["\'](.+?)["\']\s*\)', content):
        found.append((m.start(), {"action": "goto", "params": {"url": m.group(1)}}))

    # ── find_element(By.XXX, "value") 格式 ──

    # find_element(By.XXX, "value").click()
    for m in re.finditer(
        r'find_element\(\s*By\.(\w+)\s*,\s*["\'](.+?)["\']\s*\)\.click\(\)', content
    ):
        selector = _selenium_locator_to_css(m.group(1), m.group(2))
        entry: dict[str, Any] = {"action": "click", "params": {"selector": selector}}
        if selector == "UNKNOWN":
            entry["needs_fix"] = True
            entry["note"] = "原始: By.{}, '{}'".format(m.group(1), m.group(2))
        found.append((m.start(), entry))

    # find_element(By.XXX, "value").send_keys("text")
    for m in re.finditer(
        r'find_element\(\s*By\.(\w+)\s*,\s*["\'](.+?)["\']\s*\)\.send_keys\(\s*["\'](.+?)["\']\s*\)',
        content,
    ):
        selector = _selenium_locator_to_css(m.group(1), m.group(2))
        entry = {"action": "type_text", "params": {"selector": selector, "text": m.group(3)}}
        if selector == "UNKNOWN":
            entry["needs_fix"] = True
            entry["note"] = "原始: By.{}, '{}'".format(m.group(1), m.group(2))
        found.append((m.start(), entry))

    # find_element(By.XXX, "value").send_keys(Keys.ENTER)
    for m in re.finditer(
        r'find_element\(\s*By\.(\w+)\s*,\s*["\'](.+?)["\']\s*\)\.send_keys\(\s*Keys\.(\w+)\s*\)',
        content,
    ):
        key = _selenium_key_to_standard(m.group(3))
        found.append((m.start(), {"action": "press_key", "params": {"key": key}}))

    # find_element(By.XXX, "value").clear()
    for m in re.finditer(
        r'find_element\(\s*By\.(\w+)\s*,\s*["\'](.+?)["\']\s*\)\.clear\(\)', content
    ):
        selector = _selenium_locator_to_css(m.group(1), m.group(2))
        found.append((m.start(), {
            "action": "execute_js",
            "params": {"script": "document.querySelector('{}').value = ''".format(
                selector.replace("'", "\\'"),
            )},
            "note": "清空输入框",
        }))

    # ── find_element("by", "value") 字符串格式 (Selenium 4.x) ──

    # find_element("by", "value").click()
    for m in re.finditer(
        r'find_element\(\s*["\'](\w[\w ]*)["\']'
        r'\s*,\s*["\'](.+?)["\']\s*\)\.click\(\)', content
    ):
        selector = _resolve_locator(m.group(1), m.group(2))
        entry = {"action": "click", "params": {"selector": selector}}
        if selector == "UNKNOWN":
            entry["needs_fix"] = True
            entry["note"] = "原始: '{}', '{}'".format(m.group(1), m.group(2))
        found.append((m.start(), entry))

    # find_element("by", "value").send_keys("text")
    for m in re.finditer(
        r'find_element\(\s*["\'](\w[\w ]*)["\']'
        r'\s*,\s*["\'](.+?)["\']\s*\)\.send_keys\(\s*["\'](.+?)["\']\s*\)', content
    ):
        selector = _resolve_locator(m.group(1), m.group(2))
        entry = {"action": "type_text", "params": {"selector": selector, "text": m.group(3)}}
        if selector == "UNKNOWN":
            entry["needs_fix"] = True
            entry["note"] = "原始: '{}', '{}'".format(m.group(1), m.group(2))
        found.append((m.start(), entry))

    # find_element("by", "value").send_keys(Keys.XXX)
    for m in re.finditer(
        r'find_element\(\s*["\'](\w[\w ]*)["\']'
        r'\s*,\s*["\'](.+?)["\']\s*\)\.send_keys\(\s*Keys\.(\w+)\s*\)', content
    ):
        key = _selenium_key_to_standard(m.group(3))
        found.append((m.start(), {"action": "press_key", "params": {"key": key}}))

    # find_element("by", "value").clear()
    for m in re.finditer(
        r'find_element\(\s*["\'](\w[\w ]*)["\']'
        r'\s*,\s*["\'](.+?)["\']\s*\)\.clear\(\)', content
    ):
        selector = _resolve_locator(m.group(1), m.group(2))
        found.append((m.start(), {
            "action": "execute_js",
            "params": {"script": "document.querySelector('{}').value = ''".format(
                selector.replace("'", "\\'"),
            )},
            "note": "清空输入框",
        }))

    # ── find_element_by_xxx("value") 旧版格式 ──

    # find_element_by_id/name/class_name/css_selector/tag_name("value").click()
    for m in re.finditer(
        r'find_element_by_(\w+)\(\s*["\'](.+?)["\']\s*\)\.click\(\)', content
    ):
        by_type = m.group(1).upper()
        selector = _selenium_locator_to_css(by_type, m.group(2))
        entry = {"action": "click", "params": {"selector": selector}}
        if selector == "UNKNOWN":
            entry["needs_fix"] = True
            entry["note"] = "原始: find_element_by_{}('{}')".format(m.group(1), m.group(2))
        found.append((m.start(), entry))

    # find_element_by_xxx("value").send_keys("text")
    for m in re.finditer(
        r'find_element_by_(\w+)\(\s*["\'](.+?)["\']\s*\)\.send_keys\(\s*["\'](.+?)["\']\s*\)',
        content,
    ):
        by_type = m.group(1).upper()
        selector = _selenium_locator_to_css(by_type, m.group(2))
        entry = {"action": "type_text", "params": {"selector": selector, "text": m.group(3)}}
        if selector == "UNKNOWN":
            entry["needs_fix"] = True
            entry["note"] = "原始: find_element_by_{}('{}')".format(m.group(1), m.group(2))
        found.append((m.start(), entry))

    # find_element_by_xxx("value").send_keys(Keys.XXX)
    for m in re.finditer(
        r'find_element_by_(\w+)\(\s*["\'](.+?)["\']\s*\)\.send_keys\(\s*Keys\.(\w+)\s*\)',
        content,
    ):
        key = _selenium_key_to_standard(m.group(3))
        found.append((m.start(), {"action": "press_key", "params": {"key": key}}))

    # WebDriverWait
    for m in re.finditer(r'WebDriverWait\s*\(\s*\w+\s*,\s*(\d+(?:\.\d+)?)\s*\)', content):
        found.append((m.start(), {"action": "wait", "params": {"seconds": float(m.group(1))}}))

    # time.sleep(N)
    for m in re.finditer(r'(?:time\.)?sleep\(\s*(\d+(?:\.\d+)?)\s*\)', content):
        found.append((m.start(), {"action": "wait", "params": {"seconds": float(m.group(1))}}))

    # driver.back() / forward() / refresh()
    _drv = r'(?:driver|wd|browser|self\.driver)'
    for m in re.finditer(_drv + r'\.back\(\)', content):
        found.append((m.start(), {"action": "back", "params": {}}))
    for m in re.finditer(_drv + r'\.forward\(\)', content):
        found.append((m.start(), {"action": "forward", "params": {}}))
    for m in re.finditer(_drv + r'\.refresh\(\)', content):
        found.append((m.start(), {"action": "execute_js", "params": {"script": "location.reload()"}}))

    # execute_script("...")
    for m in re.finditer(r'execute_script\(\s*(?:["\'](.+?)["\']|"""(.+?)""")\s*\)', content, re.DOTALL):
        script = m.group(1) or m.group(2)
        if script:
            found.append((m.start(), {"action": "execute_js", "params": {"script": script.strip()}}))

    # switch_to.frame → 标记需修复
    for m in re.finditer(r'switch_to\.frame\(\s*["\']?(.+?)["\']?\s*\)', content):
        found.append((m.start(), {
            "action": "execute_js",
            "params": {"script": "// switch_to.frame('{}')".format(m.group(1))},
            "note": "iframe 切换: {}".format(m.group(1)),
            "needs_fix": True,
        }))

    found.sort(key=lambda x: x[0])

    seen_positions: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for pos, step in found:
        if pos not in seen_positions:
            seen_positions.add(pos)
            deduped.append(step)

    return deduped


def _parse_playwright_script(content: str) -> list[dict[str, Any]]:
    """从 Playwright Python 脚本中提取步骤（保持源码顺序）。

    支持: goto, click, fill, type, press, hover, select_option, check/uncheck,
    wait_for_selector/timeout/load_state, go_back/forward, evaluate, expect 等。
    """
    found: list[tuple[int, dict[str, Any]]] = []

    # page.goto("url")
    for m in re.finditer(r'\.goto\(\s*["\'](.+?)["\']\s*\)', content):
        found.append((m.start(), {"action": "goto", "params": {"url": m.group(1)}}))

    # page.click("selector")
    for m in re.finditer(r'page\.click\(\s*["\'](.+?)["\']\s*\)', content):
        found.append((m.start(), {"action": "click", "params": {"selector": m.group(1)}}))
    # .locator("selector").click()
    for m in re.finditer(r'\.locator\(\s*["\'](.+?)["\']\s*\)\.click\(\)', content):
        found.append((m.start(), {"action": "click", "params": {"selector": m.group(1)}}))

    # page.fill("selector", "text")
    for m in re.finditer(
        r'page\.fill\(\s*["\'](.+?)["\']\s*,\s*["\'](.+?)["\']\s*\)', content
    ):
        found.append((m.start(), {"action": "type_text", "params": {"selector": m.group(1), "text": m.group(2)}}))
    # .locator("selector").fill("text")
    for m in re.finditer(
        r'\.locator\(\s*["\'](.+?)["\']\s*\)\.fill\(\s*["\'](.+?)["\']\s*\)', content
    ):
        found.append((m.start(), {"action": "type_text", "params": {"selector": m.group(1), "text": m.group(2)}}))

    # page.type("selector", "text")
    for m in re.finditer(
        r'page\.type\(\s*["\'](.+?)["\']\s*,\s*["\'](.+?)["\']\s*\)', content
    ):
        found.append((m.start(), {"action": "type_text", "params": {"selector": m.group(1), "text": m.group(2)}}))

    # page.press("selector", "key") / page.keyboard.press("key")
    for m in re.finditer(r'page\.press\(\s*["\'](.+?)["\']\s*,\s*["\'](.+?)["\']\s*\)', content):
        found.append((m.start(), {"action": "press_key", "params": {"key": m.group(2)}}))
    for m in re.finditer(r'keyboard\.press\(\s*["\'](.+?)["\']\s*\)', content):
        found.append((m.start(), {"action": "press_key", "params": {"key": m.group(1)}}))

    # page.hover / .locator().hover()
    for m in re.finditer(r'page\.hover\(\s*["\'](.+?)["\']\s*\)', content):
        found.append((m.start(), {
            "action": "hover",
            "params": {"selector": m.group(1)},
        }))
    for m in re.finditer(r'\.locator\(\s*["\'](.+?)["\']\s*\)\.hover\(\)', content):
        found.append((m.start(), {
            "action": "hover",
            "params": {"selector": m.group(1)},
        }))

    # page.select_option("selector", "value") / .locator().select_option("value")
    for m in re.finditer(
        r'page\.select_option\(\s*["\'](.+?)["\']\s*,\s*["\'](.+?)["\']\s*\)', content
    ):
        found.append((m.start(), {
            "action": "select_option",
            "params": {"selector": m.group(1), "value": m.group(2)},
        }))
    for m in re.finditer(
        r'\.locator\(\s*["\'](.+?)["\']\s*\)\.select_option\(\s*["\'](.+?)["\']\s*\)', content
    ):
        found.append((m.start(), {
            "action": "select_option",
            "params": {"selector": m.group(1), "value": m.group(2)},
        }))

    # page.check / page.uncheck
    for m in re.finditer(r'page\.check\(\s*["\'](.+?)["\']\s*\)', content):
        found.append((m.start(), {"action": "click", "params": {"selector": m.group(1)}, "note": "check"}))
    for m in re.finditer(r'page\.uncheck\(\s*["\'](.+?)["\']\s*\)', content):
        found.append((m.start(), {"action": "click", "params": {"selector": m.group(1)}, "note": "uncheck"}))

    # wait_for_selector → wait
    for m in re.finditer(r'wait_for_selector\(\s*["\'](.+?)["\']', content):
        found.append((m.start(), {"action": "wait", "params": {"seconds": 2.0}, "note": "wait_for: {}".format(m.group(1))}))

    # wait_for_timeout(ms)
    for m in re.finditer(r'wait_for_timeout\(\s*(\d+)\s*\)', content):
        found.append((m.start(), {"action": "wait", "params": {"seconds": int(m.group(1)) / 1000}}))

    # wait_for_load_state
    for m in re.finditer(r'wait_for_load_state\(\s*["\'](\w+)["\']\s*\)', content):
        found.append((m.start(), {"action": "wait", "params": {"seconds": 1.0}, "note": "wait_for_load_state"}))

    # go_back / go_forward / reload
    for m in re.finditer(r'\.go_back\(\)', content):
        found.append((m.start(), {"action": "back", "params": {}}))
    for m in re.finditer(r'\.go_forward\(\)', content):
        found.append((m.start(), {"action": "forward", "params": {}}))
    for m in re.finditer(r'page\.reload\(\)', content):
        found.append((m.start(), {"action": "execute_js", "params": {"script": "location.reload()"}}))

    # evaluate("js")
    for m in re.finditer(r'\.evaluate\(\s*["\'](.+?)["\']\s*\)', content):
        found.append((m.start(), {"action": "execute_js", "params": {"script": m.group(1)}}))

    # time.sleep(N)
    for m in re.finditer(r'(?:time\.)?sleep\(\s*(\d+(?:\.\d+)?)\s*\)', content):
        found.append((m.start(), {"action": "wait", "params": {"seconds": float(m.group(1))}}))

    # expect(page.locator("sel")).to_be_visible() → wait
    for m in re.finditer(r'expect\(\s*page\.locator\(\s*["\'](.+?)["\']\s*\)\s*\)\.to_be_\w+', content):
        found.append((m.start(), {"action": "wait", "params": {"seconds": 2.0}, "note": "expect visible"}))

    found.sort(key=lambda x: x[0])

    seen_positions: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for pos, step in found:
        if pos not in seen_positions:
            seen_positions.add(pos)
            deduped.append(step)

    return deduped


def _parse_text_instructions(content: str) -> list[dict[str, Any]]:
    """解析简单文本指令（每行一条）。

    支持：打开 URL、点击、输入、等待、按键、滚动、后退/前进、刷新、执行 JS 等。
    """
    steps: list[dict[str, Any]] = []

    for line in content.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        # 去除行号前缀（如 "1. xxx" 或 "- xxx"）
        line = re.sub(r'^\d+[\.\)]\s*', '', line)
        line = re.sub(r'^[-*]\s+', '', line)

        lower = line.lower()

        # 打开/访问 URL
        url_match = re.search(
            r'(?:打开|访问|跳转到?|导航到?|open|goto|navigate\s+to|go\s+to)\s*(https?://\S+)',
            line, re.IGNORECASE,
        )
        if url_match:
            steps.append({"action": "goto", "params": {"url": url_match.group(1)}})
            continue

        # 等待 N 秒
        wait_match = re.search(
            r'(?:等待|wait|sleep|暂停|pause)\s*(\d+(?:\.\d+)?)\s*(?:秒|s|seconds?|毫秒|ms)?',
            line, re.IGNORECASE,
        )
        if wait_match:
            seconds = float(wait_match.group(1))
            if re.search(r'(?:毫秒|ms)', line, re.IGNORECASE):
                seconds = seconds / 1000
            steps.append({"action": "wait", "params": {"seconds": seconds}})
            continue

        # "在 selector 中输入 text"（带引号格式）
        type_match2 = re.search(
            r'(?:在|in)\s*["\'](.+?)["\']\s*(?:中|里)?(?:输入|填写|type|fill|input)\s*["\'](.+?)["\']',
            line, re.IGNORECASE,
        )
        if type_match2:
            steps.append({
                "action": "type_text",
                "params": {"selector": type_match2.group(1), "text": type_match2.group(2)},
            })
            continue

        # "在XXX中输入 YYY"（自然语言，无引号）
        type_match_nl = re.search(
            r'(?:在)\s*(.+?)\s*(?:中|里)\s*(?:输入|填写|键入)\s*(.+)',
            line,
        )
        if type_match_nl:
            selector_hint = type_match_nl.group(1).strip()
            text_val = type_match_nl.group(2).strip().strip('"\'')
            steps.append({
                "action": "type_text",
                "params": {"selector": "UNKNOWN", "text": text_val},
                "needs_fix": True,
                "note": "在 {} 中输入".format(selector_hint),
            })
            continue

        # "输入 text 到 selector"
        type_match = re.search(
            r'(?:输入|填写|type|fill|input)\s*["\'](.+?)["\']\s*(?:到|into|in|至)\s*["\'](.+?)["\']',
            line, re.IGNORECASE,
        )
        if type_match:
            steps.append({
                "action": "type_text",
                "params": {"selector": type_match.group(2), "text": type_match.group(1)},
            })
            continue

        # 截图
        if re.search(r'(?:截图|截屏|screenshot|capture|screen\s*shot)', lower):
            steps.append({"action": "screenshot", "params": {}})
            continue

        # 点击选择器（带引号）
        click_match = re.search(
            r'(?:点击|click|tap|按下)\s*["\'](.+?)["\']',
            line, re.IGNORECASE,
        )
        if click_match:
            steps.append({"action": "click", "params": {"selector": click_match.group(1)}})
            continue

        # 点击自然语言（"点击XXX按钮"）
        click_nl_match = re.search(
            r'(?:点击|click|tap|按下)\s*(.+?)(?:按钮|button|链接|link)?\s*$',
            line, re.IGNORECASE,
        )
        if click_nl_match:
            target = click_nl_match.group(1).strip()
            if target:
                steps.append({
                    "action": "click",
                    "params": {"selector": "UNKNOWN"},
                    "needs_fix": True,
                    "note": "点击 {}".format(target),
                })
                continue

        # 按键
        key_match = re.search(
            r'(?:按|press)\s*(?:下\s*)?(?:键\s*)?(Enter|Tab|Escape|Space|Backspace|Delete|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Home|End|PageUp|PageDown|F\d+)',
            line, re.IGNORECASE,
        )
        if key_match:
            key_name = key_match.group(1)
            key_map = {
                "enter": "Enter", "tab": "Tab", "escape": "Escape",
                "space": " ", "backspace": "Backspace", "delete": "Delete",
            }
            key = key_map.get(key_name.lower(), key_name)
            steps.append({"action": "press_key", "params": {"key": key}})
            continue

        # 滚动
        scroll_match = re.search(
            r'(?:滚动|scroll)\s*(?:到|to)?\s*(上|下|底部|顶部|up|down|top|bottom)',
            lower,
        )
        if scroll_match:
            direction_map = {
                "上": "up", "下": "down", "底部": "down", "顶部": "up",
                "up": "up", "down": "down", "top": "up", "bottom": "down",
            }
            direction = direction_map.get(scroll_match.group(1), "down")
            amount = 99999 if scroll_match.group(1) in ("底部", "顶部", "top", "bottom") else 500
            steps.append({"action": "scroll", "params": {"direction": direction, "amount": amount}})
            continue

        # 刷新
        if re.search(r'(?:刷新|refresh|reload)', lower):
            steps.append({"action": "execute_js", "params": {"script": "location.reload()"}})
            continue

        # 后退/前进
        if re.search(r'(?:后退|返回|go\s*back|back)', lower):
            steps.append({"action": "back", "params": {}})
            continue
        if re.search(r'(?:前进|go\s*forward|forward)', lower):
            steps.append({"action": "forward", "params": {}})
            continue

        # 悬停（hover）
        hover_match = re.search(
            r'(?:悬停|hover|鼠标移到|移动到)\s*["\'](.+?)["\']',
            line, re.IGNORECASE,
        )
        if hover_match:
            steps.append({"action": "hover", "params": {"selector": hover_match.group(1)}})
            continue

        # 下拉选择（select_option）
        select_match = re.search(
            r'(?:选择|select)\s*["\'](.+?)["\']\s*(?:中的?|里的?|from|in)\s*["\'](.+?)["\']',
            line, re.IGNORECASE,
        )
        if select_match:
            steps.append({
                "action": "select_option",
                "params": {"selector": select_match.group(2), "value": select_match.group(1)},
            })
            continue
        # "在 selector 中选择 value"
        select_match2 = re.search(
            r'(?:在)\s*["\'](.+?)["\']\s*(?:中|里)\s*(?:选择|选|select)\s*["\'](.+?)["\']',
            line, re.IGNORECASE,
        )
        if select_match2:
            steps.append({
                "action": "select_option",
                "params": {"selector": select_match2.group(1), "value": select_match2.group(2)},
            })
            continue

        # 执行 JS
        js_match = re.search(r'(?:执行|run|execute)\s*(?:js|javascript)?\s*[:：]\s*(.+)', line, re.IGNORECASE)
        if js_match:
            steps.append({"action": "execute_js", "params": {"script": js_match.group(1).strip()}})
            continue

        # 无法本地解析的行
        steps.append({
            "action": "click",
            "params": {"selector": "UNKNOWN"},
            "needs_fix": True,
            "note": line,
        })

    return steps


def _selenium_key_to_standard(key_name: str) -> str:
    """将 Selenium Keys.XXX 名称转为标准按键名。"""
    key_map = {
        "ENTER": "Enter", "RETURN": "Enter", "TAB": "Tab",
        "ESCAPE": "Escape", "SPACE": " ", "BACKSPACE": "Backspace",
        "DELETE": "Delete", "UP": "ArrowUp", "DOWN": "ArrowDown",
        "LEFT": "ArrowLeft", "RIGHT": "ArrowRight",
        "HOME": "Home", "END": "End",
        "PAGE_UP": "PageUp", "PAGE_DOWN": "PageDown",
    }
    return key_map.get(key_name.upper(), key_name)


def _selenium_locator_to_css(by_type: str, value: str) -> str:
    """将 Selenium 的 By.XXX 定位方式转为 CSS selector。"""
    by_type = by_type.upper()
    if by_type == "ID":
        return "#{}".format(value)
    if by_type == "CLASS_NAME":
        return ".{}".format(value)
    if by_type == "NAME":
        return "[name='{}']".format(value)
    if by_type == "CSS_SELECTOR":
        return value
    if by_type == "TAG_NAME":
        return value
    if by_type in ("LINK_TEXT", "PARTIAL_LINK_TEXT"):
        return "UNKNOWN"
    if by_type == "XPATH":
        simple = _simple_xpath_to_css(value)
        return simple if simple else "UNKNOWN"
    return "UNKNOWN"


def _simple_xpath_to_css(xpath: str) -> Optional[str]:
    """尝试将简单的 XPath 转为 CSS selector。"""
    m = re.match(r"^//(\w+)\[@id=['\"](.+?)['\"]\]$", xpath)
    if m:
        return "{}#{}".format(m.group(1), m.group(2))

    m = re.match(r"^//(\w+)\[@class=['\"](.+?)['\"]\]$", xpath)
    if m:
        return "{}.{}".format(m.group(1), m.group(2).replace(' ', '.'))

    m = re.match(r"^//(\w+)\[@name=['\"](.+?)['\"]\]$", xpath)
    if m:
        return "{}[name='{}']".format(m.group(1), m.group(2))

    return None


# ------------------------------------------------------------------
# 数据提取与导出
# ------------------------------------------------------------------

# 提取操作类型 — 可作为工作流步骤插入
DATA_EXTRACT_ACTIONS = frozenset({
    "extract_text", "extract_attribute", "extract_table",
})


def extract_data_from_page(
    browser_engine: Any,
    extract_steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从当前页面提取数据。

    每个 extract_step 包含:
      - type: extract_text | extract_attribute | extract_table
      - selector: CSS 选择器
      - name: 数据列名（可选）
      - attribute: 提取属性名（仅 extract_attribute 需要）
    """
    results = []
    for step in extract_steps:
        etype = step.get("type", "extract_text")
        selector = step.get("selector", "")
        name = step.get("name", selector)

        if etype == "extract_text":
            result = browser_engine.execute_action("execute_js", {
                "script": """
                    (() => {{
                        const els = document.querySelectorAll('{}');
                        return Array.from(els).map(e => e.innerText.trim());
                    }})()
                """.format(selector.replace("'", "\\'"))
            })
            values = result.get("result", [])
            results.append({"name": name, "type": etype, "values": values if isinstance(values, list) else [values]})

        elif etype == "extract_attribute":
            attr = step.get("attribute", "href")
            result = browser_engine.execute_action("execute_js", {
                "script": """
                    (() => {{
                        const els = document.querySelectorAll('{sel}');
                        return Array.from(els).map(e => e.getAttribute('{attr}'));
                    }})()
                """.format(sel=selector.replace("'", "\\'"), attr=attr.replace("'", "\\'"))
            })
            values = result.get("result", [])
            results.append({"name": name, "type": etype, "attribute": attr, "values": values if isinstance(values, list) else [values]})

        elif etype == "extract_table":
            result = browser_engine.execute_action("execute_js", {
                "script": """
                    (() => {{
                        const table = document.querySelector('{sel}');
                        if (!table) return {{headers: [], rows: []}};
                        const headers = Array.from(table.querySelectorAll('thead th, thead td')).map(e => e.innerText.trim());
                        const rows = Array.from(table.querySelectorAll('tbody tr')).map(tr =>
                            Array.from(tr.querySelectorAll('td, th')).map(td => td.innerText.trim())
                        );
                        return {{headers, rows}};
                    }})()
                """.format(sel=selector.replace("'", "\\'"))
            })
            table_data = result.get("result", {})
            results.append({"name": name, "type": etype, "data": table_data if isinstance(table_data, dict) else {}})

    return results


def export_extracted_data(
    data: list[dict[str, Any]],
    fmt: str = "json",
) -> str:
    """将提取的数据导出为指定格式字符串。

    Args:
        data: extract_data_from_page 返回的数据
        fmt: 'json' 或 'csv'

    Returns:
        格式化的字符串内容
    """
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)

    # CSV 格式
    lines = []
    for item in data:
        if item["type"] == "extract_table":
            table = item.get("data", {})
            headers = table.get("headers", [])
            rows = table.get("rows", [])
            if headers:
                lines.append(",".join('"{}"'.format(h.replace('"', '""')) for h in headers))
            for row in rows:
                lines.append(",".join('"{}"'.format(str(c).replace('"', '""')) for c in row))
        else:
            lines.append('"{}"'.format(item.get("name", "").replace('"', '""')))
            for v in item.get("values", []):
                lines.append('"{}"'.format(str(v).replace('"', '""')))
        lines.append("")  # 空行分隔

    return "\n".join(lines)


def replay_with_extraction(
    workflow_id: str,
    browser_engine: Any,
    extract_steps: list[dict[str, Any]],
    variables: Optional[dict[str, str]] = None,
    on_step: Optional[Callable[[dict], None]] = None,
    ai_config: Optional[dict[str, str]] = None,
    stop_event: Optional[threading.Event] = None,
) -> dict[str, Any]:
    """回放工作流后执行数据提取。"""
    replay_result = replay_workflow(
        workflow_id=workflow_id,
        browser_engine=browser_engine,
        variables=variables,
        on_step=on_step,
        ai_config=ai_config,
        stop_event=stop_event,
    )

    if not replay_result.get("success"):
        return replay_result

    extracted = extract_data_from_page(browser_engine, extract_steps)
    replay_result["extracted_data"] = extracted
    return replay_result


# ------------------------------------------------------------------
# 条件分支
# ------------------------------------------------------------------

def evaluate_step_condition(
    browser_engine: Any,
    condition: dict[str, Any],
) -> bool:
    """评估步骤条件是否满足。

    条件格式:
      {"type": "element_exists", "selector": "..."}
      {"type": "element_visible", "selector": "..."}
      {"type": "text_contains", "selector": "...", "text": "..."}
      {"type": "text_equals", "selector": "...", "text": "..."}
      {"type": "url_contains", "text": "..."}
      {"type": "js_expression", "script": "..."}  # 返回 truthy/falsy
    """
    ctype = condition.get("type", "")
    selector = condition.get("selector", "")
    text = condition.get("text", "")

    if ctype == "element_exists":
        result = browser_engine.execute_action("execute_js", {
            "script": "!!document.querySelector('{}')".format(selector.replace("'", "\\'"))
        })
        return bool(result.get("result"))

    if ctype == "element_visible":
        result = browser_engine.execute_action("execute_js", {
            "script": """
                (() => {{
                    const el = document.querySelector('{sel}');
                    if (!el) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                }})()
            """.format(sel=selector.replace("'", "\\'"))
        })
        return bool(result.get("result"))

    if ctype == "text_contains":
        result = browser_engine.execute_action("execute_js", {
            "script": """
                (() => {{
                    const el = document.querySelector('{sel}');
                    return el ? el.innerText.includes('{txt}') : false;
                }})()
            """.format(
                sel=selector.replace("'", "\\'"),
                txt=text.replace("'", "\\'"),
            )
        })
        return bool(result.get("result"))

    if ctype == "text_equals":
        result = browser_engine.execute_action("execute_js", {
            "script": """
                (() => {{
                    const el = document.querySelector('{sel}');
                    return el ? el.innerText.trim() === '{txt}' : false;
                }})()
            """.format(
                sel=selector.replace("'", "\\'"),
                txt=text.replace("'", "\\'"),
            )
        })
        return bool(result.get("result"))

    if ctype == "url_contains":
        result = browser_engine.execute_action("execute_js", {
            "script": "window.location.href.includes('{}')".format(text.replace("'", "\\'"))
        })
        return bool(result.get("result"))

    if ctype == "js_expression":
        script = condition.get("script", "false")
        result = browser_engine.execute_action("execute_js", {"script": script})
        return bool(result.get("result"))

    logger.warning("未知条件类型: %s", ctype)
    return True  # 未知条件默认通过


def replay_with_conditions(
    workflow_id: str,
    browser_engine: Any,
    step_conditions: Optional[dict[int, dict[str, Any]]] = None,
    variables: Optional[dict[str, str]] = None,
    on_step: Optional[Callable[[dict], None]] = None,
    ai_config: Optional[dict[str, str]] = None,
    stop_event: Optional[threading.Event] = None,
    max_retries: int = 2,
    retry_delay: float = 1.0,
) -> dict[str, Any]:
    """带条件分支的回放。

    Args:
        step_conditions: {步骤索引: 条件配置}。满足条件才执行该步骤。
            条件配置: {"condition": {...}, "on_false": "skip"|"stop"|"goto:N"}
    """
    wf = get_workflow(workflow_id)
    if wf is None:
        return {"success": False, "error": "工作流不存在"}

    steps = wf.get("steps", [])
    if not steps:
        return {"success": False, "error": "工作流没有可回放的步骤"}

    step_conditions = step_conditions or {}
    rc = wf.get("replay_config") or {}
    max_retries = rc.get("max_retries", max_retries)
    retry_delay = rc.get("retry_delay", retry_delay)

    variables = variables or {}
    results = []
    total = len(steps)
    ai_fix_count = 0
    start_time = time.time()

    i = 0
    while i < total:
        if stop_event and stop_event.is_set():
            end_time = time.time()
            save_execution_record(
                workflow_id=workflow_id,
                workflow_name=wf.get("name", ""),
                status="cancelled",
                steps=results,
                start_time=start_time,
                end_time=end_time,
                error="用户手动停止",
                extra={"ai_fix_count": ai_fix_count},
            )
            return {
                "success": False,
                "error": "回放已被用户手动停止",
                "steps": results,
                "stopped_at": i + 1,
                "ai_fix_count": ai_fix_count,
                "cancelled": True,
            }

        step = steps[i]
        action = step["action"]
        params = _apply_variables(step.get("params", {}), variables)

        step_info = {
            "step": i + 1,
            "total": total,
            "type": "replay_action",
            "action": action,
            "params": params,
        }

        # 条件分支检查
        if i in step_conditions:
            cond_config = step_conditions[i]
            cond = cond_config.get("condition", {})
            on_false = cond_config.get("on_false", "skip")

            is_met = evaluate_step_condition(browser_engine, cond)
            step_info["condition_met"] = is_met
            step_info["condition"] = cond

            if not is_met:
                step_info["success"] = True
                step_info["skipped"] = True
                step_info["skip_reason"] = "条件不满足: {}".format(cond.get("type", ""))
                results.append(step_info)
                if on_step:
                    on_step(step_info)

                if on_false == "stop":
                    break
                if on_false.startswith("goto:"):
                    target = int(on_false.split(":")[1]) - 1
                    i = max(0, min(target, total - 1))
                    continue
                # skip — 默认
                i += 1
                continue

        # 执行步骤（与 replay_workflow 相同逻辑）
        for attempt in range(1 + max_retries):
            if attempt > 0:
                step_info["retry"] = attempt
                time.sleep(retry_delay)

            try:
                result = browser_engine.execute_action(action, params)
                step_info["result"] = result
                step_info["success"] = "error" not in result
            except Exception as exc:
                step_info["result"] = {"error": str(exc)}
                step_info["success"] = False

            if step_info["success"]:
                break

            # AI 降级修复
            if (
                attempt == max_retries
                and action in _SELECTOR_ACTIONS
                and "selector" in params
                and ai_config
            ):
                fix_result = _ai_fallback_fix(
                    browser_engine=browser_engine,
                    action=action,
                    original_params=params,
                    error_msg=step_info["result"].get("error", ""),
                    ai_config=ai_config,
                )
                if fix_result and fix_result.get("new_selector"):
                    new_params = dict(params)
                    new_params["selector"] = fix_result["new_selector"]
                    step_info["ai_fix"] = {
                        "original_selector": params["selector"],
                        "new_selector": fix_result["new_selector"],
                    }
                    try:
                        result = browser_engine.execute_action(action, new_params)
                        step_info["result"] = result
                        step_info["success"] = "error" not in result
                        step_info["params"] = new_params
                        if step_info["success"]:
                            ai_fix_count += 1
                    except Exception as exc:
                        step_info["result"] = {"error": str(exc)}
                        step_info["success"] = False

        results.append(step_info)
        if on_step:
            on_step(step_info)

        if not step_info["success"]:
            end_time = time.time()
            error_msg = "步骤 {}/{} ({}) 执行失败".format(i + 1, total, action)
            save_execution_record(
                workflow_id=workflow_id,
                workflow_name=wf.get("name", ""),
                status="failed",
                steps=results,
                start_time=start_time,
                end_time=end_time,
                error=error_msg,
                extra={"ai_fix_count": ai_fix_count},
            )
            return {
                "success": False,
                "error": error_msg,
                "steps": results,
                "stopped_at": i + 1,
                "ai_fix_count": ai_fix_count,
            }

        smart_wait(browser_engine, action)
        i += 1

    end_time = time.time()
    _increment_run_count(workflow_id)
    summary = "条件回放完成，共执行 {} 步".format(len(results))
    save_execution_record(
        workflow_id=workflow_id,
        workflow_name=wf.get("name", ""),
        status="success",
        steps=results,
        start_time=start_time,
        end_time=end_time,
        replay_type="conditional",
        extra={"ai_fix_count": ai_fix_count},
    )
    return {
        "success": True,
        "summary": summary,
        "steps": results,
        "ai_fix_count": ai_fix_count,
    }


# ------------------------------------------------------------------
# 定时执行
# ------------------------------------------------------------------

# 调度存储目录
SCHEDULE_DIR = os.path.join(os.path.dirname(__file__), "schedules")

_scheduler = None
_scheduler_lock = threading.Lock()


def _ensure_schedule_dir() -> None:
    os.makedirs(SCHEDULE_DIR, exist_ok=True)


def _schedule_path(schedule_id: str) -> str:
    return os.path.join(SCHEDULE_DIR, "{}.json".format(schedule_id))


def get_scheduler():
    """获取或初始化 APScheduler 实例（延迟初始化）。"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return _scheduler
        from apscheduler.schedulers.background import BackgroundScheduler
        _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        _scheduler.start()
        # 恢复已保存的定时任务
        _restore_schedules()
        return _scheduler


def save_schedule(
    workflow_id: str,
    trigger_type: str,
    trigger_args: dict[str, Any],
    variables: Optional[dict[str, str]] = None,
    ai_config: Optional[dict[str, str]] = None,
    enabled: bool = True,
    auto_start_browser: bool = True,
    auto_close_browser: bool = False,
) -> dict[str, Any]:
    """创建或更新定时任务。

    Args:
        trigger_type: 'cron' | 'interval' | 'date'
        trigger_args: APScheduler 触发器参数
            cron: {"hour": 9, "minute": 30, "day_of_week": "mon-fri"}
            interval: {"minutes": 30}
            date: {"run_date": "2026-01-01 09:00:00"}
    """
    wf = get_workflow(workflow_id)
    if wf is None:
        return {"success": False, "error": "工作流不存在"}

    schedule_id = str(uuid.uuid4())[:8]
    record = {
        "id": schedule_id,
        "workflow_id": workflow_id,
        "workflow_name": wf.get("name", ""),
        "trigger_type": trigger_type,
        "trigger_args": trigger_args,
        "variables": variables or {},
        "ai_config": ai_config,
        "enabled": enabled,
        "auto_start_browser": auto_start_browser,
        "auto_close_browser": auto_close_browser,
        "created_at": time.time(),
        "last_run": None,
        "run_count": 0,
    }

    _ensure_schedule_dir()
    with _file_lock:
        with open(_schedule_path(schedule_id), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

    if enabled:
        _register_job(record)

    return {"success": True, "schedule": record}


def list_schedules() -> list[dict[str, Any]]:
    """列出所有定时任务。"""
    _ensure_schedule_dir()
    schedules = []
    with _file_lock:
        for fname in os.listdir(SCHEDULE_DIR):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(SCHEDULE_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    schedules.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
    schedules.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return schedules


def get_schedule(schedule_id: str) -> Optional[dict[str, Any]]:
    """获取单个定时任务。"""
    fpath = _schedule_path(schedule_id)
    with _file_lock:
        if not os.path.isfile(fpath):
            return None
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None


def update_schedule(schedule_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """更新定时任务配置。"""
    record = get_schedule(schedule_id)
    if record is None:
        return {"success": False, "error": "定时任务不存在"}

    for key in ("trigger_type", "trigger_args", "variables", "ai_config", "enabled", "auto_start_browser", "auto_close_browser"):
        if key in updates:
            record[key] = updates[key]

    with _file_lock:
        with open(_schedule_path(schedule_id), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

    # 更新调度器
    scheduler = get_scheduler()
    job = scheduler.get_job(schedule_id)
    if job:
        scheduler.remove_job(schedule_id)
    if record.get("enabled"):
        _register_job(record)

    return {"success": True, "schedule": record}


def delete_schedule(schedule_id: str) -> bool:
    """删除定时任务。"""
    fpath = _schedule_path(schedule_id)
    with _file_lock:
        if not os.path.isfile(fpath):
            return False
        os.remove(fpath)

    scheduler = get_scheduler()
    job = scheduler.get_job(schedule_id)
    if job:
        scheduler.remove_job(schedule_id)
    return True


def toggle_schedule(schedule_id: str, enabled: bool) -> dict[str, Any]:
    """启用/禁用定时任务。"""
    return update_schedule(schedule_id, {"enabled": enabled})


def _register_job(record: dict[str, Any]) -> None:
    """将定时任务注册到调度器。"""
    scheduler = get_scheduler()
    trigger_type = record.get("trigger_type", "interval")
    trigger_args = record.get("trigger_args", {})

    scheduler.add_job(
        func=_scheduled_replay_job,
        trigger=trigger_type,
        id=record["id"],
        kwargs={
            "schedule_id": record["id"],
            "workflow_id": record["workflow_id"],
            "variables": record.get("variables"),
            "ai_config": record.get("ai_config"),
        },
        replace_existing=True,
        max_instances=1,           # 上一次还没跑完时不重复触发
        coalesce=True,             # 错过的多次触发合并为一次
        misfire_grace_time=300,    # 延迟 5 分钟内仍会补跑
        **trigger_args,
    )
    logger.info("已注册定时任务: %s (%s)", record["id"], record.get("workflow_name", ""))


def _restore_schedules() -> None:
    """启动时恢复所有已启用的定时任务。"""
    for schedule in list_schedules():
        if schedule.get("enabled"):
            try:
                _register_job(schedule)
            except Exception as exc:
                logger.error("恢复定时任务失败: %s — %s", schedule.get("id"), exc)


# 定时回放执行函数 — 被 APScheduler 调用时 browser_engine 需从外部注入
_scheduled_browser_engine = None
_scheduled_event_callback = None
_scheduled_engine_provider: Optional[Callable[..., Any]] = None
_scheduled_browser_stopper: Optional[Callable[[], None]] = None


def set_scheduled_replay_context(
    browser_engine: Any,
    event_callback: Optional[Callable[[str, Any], None]] = None,
    engine_provider: Optional[Callable[..., Any]] = None,
    stop_browser_provider: Optional[Callable[[], None]] = None,
) -> None:
    """设置定时回放的全局上下文（由 app.py 在启动时调用）。

    engine_provider(auto_start: bool) -> engine：触发时调用，负责返回可用引擎，
    浏览器未启动且 auto_start=True 时自动拉起；不可用时抛出异常说明原因。
    stop_browser_provider()：执行完且 auto_close_browser=True 时调用，关闭浏览器。
    """
    global _scheduled_browser_engine, _scheduled_event_callback, _scheduled_engine_provider, _scheduled_browser_stopper
    _scheduled_browser_engine = browser_engine
    _scheduled_event_callback = event_callback
    if engine_provider is not None:
        _scheduled_engine_provider = engine_provider
    if stop_browser_provider is not None:
        _scheduled_browser_stopper = stop_browser_provider


def _emit_schedule(event: str, data: dict) -> None:
    if _scheduled_event_callback:
        try:
            _scheduled_event_callback(event, data)
        except Exception:
            pass


def set_engine_running_reporter(reporter: Optional[Callable[[], bool]]) -> None:
    """注入"浏览器当前是否已连接"查询回调（app.py 在启动时调用）。

    定时任务用它判断触发前浏览器是否本来就在运行：
    只有原本没开、由本次任务自动拉起的浏览器，auto_close_browser 才会关闭。
    """
    global _engine_running_reporter
    if reporter is not None:
        _engine_running_reporter = reporter


_engine_running_reporter: Optional[Callable[[], bool]] = None


def _provider_reports_running() -> bool:
    if _engine_running_reporter is None:
        return False
    try:
        return bool(_engine_running_reporter())
    except Exception:
        return False


def _save_schedule_run(schedule_id: str, success: bool, error: str = "") -> None:
    record = get_schedule(schedule_id)
    if not record:
        return
    record["last_run"] = time.time()
    record["run_count"] = record.get("run_count", 0) + 1
    record["last_result"] = "success" if success else "failed"
    record["last_error"] = "" if success else (error or "")[:300]
    with _file_lock:
        with open(_schedule_path(schedule_id), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)


def _scheduled_replay_job(
    schedule_id: str,
    workflow_id: str,
    variables: Optional[dict[str, str]] = None,
    ai_config: Optional[dict[str, str]] = None,
) -> None:
    """定时任务执行函数。"""
    record = get_schedule(schedule_id) or {}
    wf_name = record.get("workflow_name", "")
    auto_start = record.get("auto_start_browser", True)
    auto_close = record.get("auto_close_browser", False)
    # 触发前浏览器是否本来就在运行（稍后 engine 获取后再实际判断）
    browser_was_running = False
    logger.info("定时任务触发: %s (workflow=%s)", schedule_id, workflow_id)
    _emit_schedule("scheduled_replay_start", {
        "schedule_id": schedule_id, "workflow_id": workflow_id, "workflow_name": wf_name,
    })

    # 获取可用浏览器引擎（未启动时按配置自动拉起）
    engine = None
    try:
        if _scheduled_engine_provider is not None:
            browser_was_running = _provider_reports_running()
            engine = _scheduled_engine_provider(auto_start=auto_start)
        else:
            browser_was_running = True
            engine = _scheduled_browser_engine
            if engine is None or not getattr(engine, "is_connected", False):
                raise RuntimeError("浏览器引擎未就绪")
    except Exception as exc:
        err = str(exc)
        logger.error("定时回放未执行: %s — %s", schedule_id, err)
        _save_schedule_run(schedule_id, False, err)
        _emit_schedule("scheduled_replay_complete", {
            "schedule_id": schedule_id, "workflow_id": workflow_id, "workflow_name": wf_name,
            "success": False, "error": err,
        })
        return

    def on_step(step_info: dict) -> None:
        _emit_schedule("scheduled_replay_step", step_info)

    try:
        result = replay_workflow(
            workflow_id=workflow_id,
            browser_engine=engine,
            variables=variables,
            on_step=on_step,
            ai_config=ai_config,
        )
    except Exception as exc:
        result = {"success": False, "error": str(exc)}

    success = bool(result.get("success"))
    _save_schedule_run(schedule_id, success, result.get("error", ""))
    _emit_schedule("scheduled_replay_complete", {
        "schedule_id": schedule_id,
        "workflow_id": workflow_id,
        "workflow_name": wf_name,
        "success": success,
        "summary": result.get("summary", ""),
        "error": result.get("error", ""),
    })

    logger.info("定时任务完成: %s — %s", schedule_id, "成功" if success else "失败")

    # 执行完自动关闭浏览器（仅当浏览器是本次任务自动拉起的）
    if auto_close and _scheduled_browser_stopper is not None and not browser_was_running:
        logger.info("定时任务: 浏览器为本次自动拉起，正在自动关闭…")
        try:
            _scheduled_browser_stopper()
        except Exception as exc:
            logger.error("定时任务自动关闭浏览器失败: %s", exc)


def run_schedule_now(schedule_id: str) -> dict[str, Any]:
    """立即在后台执行一次定时任务（与定时触发流程一致）。"""
    record = get_schedule(schedule_id)
    if record is None:
        return {"success": False, "error": "定时任务不存在"}
    t = threading.Thread(
        target=_scheduled_replay_job,
        kwargs={
            "schedule_id": record["id"],
            "workflow_id": record["workflow_id"],
            "variables": record.get("variables"),
            "ai_config": record.get("ai_config"),
        },
        daemon=True,
    )
    t.start()
    return {"success": True, "message": "已开始执行"}


def get_schedule_next_run(schedule_id: str) -> Optional[float]:
    """返回定时任务下次触发的时间戳（未注册/已禁用返回 None）。"""
    try:
        job = get_scheduler().get_job(schedule_id)
        if job and job.next_run_time:
            return job.next_run_time.timestamp()
    except Exception:
        pass
    return None


# ------------------------------------------------------------------
# Webhook 通知
# ------------------------------------------------------------------

WEBHOOK_DIR = os.path.join(os.path.dirname(__file__), "webhooks")


def _ensure_webhook_dir() -> None:
    os.makedirs(WEBHOOK_DIR, exist_ok=True)


def _webhook_path(webhook_id: str) -> str:
    return os.path.join(WEBHOOK_DIR, f"{webhook_id}.json")


def save_webhook(
    name: str,
    url: str,
    events: Optional[list[str]] = None,
    headers: Optional[dict[str, str]] = None,
    enabled: bool = True,
    webhook_id: Optional[str] = None,
) -> dict:
    """创建/更新 Webhook 配置。

    events: 触发事件列表，如 ["replay_success", "replay_failed", "schedule_complete"]
    """
    _ensure_webhook_dir()
    wid = webhook_id or str(uuid.uuid4())[:8]
    record = {
        "id": wid,
        "name": name,
        "url": url,
        "events": events or ["replay_success", "replay_failed"],
        "headers": headers or {},
        "enabled": enabled,
        "created_at": time.time(),
    }
    with _file_lock:
        with open(_webhook_path(wid), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def list_webhooks() -> list[dict]:
    """列出所有 Webhook 配置。"""
    _ensure_webhook_dir()
    result = []
    for fname in os.listdir(WEBHOOK_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(WEBHOOK_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                result.append(json.load(f))
        except Exception:
            continue
    result.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return result


def get_webhook(webhook_id: str) -> Optional[dict]:
    """获取单个 Webhook 配置。"""
    fpath = _webhook_path(webhook_id)
    if not os.path.isfile(fpath):
        return None
    with open(fpath, "r", encoding="utf-8") as f:
        return json.load(f)


def update_webhook(webhook_id: str, updates: dict) -> Optional[dict]:
    """更新 Webhook 配置字段。"""
    record = get_webhook(webhook_id)
    if not record:
        return None
    record.update(updates)
    record["id"] = webhook_id
    with _file_lock:
        with open(_webhook_path(webhook_id), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def delete_webhook(webhook_id: str) -> bool:
    """删除 Webhook 配置。"""
    fpath = _webhook_path(webhook_id)
    if os.path.isfile(fpath):
        os.remove(fpath)
        return True
    return False


def fire_webhooks(event: str, payload: dict) -> list[dict]:
    """触发匹配指定事件的所有 Webhook，发送 POST 请求。

    返回每个 Webhook 的发送结果列表。
    """
    results = []
    for wh in list_webhooks():
        if not wh.get("enabled", True):
            continue
        if event not in wh.get("events", []):
            continue
        body = json.dumps({
            "event": event,
            "timestamp": time.time(),
            "webhook_id": wh["id"],
            "webhook_name": wh["name"],
            "payload": payload,
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            wh["url"],
            data=body,
            headers={
                "Content-Type": "application/json",
                **wh.get("headers", {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                results.append({
                    "webhook_id": wh["id"],
                    "status": resp.status,
                    "success": True,
                })
        except Exception as exc:
            logger.warning("Webhook %s 发送失败: %s", wh["id"], exc)
            results.append({
                "webhook_id": wh["id"],
                "error": str(exc),
                "success": False,
            })
    return results


def fire_webhooks_async(event: str, payload: dict) -> None:
    """异步触发 Webhook（在后台线程中执行，不阻塞主流程）。"""
    t = threading.Thread(target=fire_webhooks, args=(event, payload), daemon=True)
    t.start()


# ------------------------------------------------------------------
# 工作流版本控制
# ------------------------------------------------------------------

VERSION_DIR = os.path.join(os.path.dirname(__file__), "workflow_versions")


def _ensure_version_dir() -> None:
    os.makedirs(VERSION_DIR, exist_ok=True)


def _version_dir_for(workflow_id: str) -> str:
    """每个工作流的版本目录。"""
    d = os.path.join(VERSION_DIR, workflow_id)
    os.makedirs(d, exist_ok=True)
    return d


def create_version(workflow_id: str, message: str = "") -> Optional[dict]:
    """为指定工作流创建一个新版本快照。

    读取当前工作流数据，保存为版本文件。
    返回版本元数据 dict，失败返回 None。
    """
    _ensure_version_dir()
    wf = get_workflow(workflow_id)
    if not wf:
        return None

    ver_dir = _version_dir_for(workflow_id)
    existing = _list_version_files(workflow_id)
    version_num = len(existing) + 1
    version_id = "{}_v{}".format(workflow_id, version_num)

    snapshot = {
        "version_id": version_id,
        "version_num": version_num,
        "workflow_id": workflow_id,
        "message": message or "版本 {}".format(version_num),
        "created_at": time.time(),
        "data": copy.deepcopy(wf),
    }

    fpath = os.path.join(ver_dir, "{}.json".format(version_id))
    with _file_lock:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    logger.info("工作流 %s 创建版本 v%d: %s", workflow_id, version_num, message)
    return {
        "version_id": version_id,
        "version_num": version_num,
        "message": snapshot["message"],
        "created_at": snapshot["created_at"],
    }


def _list_version_files(workflow_id: str) -> list[str]:
    """返回指定工作流的版本文件列表（按文件名排序）。"""
    ver_dir = _version_dir_for(workflow_id)
    files = [f for f in os.listdir(ver_dir) if f.endswith(".json")]
    files.sort()
    return files


def list_versions(workflow_id: str) -> list[dict]:
    """列出指定工作流的所有版本（元数据，不含完整data）。"""
    _ensure_version_dir()
    versions = []
    for fname in _list_version_files(workflow_id):
        fpath = os.path.join(_version_dir_for(workflow_id), fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                v = json.load(f)
            versions.append({
                "version_id": v.get("version_id"),
                "version_num": v.get("version_num"),
                "message": v.get("message", ""),
                "created_at": v.get("created_at"),
                "step_count": len(v.get("data", {}).get("steps", [])),
            })
        except Exception:
            continue
    versions.sort(key=lambda x: x.get("version_num", 0), reverse=True)
    return versions


def get_version(workflow_id: str, version_id: str) -> Optional[dict]:
    """获取指定版本的完整数据。"""
    fpath = os.path.join(_version_dir_for(workflow_id), "{}.json".format(version_id))
    if not os.path.isfile(fpath):
        return None
    with open(fpath, "r", encoding="utf-8") as f:
        return json.load(f)


def restore_version(workflow_id: str, version_id: str) -> Optional[dict]:
    """将工作流恢复到指定版本。

    会先对当前状态创建一个自动版本，然后用指定版本的数据覆盖。
    """
    version = get_version(workflow_id, version_id)
    if not version:
        return None

    # 先保存当前状态为一个自动版本
    create_version(workflow_id, message="自动备份 (回滚前)")

    # 用版本数据覆盖当前工作流
    ver_data = version.get("data", {})
    update_fields = {}
    if "steps" in ver_data:
        update_fields["steps"] = ver_data["steps"]
    if "name" in ver_data:
        update_fields["name"] = ver_data["name"]
    if "description" in ver_data:
        update_fields["description"] = ver_data["description"]
    if "variables" in ver_data:
        update_fields["variables"] = ver_data["variables"]

    result = update_workflow(workflow_id, update_fields)
    if result:
        logger.info("工作流 %s 已回滚到版本 %s", workflow_id, version_id)
    return result


def diff_versions(
    workflow_id: str, version_id_a: str, version_id_b: str,
) -> Optional[dict]:
    """比较两个版本的步骤差异。

    返回 {"added": [...], "removed": [...], "changed": [...], "summary": str}
    """
    va = get_version(workflow_id, version_id_a)
    vb = get_version(workflow_id, version_id_b)
    if not va or not vb:
        return None

    steps_a = va.get("data", {}).get("steps", [])
    steps_b = vb.get("data", {}).get("steps", [])

    added = []
    removed = []
    changed = []

    max_len = max(len(steps_a), len(steps_b))
    for i in range(max_len):
        sa = steps_a[i] if i < len(steps_a) else None
        sb = steps_b[i] if i < len(steps_b) else None
        if sa is None and sb is not None:
            added.append({"index": i, "step": sb})
        elif sa is not None and sb is None:
            removed.append({"index": i, "step": sa})
        elif sa != sb:
            changed.append({"index": i, "before": sa, "after": sb})

    return {
        "version_a": version_id_a,
        "version_b": version_id_b,
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": "新增 {} 步, 删除 {} 步, 修改 {} 步".format(
            len(added), len(removed), len(changed),
        ),
    }


# ------------------------------------------------------------------
# 子工作流 (Sub-workflow)
# ------------------------------------------------------------------

# 子工作流步骤 action 标识
SUB_WORKFLOW_ACTION = "call_workflow"

# 最大嵌套深度，防止无限递归
MAX_SUB_WORKFLOW_DEPTH = 5


def replay_with_sub_workflows(
    workflow_id: str,
    browser_engine: Any,
    variables: Optional[dict[str, str]] = None,
    on_step: Optional[Callable] = None,
    ai_config: Optional[dict] = None,
    depth: int = 0,
) -> dict:
    """支持子工作流调用的回放。

    工作流步骤中如果包含 action="call_workflow"、params={"workflow_id": "xxx"}，
    则递归调用该子工作流。
    """
    if depth > MAX_SUB_WORKFLOW_DEPTH:
        return {
            "success": False,
            "summary": "子工作流嵌套深度超过限制 ({})".format(MAX_SUB_WORKFLOW_DEPTH),
        }

    wf = get_workflow(workflow_id)
    if not wf:
        return {"success": False, "summary": "工作流不存在: {}".format(workflow_id)}

    steps = wf.get("steps", [])
    vars_merged = dict(wf.get("variables", {}))
    if variables:
        vars_merged.update(variables)

    results = []
    failed = False

    for i, step in enumerate(steps):
        action = step.get("action", "")
        params = step.get("params", {})

        # 应用变量替换
        params = _apply_variables(params, vars_merged)

        if action == SUB_WORKFLOW_ACTION:
            sub_wf_id = params.get("workflow_id", "")
            sub_vars = params.get("variables", {})
            if not sub_wf_id:
                results.append({
                    "step": i,
                    "action": action,
                    "error": "缺少 workflow_id 参数",
                })
                failed = True
                break

            if on_step:
                on_step({
                    "step": i,
                    "action": "call_workflow",
                    "workflow_id": sub_wf_id,
                    "depth": depth + 1,
                    "status": "calling",
                })

            sub_result = replay_with_sub_workflows(
                workflow_id=sub_wf_id,
                browser_engine=browser_engine,
                variables={**vars_merged, **sub_vars},
                on_step=on_step,
                ai_config=ai_config,
                depth=depth + 1,
            )
            results.append({
                "step": i,
                "action": "call_workflow",
                "workflow_id": sub_wf_id,
                "sub_result": sub_result,
            })
            if not sub_result.get("success"):
                failed = True
                break
            continue

        # 普通步骤：检查是否可回放
        if action not in PLAYABLE_ACTIONS:
            continue

        if on_step:
            on_step({
                "step": i,
                "total": len(steps),
                "action": action,
                "params": params,
                "depth": depth,
            })

        result = browser_engine.execute_action(action, params)
        results.append({"step": i, "action": action, "result": result})

        if result.get("error"):
            # AI 修复尝试
            if ai_config and ai_config.get("api_key"):
                fix_result = _ai_fallback_fix(
                    browser_engine=browser_engine,
                    step=step,
                    error=result["error"],
                    api_key=ai_config["api_key"],
                    base_url=ai_config.get("base_url", ""),
                    model=ai_config.get("model", "gpt-4o"),
                )
                if fix_result and fix_result.get("success"):
                    results[-1]["ai_fix"] = fix_result
                    continue
            failed = True
            break

        smart_wait(browser_engine, action)

    return {
        "success": not failed,
        "workflow_id": workflow_id,
        "depth": depth,
        "steps_executed": len(results),
        "steps_total": len(steps),
        "results": results,
        "summary": "子工作流 {} 执行{}".format(
            workflow_id,
            "成功" if not failed else "失败",
        ),
    }


def get_sub_workflow_references(workflow_id: str) -> list[dict]:
    """获取工作流中引用的所有子工作流。"""
    wf = get_workflow(workflow_id)
    if not wf:
        return []
    refs = []
    for i, step in enumerate(wf.get("steps", [])):
        if step.get("action") == SUB_WORKFLOW_ACTION:
            sub_id = step.get("params", {}).get("workflow_id", "")
            sub_wf = get_workflow(sub_id) if sub_id else None
            refs.append({
                "step_index": i,
                "workflow_id": sub_id,
                "exists": sub_wf is not None,
                "name": sub_wf.get("name", "") if sub_wf else "",
            })
    return refs


def validate_sub_workflow_chain(
    workflow_id: str, visited: Optional[set] = None,
) -> dict:
    """验证子工作流调用链是否有效（检测循环引用和缺失引用）。"""
    if visited is None:
        visited = set()

    if workflow_id in visited:
        return {
            "valid": False,
            "error": "检测到循环引用: {} 已在调用链中".format(workflow_id),
        }

    wf = get_workflow(workflow_id)
    if not wf:
        return {
            "valid": False,
            "error": "工作流不存在: {}".format(workflow_id),
        }

    visited.add(workflow_id)
    refs = get_sub_workflow_references(workflow_id)
    errors = []

    for ref in refs:
        if not ref["exists"]:
            errors.append("步骤 {} 引用的子工作流 {} 不存在".format(
                ref["step_index"], ref["workflow_id"],
            ))
            continue
        sub_check = validate_sub_workflow_chain(ref["workflow_id"], visited.copy())
        if not sub_check["valid"]:
            errors.append(sub_check["error"])

    if errors:
        return {"valid": False, "error": "; ".join(errors)}
    return {"valid": True, "workflow_id": workflow_id, "sub_count": len(refs)}
