# -*- coding: utf-8 -*-
"""Flask API 服务 - 提供 Web 接口和前端页面。"""

import json
import logging
import os
import queue
import re
import threading
import time
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS

from ai_agent import AIAgent, MODE_BROWSER, MODE_DESKTOP
from browser_engine import (
    BROWSER_MODE_BUILTIN,
    BROWSER_MODE_REUSE,
    BROWSER_MODE_STANDALONE,
    BrowserEngine,
)
from desktop_engine import DesktopEngine
from workflow_manager import (
    clear_execution_history,
    clone_workflow,
    create_version,
    delete_execution_record,
    delete_schedule,
    delete_webhook,
    delete_workflow,
    diff_versions,
    export_extracted_data,
    extract_data_from_page,
    extract_playable_steps,
    fire_webhooks_async,
    get_execution_record,
    get_schedule,
    get_sub_workflow_references,
    get_version,
    get_webhook,
    get_workflow,
    list_execution_history,
    list_schedules,
    list_versions,
    list_webhooks,
    list_workflows,
    loop_replay_workflow,
    parse_multi_files,
    parse_script_with_ai,
    parse_zip_file,
    replay_with_conditions,
    replay_with_extraction,
    replay_with_sub_workflows,
    replay_workflow,
    restore_version,
    safe_decode,
    save_schedule,
    save_webhook,
    save_workflow,
    set_scheduled_replay_context,
    toggle_schedule,
    update_schedule,
    update_webhook,
    update_workflow,
    validate_and_fix_workflow,
    validate_import_file,
    validate_sub_workflow_chain,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)

# ------------------------------------------------------------------
# 全局状态
# ------------------------------------------------------------------
browser_engine: Optional[BrowserEngine] = None
desktop_engine: Optional[DesktopEngine] = None
ai_agent: Optional[AIAgent] = None
task_thread: Optional[threading.Thread] = None
browser_thread: Optional[threading.Thread] = None
browser_cmd_queue: queue.Queue = queue.Queue()
event_queues: list[queue.Queue] = []
event_queues_lock = threading.Lock()
current_task_status = {"running": False, "task": "", "steps": []}
_start_time = time.time()


def get_config() -> dict:
    # browser_mode 优先级：BROWSER_MODE > CONNECT_EXISTING 兼容推断
    raw_mode = os.getenv("BROWSER_MODE", "").strip().lower()
    if raw_mode in (BROWSER_MODE_REUSE, BROWSER_MODE_STANDALONE, BROWSER_MODE_BUILTIN):
        browser_mode = raw_mode
    elif os.getenv("CONNECT_EXISTING", "true").lower() == "true":
        browser_mode = BROWSER_MODE_REUSE
    else:
        browser_mode = BROWSER_MODE_STANDALONE

    return {
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
        "headless": os.getenv("HEADLESS", "false").lower() == "true",
        "browser_type": os.getenv("BROWSER_TYPE", "chromium"),
        "browser_mode": browser_mode,
        "cdp_url": os.getenv("CDP_URL", "http://127.0.0.1:9222"),
        "viewport_width": int(os.getenv("VIEWPORT_WIDTH", "1280")),
        "viewport_height": int(os.getenv("VIEWPORT_HEIGHT", "720")),
    }


def broadcast_event(event_type: str, data: dict) -> None:
    """向所有 SSE 客户端广播事件（线程安全）。"""
    msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with event_queues_lock:
        dead = []
        for q in event_queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        if dead:
            for q in dead:
                try:
                    event_queues.remove(q)
                except ValueError:
                    pass


def _get_ai_config() -> Optional[dict[str, str]]:
    """获取 LLM 配置。"""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
    }


# ------------------------------------------------------------------
# 路由 - 页面
# ------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ------------------------------------------------------------------
# 路由 - API
# ------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def api_status():
    """获取当前状态。"""
    return jsonify({
        "browser_running": browser_engine is not None and browser_engine.is_connected,
        "desktop_running": desktop_engine is not None and desktop_engine.is_running,
        "task_running": current_task_status["running"],
        "current_task": current_task_status["task"],
        "steps_count": len(current_task_status["steps"]),
    })


@app.route("/api/browser/start", methods=["POST"])
def api_browser_start():
    """启动浏览器（在专属线程中运行，解决 Playwright 线程安全问题）。"""
    global browser_engine, browser_thread
    if browser_engine and browser_engine.is_connected:
        return jsonify({"success": True, "message": "浏览器已在运行"})

    # 前端可通过 POST body 覆盖默认 mode
    data = request.get_json(silent=True) or {}
    config = get_config()
    browser_mode = data.get("browser_mode", config["browser_mode"])

    ready_event = threading.Event()
    error_holder = [None]

    def browser_worker():
        """浏览器专属线程：启动浏览器 → 循环处理调用请求。"""
        global browser_engine
        try:
            engine = BrowserEngine(
                headless=config["headless"],
                browser_type=config["browser_type"],
                browser_mode=browser_mode,
                cdp_url=config["cdp_url"],
                viewport_width=config["viewport_width"],
                viewport_height=config["viewport_height"],
            )
            engine.start()
            browser_engine = engine
            ready_event.set()

            # 持续处理来自其他线程的 Playwright 调用
            while browser_engine is not None and browser_engine.is_connected:
                browser_engine.process_pending_calls(timeout=0.05)
            # stop_requested 信号触发了 stop()，或 browser_engine 被置空
            # 无论哪种情况，线程均正常退出
        except Exception as exc:
            error_holder[0] = str(exc)
            ready_event.set()

    browser_thread = threading.Thread(target=browser_worker, daemon=True)
    browser_thread.start()
    # reuse 模式可能需要关闭 Chrome + 等待端口释放 + 重启 + 等待 CDP，给更长超时
    wait_timeout = 90 if browser_mode == BROWSER_MODE_REUSE else 30
    ready_event.wait(timeout=wait_timeout)

    if error_holder[0]:
        broadcast_event("browser", {"status": "error", "message": error_holder[0]})
        return jsonify({"error": error_holder[0]}), 500

    # 检查是否真的启动成功（防止 ready_event 超时但无错误的情况）
    if browser_engine is None or not browser_engine.is_connected:
        msg = "浏览器启动超时，请检查 Chrome 是否正常运行后重试。"
        broadcast_event("browser", {"status": "error", "message": msg})
        return jsonify({"error": msg}), 500

    broadcast_event("browser", {"status": "started", "mode": browser_mode})
    # 设置定时任务的全局浏览器引擎上下文
    set_scheduled_replay_context(browser_engine, broadcast_event)
    return jsonify({"success": True, "message": "浏览器已启动", "mode": browser_mode})


@app.route("/api/browser/stop", methods=["POST"])
def api_browser_stop():
    """停止浏览器。"""
    global browser_engine, browser_thread
    if browser_engine:
        engine = browser_engine
        browser_engine = None  # 先置空，让 worker 线程在 stop 完成后退出循环
        engine.request_stop()  # 线程安全：由 owner 线程在 process_pending_calls 中执行 stop()
        # 等待 worker 线程退出（给予足够时间完成清理）
        if browser_thread and browser_thread.is_alive():
            browser_thread.join(timeout=5)
        browser_thread = None
    broadcast_event("browser", {"status": "stopped"})
    return jsonify({"success": True, "message": "浏览器已关闭"})


@app.route("/api/browser/screenshot", methods=["GET"])
def api_browser_screenshot():
    """获取当前页面截图。"""
    if not browser_engine or not browser_engine.is_connected:
        return jsonify({"error": "浏览器未运行"}), 400
    result = browser_engine.screenshot()
    return jsonify({"success": True, "base64": result["base64"]})


@app.route("/api/task/run", methods=["POST"])
def api_task_run():
    """提交并执行 AI 任务（支持浏览器模式和桌面模式）。"""
    global ai_agent, task_thread

    if current_task_status["running"]:
        return jsonify({"error": "已有任务在执行中"}), 400

    data = request.get_json() or {}
    task_text = data.get("task", "").strip()
    user_images = data.get("images", [])  # [data:image/xxx;base64,...] 格式列表
    # 兼容旧的单图字段
    if not user_images and data.get("image"):
        user_images = [data["image"]]
    if not task_text and not user_images:
        return jsonify({"error": "任务内容不能为空"}), 400
    if not task_text:
        task_text = "请看我提供的图片并完成相关操作"

    # 判断操作模式
    task_mode = data.get("mode", MODE_BROWSER)

    if task_mode == MODE_DESKTOP:
        # 桌面模式：确保桌面引擎可用
        if not desktop_engine or not desktop_engine.is_running:
            return jsonify({"error": "请先启动桌面引擎"}), 400
    else:
        # 浏览器模式：确保浏览器已启动
        if not browser_engine or not browser_engine.is_connected:
            return jsonify({"error": "请先启动浏览器"}), 400

    config = get_config()
    if not config["api_key"]:
        return jsonify({"error": "未配置 OPENAI_API_KEY"}), 400

    ai_agent = AIAgent(
        browser=browser_engine,
        desktop=desktop_engine,
        api_key=config["api_key"],
        base_url=config["base_url"],
        model=config["model"],
        mode=task_mode,
    )

    current_task_status["running"] = True
    current_task_status["task"] = task_text
    current_task_status["steps"] = []
    current_task_status["mode"] = task_mode

    def on_step(step_info: dict) -> None:
        current_task_status["steps"].append(step_info)
        broadcast_event("step", step_info)

    def run_in_thread() -> None:
        try:
            result = ai_agent.run_task(task_text, on_step=on_step, user_images=user_images)
            # 自动提取可回放步骤，附加到完成事件
            playable = extract_playable_steps(result.get("steps", []))
            result["playable_steps"] = playable
            result["playable_count"] = len(playable)
            result["mode"] = task_mode
            broadcast_event("task_complete", result)
        except Exception as exc:
            broadcast_event("task_error", {"error": str(exc)})
        finally:
            current_task_status["running"] = False

    task_thread = threading.Thread(target=run_in_thread, daemon=True)
    task_thread.start()

    return jsonify({"success": True, "message": "任务已开始执行", "mode": task_mode})


@app.route("/api/task/stop", methods=["POST"])
def api_task_stop():
    """停止当前任务。"""
    if ai_agent and ai_agent.is_running:
        ai_agent.stop()
        return jsonify({"success": True, "message": "已请求停止任务"})
    return jsonify({"success": True, "message": "当前没有运行中的任务"})


@app.route("/api/task/steps", methods=["GET"])
def api_task_steps():
    """获取当前任务的步骤日志。"""
    return jsonify({
        "task": current_task_status["task"],
        "running": current_task_status["running"],
        "steps": current_task_status["steps"],
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    """获取当前配置（隐藏 API Key）。"""
    config = get_config()
    config["api_key"] = "***" if config["api_key"] else ""
    return jsonify(config)


@app.route("/api/browser/launch-hint", methods=["GET"])
def api_browser_launch_hint():
    """返回 Chrome 远程调试启动命令，供前端展示给用户。"""
    config = get_config()
    cdp_url = config["cdp_url"]
    port = cdp_url.split(":")[-1].rstrip("/")

    # 查找本地浏览器路径
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    browser_path = ""
    for p in chrome_paths:
        if os.path.isfile(p):
            browser_path = p
            break

    if browser_path:
        launch_cmd = f'"{browser_path}" --remote-debugging-port={port}'
    else:
        launch_cmd = f"chrome.exe --remote-debugging-port={port}"

    return jsonify({
        "port": port,
        "launch_cmd": launch_cmd,
        "browser_path": browser_path,
        "browser_mode": config["browser_mode"],
    })


@app.route("/api/config", methods=["POST"])
def api_update_config():
    """更新运行时配置（写入 .env 文件）。"""
    data = request.get_json() or {}
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    lines = []

    mapping = {
        "api_key": "OPENAI_API_KEY",
        "base_url": "OPENAI_BASE_URL",
        "model": "OPENAI_MODEL",
        "headless": "HEADLESS",
        "browser_type": "BROWSER_TYPE",
        "browser_mode": "BROWSER_MODE",
        "viewport_width": "VIEWPORT_WIDTH",
        "viewport_height": "VIEWPORT_HEIGHT",
    }

    # 读取现有 .env
    existing = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    existing[key.strip()] = val.strip()

    # 更新
    for param_key, env_key in mapping.items():
        if param_key in data and data[param_key] != "***":
            existing[env_key] = str(data[param_key])

    # 写回
    with open(env_path, "w", encoding="utf-8") as f:
        for key, val in existing.items():
            f.write(f"{key}={val}\n")

    # 重新加载
    load_dotenv(override=True)

    return jsonify({"success": True, "message": "配置已更新"})


# ------------------------------------------------------------------
# 路由 - 工作流（录制与回放）
# ------------------------------------------------------------------

workflow_replay_thread: Optional[threading.Thread] = None
workflow_replay_status = {"running": False, "workflow_id": "", "steps": []}
workflow_replay_stop_event = threading.Event()


@app.route("/api/workflows", methods=["GET"])
def api_list_workflows():
    """获取所有已保存的工作流列表。"""
    return jsonify({"success": True, "workflows": list_workflows()})


@app.route("/api/workflows", methods=["POST"])
def api_save_workflow():
    """保存当前任务为工作流。"""
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "工作流名称不能为空"}), 400

    # 可以直接传 steps，也可以从最近一次任务自动提取
    steps = data.get("steps")
    task = data.get("task", "")

    if not steps:
        # 从最近一次 AI 任务结果中提取
        if not current_task_status["steps"]:
            return jsonify({"error": "没有可保存的任务记录，请先执行一次任务"}), 400
        steps = extract_playable_steps(current_task_status["steps"])
        task = task or current_task_status.get("task", "")

    if not steps:
        return jsonify({"error": "任务中没有可录制的操作步骤"}), 400

    wf = save_workflow(
        name=name,
        task=task,
        steps=steps,
        description=data.get("description", ""),
        replay_config=data.get("replay_config"),
        mode=data.get("mode", current_task_status.get("mode", MODE_BROWSER)),
    )
    return jsonify({"success": True, "workflow": wf})


@app.route("/api/workflows/<workflow_id>", methods=["GET"])
def api_get_workflow(workflow_id: str):
    """获取工作流详情。"""
    wf = get_workflow(workflow_id)
    if wf is None:
        return jsonify({"error": "工作流不存在"}), 404
    return jsonify({"success": True, "workflow": wf})


@app.route("/api/workflows/<workflow_id>", methods=["PUT"])
def api_update_workflow(workflow_id: str):
    """更新工作流（名称、描述、步骤）。"""
    data = request.get_json() or {}
    wf = update_workflow(workflow_id, data)
    if wf is None:
        return jsonify({"error": "工作流不存在"}), 404
    return jsonify({"success": True, "workflow": wf})


@app.route("/api/workflows/<workflow_id>", methods=["DELETE"])
def api_delete_workflow(workflow_id: str):
    """删除工作流。"""
    if delete_workflow(workflow_id):
        return jsonify({"success": True, "message": "工作流已删除"})
    return jsonify({"error": "工作流不存在"}), 404


@app.route("/api/workflows/batch", methods=["DELETE"])
def api_batch_delete_workflows():
    """批量删除工作流。"""
    data = request.get_json() or {}
    ids = data.get("ids", [])
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "请提供要删除的工作流 ID 列表"}), 400
    deleted = []
    failed = []
    for wf_id in ids:
        if delete_workflow(wf_id):
            deleted.append(wf_id)
        else:
            failed.append(wf_id)
    return jsonify({
        "success": True,
        "deleted": deleted,
        "failed": failed,
        "message": f"成功删除 {len(deleted)} 个工作流",
    })


@app.route("/api/workflows/<workflow_id>/clone", methods=["POST"])
def api_clone_workflow(workflow_id: str):
    """克隆（复制）一个工作流。"""
    cloned = clone_workflow(workflow_id)
    if cloned is None:
        return jsonify({"error": "工作流不存在"}), 404
    return jsonify({"success": True, "workflow": cloned})


@app.route("/api/workflows/<workflow_id>/export", methods=["GET"])
def api_export_workflow(workflow_id: str):
    """导出工作流为 JSON 文件下载。"""
    wf = get_workflow(workflow_id)
    if wf is None:
        return jsonify({"error": "工作流不存在"}), 404

    # 构建干净的导出数据（去掉内部字段）
    export_data = {
        "name": wf["name"],
        "task": wf.get("task", ""),
        "description": wf.get("description", ""),
        "steps": wf.get("steps", []),
        "replay_config": wf.get("replay_config"),
        "exported_at": time.time(),
    }
    json_bytes = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")
    safe_name = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', wf["name"])[:50] or "workflow"

    return Response(
        json_bytes,
        mimetype="application/json",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''{}.json".format(safe_name),
        },
    )


@app.route("/api/workflows/<workflow_id>/replay", methods=["POST"])
def api_replay_workflow(workflow_id: str):
    """回放工作流（支持浏览器和桌面模式）。"""
    global workflow_replay_thread

    if workflow_replay_status["running"]:
        return jsonify({"error": "已有工作流在回放中"}), 400
    if current_task_status["running"]:
        return jsonify({"error": "已有 AI 任务在执行中"}), 400

    wf = get_workflow(workflow_id)
    if wf is None:
        return jsonify({"error": "工作流不存在"}), 404

    data = request.get_json(silent=True) or {}
    variables = data.get("variables", {})

    # 判断回放引擎：桌面 or 浏览器
    replay_mode = data.get("mode", wf.get("mode", MODE_BROWSER))
    if replay_mode == MODE_DESKTOP:
        if not desktop_engine or not desktop_engine.is_running:
            return jsonify({"error": "请先启动桌面引擎"}), 400
        engine = desktop_engine
    else:
        if not browser_engine or not browser_engine.is_connected:
            return jsonify({"error": "请先启动浏览器"}), 400
        engine = browser_engine

    workflow_replay_status["running"] = True
    workflow_replay_status["workflow_id"] = workflow_id
    workflow_replay_status["steps"] = []
    workflow_replay_stop_event.clear()

    def on_step(step_info: dict) -> None:
        workflow_replay_status["steps"].append(step_info)
        broadcast_event("replay_step", step_info)

    # 读取当前 LLM 配置，用于智能降级
    ai_config = _get_ai_config()

    def run_replay() -> None:
        try:
            result = replay_workflow(
                workflow_id=workflow_id,
                browser_engine=engine,
                variables=variables,
                on_step=on_step,
                ai_config=ai_config,
                stop_event=workflow_replay_stop_event,
            )
            broadcast_event("replay_complete", result)
            # Webhook 通知
            event_name = "replay_success" if result.get("success") else "replay_failed"
            fire_webhooks_async(event_name, {
                "workflow_id": workflow_id,
                "workflow_name": wf.get("name", ""),
                "success": result.get("success", False),
                "summary": result.get("summary", ""),
            })
        except Exception as exc:
            broadcast_event("replay_error", {"error": str(exc)})
            fire_webhooks_async("replay_failed", {
                "workflow_id": workflow_id,
                "workflow_name": wf.get("name", ""),
                "error": str(exc),
            })
        finally:
            workflow_replay_status["running"] = False

    workflow_replay_thread = threading.Thread(target=run_replay, daemon=True)
    workflow_replay_thread.start()

    return jsonify({
        "success": True,
        "message": "工作流开始回放",
        "workflow_name": wf["name"],
        "steps_count": len(wf.get("steps", [])),
        "mode": replay_mode,
    })


# 循环回放状态
loop_replay_thread: Optional[threading.Thread] = None
loop_replay_status = {"running": False}
loop_replay_stop_event = threading.Event()


@app.route("/api/workflows/<workflow_id>/loop-replay", methods=["POST"])
def api_loop_replay_workflow(workflow_id: str):
    """循环回放工作流 — 对列表中每一行自动执行已录制的操作。

    请求 body:
        row_selector: 行 CSS 选择器（如 "table tbody tr"）
        condition: 执行条件（自然语言，可选）
        max_rows: 最大处理行数（默认 100）
    """
    global loop_replay_thread

    if loop_replay_status["running"]:
        return jsonify({"error": "已有循环回放在执行中"}), 400
    if workflow_replay_status["running"]:
        return jsonify({"error": "已有工作流在回放中"}), 400
    if current_task_status["running"]:
        return jsonify({"error": "已有 AI 任务在执行中"}), 400
    if not browser_engine or not browser_engine.is_connected:
        return jsonify({"error": "请先启动浏览器"}), 400

    wf = get_workflow(workflow_id)
    if wf is None:
        return jsonify({"error": "工作流不存在"}), 404

    data = request.get_json(silent=True) or {}
    row_selector = data.get("row_selector", "").strip()
    if not row_selector:
        return jsonify({"error": "请提供行选择器 (row_selector)"}), 400

    condition = data.get("condition", "").strip()
    max_rows = data.get("max_rows", 100)

    # LLM 配置（可选 — 简单条件和选择器适配不需要 AI）
    ai_config = _get_ai_config()

    loop_replay_status["running"] = True
    loop_replay_stop_event.clear()

    def on_step(step_info: dict) -> None:
        broadcast_event("loop_replay_step", step_info)

    def run_loop_replay() -> None:
        try:
            result = loop_replay_workflow(
                workflow_id=workflow_id,
                browser_engine=browser_engine,
                row_selector=row_selector,
                condition=condition,
                max_rows=max_rows,
                on_step=on_step,
                ai_config=ai_config,
                stop_event=loop_replay_stop_event,
            )
            broadcast_event("loop_replay_complete", result)
            event_name = "replay_success" if result.get("success") else "replay_failed"
            fire_webhooks_async(event_name, {
                "workflow_id": workflow_id,
                "type": "loop_replay",
                "success": result.get("success", False),
                "summary": result.get("summary", ""),
            })
        except Exception as exc:
            broadcast_event("loop_replay_error", {"error": str(exc)})
            fire_webhooks_async("replay_failed", {
                "workflow_id": workflow_id,
                "type": "loop_replay",
                "error": str(exc),
            })
        finally:
            loop_replay_status["running"] = False

    loop_replay_thread = threading.Thread(target=run_loop_replay, daemon=True)
    loop_replay_thread.start()

    return jsonify({
        "success": True,
        "message": "循环回放已启动",
        "workflow_name": wf["name"],
        "row_selector": row_selector,
        "condition": condition or "（无条件）",
    })


@app.route("/api/workflows/stop-replay", methods=["POST"])
def api_stop_replay():
    """停止当前正在进行的回放或循环回放。"""
    stopped = False
    if workflow_replay_status["running"]:
        workflow_replay_stop_event.set()
        stopped = True
    if loop_replay_status["running"]:
        loop_replay_stop_event.set()
        stopped = True

    if stopped:
        broadcast_event("replay_stopped", {"message": "回放已停止"})
        return jsonify({"success": True, "message": "已请求停止回放"})
    return jsonify({"success": True, "message": "当前没有运行中的回放"})


@app.route("/api/workflows/<workflow_id>/quick-run", methods=["POST"])
def api_quick_run_workflow(workflow_id: str):
    """一键执行工作流 — 使用保存的 replay_config 直接运行。

    如果工作流保存了回放配置（replay_config），直接按配置执行，
    无需用户再次填写参数。
    """
    global workflow_replay_thread, loop_replay_thread

    if workflow_replay_status["running"] or loop_replay_status["running"]:
        return jsonify({"error": "已有工作流在执行中"}), 400
    if current_task_status["running"]:
        return jsonify({"error": "已有 AI 任务在执行中"}), 400
    if not browser_engine or not browser_engine.is_connected:
        return jsonify({"error": "请先启动浏览器"}), 400

    wf = get_workflow(workflow_id)
    if wf is None:
        return jsonify({"error": "工作流不存在"}), 404

    config = wf.get("replay_config") or {}
    replay_type = config.get("type", "simple")

    # LLM 配置
    ai_config = _get_ai_config()

    if replay_type == "loop":
        row_selector = config.get("row_selector", "")
        if not row_selector:
            return jsonify({"error": "工作流配置缺少行选择器 (row_selector)"}), 400

        condition = config.get("condition", "")
        max_rows = config.get("max_rows", 100)

        loop_replay_status["running"] = True
        loop_replay_stop_event.clear()

        def on_loop_step(step_info: dict) -> None:
            broadcast_event("loop_replay_step", step_info)

        def run_loop() -> None:
            try:
                result = loop_replay_workflow(
                    workflow_id=workflow_id,
                    browser_engine=browser_engine,
                    row_selector=row_selector,
                    condition=condition,
                    max_rows=max_rows,
                    on_step=on_loop_step,
                    ai_config=ai_config,
                    stop_event=loop_replay_stop_event,
                )
                broadcast_event("loop_replay_complete", result)
            except Exception as exc:
                broadcast_event("loop_replay_error", {"error": str(exc)})
            finally:
                loop_replay_status["running"] = False

        loop_replay_thread = threading.Thread(target=run_loop, daemon=True)
        loop_replay_thread.start()

        return jsonify({
            "success": True,
            "message": "一键执行: 循环回放已启动",
            "workflow_name": wf["name"],
            "replay_type": "loop",
            "row_selector": row_selector,
            "condition": condition or "（无条件）",
        })

    # 默认: simple 回放
    variables = config.get("variables", {})

    workflow_replay_status["running"] = True
    workflow_replay_status["workflow_id"] = workflow_id
    workflow_replay_status["steps"] = []
    workflow_replay_stop_event.clear()

    def on_step(step_info: dict) -> None:
        workflow_replay_status["steps"].append(step_info)
        broadcast_event("replay_step", step_info)

    def run_simple() -> None:
        try:
            result = replay_workflow(
                workflow_id=workflow_id,
                browser_engine=browser_engine,
                variables=variables,
                on_step=on_step,
                ai_config=ai_config,
                stop_event=workflow_replay_stop_event,
            )
            broadcast_event("replay_complete", result)
        except Exception as exc:
            broadcast_event("replay_error", {"error": str(exc)})
        finally:
            workflow_replay_status["running"] = False

    workflow_replay_thread = threading.Thread(target=run_simple, daemon=True)
    workflow_replay_thread.start()

    return jsonify({
        "success": True,
        "message": "一键执行: 回放已启动",
        "workflow_name": wf["name"],
        "replay_type": "simple",
    })


@app.route("/api/workflows/<workflow_id>/save-config", methods=["POST"])
def api_save_workflow_config(workflow_id: str):
    """为已有工作流保存/更新回放配置。"""
    wf = get_workflow(workflow_id)
    if wf is None:
        return jsonify({"error": "工作流不存在"}), 404

    data = request.get_json() or {}
    replay_config = data.get("replay_config")
    if not replay_config:
        return jsonify({"error": "replay_config 不能为空"}), 400

    updated = update_workflow(workflow_id, {"replay_config": replay_config})
    return jsonify({"success": True, "workflow": {
        "id": updated["id"],
        "name": updated["name"],
        "replay_config": updated.get("replay_config"),
    }})


# ------------------------------------------------------------------
# 路由 - 工作流导入（外部脚本 → 内部工作流）
# ------------------------------------------------------------------

import_validate_thread: Optional[threading.Thread] = None
import_validate_status = {"running": False}


@app.route("/api/workflows/import-text", methods=["POST"])
def api_import_text():
    """通过粘贴脚本内容导入工作流。

    请求体:
        {
            "content": "脚本内容",
            "format": "auto|json|selenium|playwright|text",
            "name": "工作流名称（可选）"
        }
    """
    data = request.get_json() or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "脚本内容不能为空"}), 400

    format_hint = data.get("format", "auto")
    name = data.get("name", "").strip() or "导入的工作流"
    ai_config = _get_ai_config()

    # 解析脚本
    result = parse_script_with_ai(content, format_hint, ai_config)
    steps = result.get("steps", [])

    if not steps:
        return jsonify({
            "error": "无法解析脚本，请检查格式是否正确",
            "format_detected": result.get("format_detected"),
            "warnings": result.get("warnings", []),
        }), 400

    # 保存工作流
    wf = save_workflow(
        name=name,
        task=f"导入的{result['format_detected']}脚本",
        steps=steps,
        description=f"从{result['format_detected']}脚本导入，共 {len(steps)} 步"
                    + (", AI 参与解析" if result.get("ai_used") else ", 本地解析"),
        replay_config={"type": "simple"},
    )

    # 统计 needs_fix 数量
    needs_fix_count = sum(1 for s in steps if s.get("needs_fix"))

    return jsonify({
        "success": True,
        "workflow": {
            "id": wf["id"],
            "name": wf["name"],
            "steps_count": len(steps),
            "format_detected": result.get("format_detected"),
            "ai_used": result.get("ai_used", False),
            "needs_fix_count": needs_fix_count,
            "warnings": result.get("warnings", []),
        },
    })


@app.route("/api/workflows/import-file", methods=["POST"])
def api_import_file():
    """通过上传文件导入工作流（支持单文件、多文件、ZIP 包）。

    表单字段:
        file / files[]: 上传的文件，支持多个 (.json/.py/.txt/.zip)
        format: auto|json|selenium|playwright|text（可选）
        name: 工作流名称（可选）
    """
    # 支持 files[] 多文件上传和 file 单文件上传
    uploaded_files = request.files.getlist("files[]") or request.files.getlist("file")
    if not uploaded_files or not uploaded_files[0].filename:
        return jsonify({"error": "请选择要上传的文件"}), 400

    format_hint = request.form.get("format", "auto")
    name = request.form.get("name", "").strip()
    ai_config = _get_ai_config()

    # 单文件 ZIP → 直接解析 ZIP
    if len(uploaded_files) == 1 and uploaded_files[0].filename.lower().endswith(".zip"):
        zip_file = uploaded_files[0]
        raw_bytes = zip_file.read()
        result = parse_zip_file(raw_bytes, format_hint, ai_config)
        steps = result.get("steps", [])
        if not steps:
            return jsonify({
                "error": "无法从 ZIP 中解析出有效步骤",
                "format_detected": result.get("format_detected"),
                "warnings": result.get("warnings", []),
            }), 400

        if not name:
            name = os.path.splitext(zip_file.filename)[0]

        files_parsed = result.get("files_parsed", [])
        wf = save_workflow(
            name=name,
            task="从 ZIP 包 {} 导入（{}）".format(zip_file.filename, ", ".join(files_parsed)),
            steps=steps,
            description="从 {} 导入，包含 {} 个文件，共 {} 步".format(
                zip_file.filename, len(files_parsed), len(steps),
            ),
            replay_config={"type": "simple"},
        )

        needs_fix_count = sum(1 for s in steps if s.get("needs_fix"))
        return jsonify({
            "success": True,
            "workflow": {
                "id": wf["id"],
                "name": wf["name"],
                "steps_count": len(steps),
                "format_detected": result.get("format_detected"),
                "ai_used": result.get("ai_used", False),
                "needs_fix_count": needs_fix_count,
                "warnings": result.get("warnings", []),
                "files_parsed": files_parsed,
            },
        })

    # 多文件上传
    if len(uploaded_files) > 1:
        files_dict: dict[str, bytes] = {}
        for f in uploaded_files:
            if f.filename:
                files_dict[f.filename] = f.read()

        result = parse_multi_files(files_dict, format_hint, ai_config)
        steps = result.get("steps", [])
        if not steps:
            return jsonify({
                "error": "无法从上传的文件中解析出有效步骤",
                "format_detected": result.get("format_detected"),
                "warnings": result.get("warnings", []),
            }), 400

        if not name:
            name = "多文件导入（{} 个文件）".format(len(uploaded_files))

        files_parsed = result.get("files_parsed", [])
        wf = save_workflow(
            name=name,
            task="从 {} 个文件导入".format(len(uploaded_files)),
            steps=steps,
            description="多文件导入，包含 {} 个文件，共 {} 步".format(
                len(files_parsed), len(steps),
            ),
            replay_config={"type": "simple"},
        )

        needs_fix_count = sum(1 for s in steps if s.get("needs_fix"))
        return jsonify({
            "success": True,
            "workflow": {
                "id": wf["id"],
                "name": wf["name"],
                "steps_count": len(steps),
                "format_detected": result.get("format_detected"),
                "ai_used": result.get("ai_used", False),
                "needs_fix_count": needs_fix_count,
                "warnings": result.get("warnings", []),
                "files_parsed": files_parsed,
            },
        })

    # 单文件上传（非 ZIP）
    file = uploaded_files[0]
    raw_bytes = file.read()

    # 安全校验
    is_valid, err_msg = validate_import_file(raw_bytes, file.filename)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    # 解码
    try:
        content = safe_decode(raw_bytes)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not content.strip():
        return jsonify({"error": "文件内容为空"}), 400

    if not name:
        name = os.path.splitext(file.filename)[0]

    result = parse_script_with_ai(content, format_hint, ai_config)
    steps = result.get("steps", [])

    if not steps:
        return jsonify({
            "error": "无法解析文件 {}，请检查格式".format(file.filename),
            "format_detected": result.get("format_detected"),
            "warnings": result.get("warnings", []),
        }), 400

    wf = save_workflow(
        name=name,
        task="从文件 {} 导入".format(file.filename),
        steps=steps,
        description="从 {} ({}) 导入，共 {} 步".format(
            file.filename, result["format_detected"], len(steps),
        ),
        replay_config={"type": "simple"},
    )

    needs_fix_count = sum(1 for s in steps if s.get("needs_fix"))

    return jsonify({
        "success": True,
        "workflow": {
            "id": wf["id"],
            "name": wf["name"],
            "steps_count": len(steps),
            "format_detected": result.get("format_detected"),
            "ai_used": result.get("ai_used", False),
            "needs_fix_count": needs_fix_count,
            "warnings": result.get("warnings", []),
        },
    })


@app.route("/api/workflows/<workflow_id>/validate-fix", methods=["POST"])
def api_validate_fix_workflow(workflow_id: str):
    """验证导入的工作流并尝试 AI 自动修复。

    需要浏览器已启动并导航到目标页面。
    """
    global import_validate_thread

    if import_validate_status["running"]:
        return jsonify({"error": "已有验证任务在进行中"}), 400
    if not browser_engine or not browser_engine.is_connected:
        return jsonify({"error": "请先启动浏览器并导航到目标页面"}), 400

    wf = get_workflow(workflow_id)
    if wf is None:
        return jsonify({"error": "工作流不存在"}), 404

    steps = wf.get("steps", [])
    if not steps:
        return jsonify({"error": "工作流没有步骤"}), 400

    ai_config = _get_ai_config()
    import_validate_status["running"] = True

    def on_progress(info: dict) -> None:
        broadcast_event("import_validate_progress", info)

    def run_validate() -> None:
        try:
            result = validate_and_fix_workflow(
                steps=steps,
                browser_engine=browser_engine,
                ai_config=ai_config,
                on_progress=on_progress,
            )
            # 保存修复后的步骤
            if result["fixed"] > 0:
                update_workflow(workflow_id, {"steps": result["steps"]})

            broadcast_event("import_validate_complete", {
                "workflow_id": workflow_id,
                "total": result["total"],
                "valid": result["valid"],
                "fixed": result["fixed"],
                "failed": result["failed"],
                "details": result["details"],
            })
        except Exception as exc:
            broadcast_event("import_validate_error", {"error": str(exc)})
        finally:
            import_validate_status["running"] = False

    import_validate_thread = threading.Thread(target=run_validate, daemon=True)
    import_validate_thread.start()

    return jsonify({
        "success": True,
        "message": "验证+修复已启动",
        "steps_count": len(steps),
    })


# ------------------------------------------------------------------
# 路由 - 健康检查
# ------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def api_health():
    """健康检查端点 — 用于监控和可用性探测。"""
    return jsonify({
        "status": "ok",
        "browser_running": browser_engine is not None and browser_engine.is_connected,
        "task_running": current_task_status["running"],
        "replay_running": workflow_replay_status["running"] or loop_replay_status["running"],
        "uptime": time.time() - _start_time,
    })


# ------------------------------------------------------------------
# 路由 - 执行历史
# ------------------------------------------------------------------

@app.route("/api/execution-history", methods=["GET"])
def api_list_execution_history():
    """列出执行历史记录。支持 ?workflow_id= 过滤和 ?limit= 限制数量。"""
    workflow_id = request.args.get("workflow_id")
    limit = request.args.get("limit", 50, type=int)
    records = list_execution_history(workflow_id=workflow_id, limit=limit)
    return jsonify({"success": True, "records": records})


@app.route("/api/execution-history/<execution_id>", methods=["GET"])
def api_get_execution_record(execution_id: str):
    """获取单条执行记录详情（含步骤）。"""
    record = get_execution_record(execution_id)
    if record is None:
        return jsonify({"success": False, "error": "记录不存在"}), 404
    return jsonify({"success": True, "record": record})


@app.route("/api/execution-history/<execution_id>", methods=["DELETE"])
def api_delete_execution_record(execution_id: str):
    """删除单条执行记录。"""
    ok = delete_execution_record(execution_id)
    if not ok:
        return jsonify({"success": False, "error": "记录不存在"}), 404
    return jsonify({"success": True})


@app.route("/api/execution-history", methods=["DELETE"])
def api_clear_execution_history():
    """清空执行历史。支持 ?workflow_id= 过滤。"""
    workflow_id = request.args.get("workflow_id")
    count = clear_execution_history(workflow_id=workflow_id)
    return jsonify({"success": True, "deleted": count})


# ------------------------------------------------------------------
# 路由 - 数据提取与导出
# ------------------------------------------------------------------

@app.route("/api/extract-data", methods=["POST"])
def api_extract_data():
    """从当前页面提取数据。"""
    if browser_engine is None or not browser_engine.is_connected:
        return jsonify({"success": False, "error": "浏览器未连接"}), 400

    body = request.get_json(silent=True) or {}
    extract_steps = body.get("steps", [])
    if not extract_steps:
        return jsonify({"success": False, "error": "未提供提取步骤"}), 400

    try:
        data = extract_data_from_page(browser_engine, extract_steps)
        return jsonify({"success": True, "data": data})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/extract-data/export", methods=["POST"])
def api_export_extracted_data():
    """导出已提取的数据为 JSON/CSV。"""
    body = request.get_json(silent=True) or {}
    data = body.get("data", [])
    fmt = body.get("format", "json")

    if not data:
        return jsonify({"success": False, "error": "无数据可导出"}), 400

    content = export_extracted_data(data, fmt=fmt)
    mime = "application/json" if fmt == "json" else "text/csv"
    return Response(content, mimetype=mime, headers={
        "Content-Disposition": "attachment; filename=extracted_data.{}".format(fmt),
    })


@app.route("/api/replay-with-extraction", methods=["POST"])
def api_replay_with_extraction():
    """回放工作流后提取数据。"""
    if browser_engine is None or not browser_engine.is_connected:
        return jsonify({"success": False, "error": "浏览器未连接"}), 400

    body = request.get_json(silent=True) or {}
    workflow_id = body.get("workflow_id")
    extract_steps = body.get("extract_steps", [])
    variables = body.get("variables", {})

    if not workflow_id:
        return jsonify({"success": False, "error": "缺少 workflow_id"}), 400

    config = get_config()
    ai_config = None
    if config.get("openai_api_key"):
        ai_config = {
            "api_key": config["openai_api_key"],
            "base_url": config.get("openai_base_url", "https://api.openai.com/v1"),
            "model": config.get("openai_model", "gpt-4o"),
        }

    stop_event = threading.Event()

    def run():
        result = replay_with_extraction(
            workflow_id=workflow_id,
            browser_engine=browser_engine,
            extract_steps=extract_steps,
            variables=variables,
            on_step=lambda s: broadcast_event("replay_step", s),
            ai_config=ai_config,
            stop_event=stop_event,
        )
        if result.get("success"):
            broadcast_event("replay_complete", result)
        else:
            broadcast_event("replay_error", result)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return jsonify({"success": True, "message": "回放+提取已启动"})


# ------------------------------------------------------------------
# 路由 - 条件分支回放
# ------------------------------------------------------------------

@app.route("/api/replay-with-conditions", methods=["POST"])
def api_replay_with_conditions():
    """带条件分支的回放。"""
    if browser_engine is None or not browser_engine.is_connected:
        return jsonify({"success": False, "error": "浏览器未连接"}), 400

    body = request.get_json(silent=True) or {}
    workflow_id = body.get("workflow_id")
    step_conditions_raw = body.get("step_conditions", {})
    variables = body.get("variables", {})

    if not workflow_id:
        return jsonify({"success": False, "error": "缺少 workflow_id"}), 400

    # step_conditions 的 key 转为 int
    step_conditions = {int(k): v for k, v in step_conditions_raw.items()}

    config = get_config()
    ai_config = None
    if config.get("openai_api_key"):
        ai_config = {
            "api_key": config["openai_api_key"],
            "base_url": config.get("openai_base_url", "https://api.openai.com/v1"),
            "model": config.get("openai_model", "gpt-4o"),
        }

    stop_event = threading.Event()

    def run():
        result = replay_with_conditions(
            workflow_id=workflow_id,
            browser_engine=browser_engine,
            step_conditions=step_conditions,
            variables=variables,
            on_step=lambda s: broadcast_event("replay_step", s),
            ai_config=ai_config,
            stop_event=stop_event,
        )
        if result.get("success"):
            broadcast_event("replay_complete", result)
        else:
            broadcast_event("replay_error", result)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return jsonify({"success": True, "message": "条件回放已启动"})


# ------------------------------------------------------------------
# 路由 - 定时执行
# ------------------------------------------------------------------

@app.route("/api/schedules", methods=["GET"])
def api_list_schedules():
    """列出所有定时任务。"""
    schedules = list_schedules()
    return jsonify({"success": True, "schedules": schedules})


@app.route("/api/schedules", methods=["POST"])
def api_create_schedule():
    """创建定时任务。"""
    body = request.get_json(silent=True) or {}
    workflow_id = body.get("workflow_id")
    trigger_type = body.get("trigger_type", "interval")
    trigger_args = body.get("trigger_args", {})
    variables = body.get("variables", {})
    enabled = body.get("enabled", True)

    if not workflow_id:
        return jsonify({"success": False, "error": "缺少 workflow_id"}), 400

    config = get_config()
    ai_config = None
    if config.get("openai_api_key"):
        ai_config = {
            "api_key": config["openai_api_key"],
            "base_url": config.get("openai_base_url", "https://api.openai.com/v1"),
            "model": config.get("openai_model", "gpt-4o"),
        }

    result = save_schedule(
        workflow_id=workflow_id,
        trigger_type=trigger_type,
        trigger_args=trigger_args,
        variables=variables,
        ai_config=ai_config,
        enabled=enabled,
    )
    return jsonify(result)


@app.route("/api/schedules/<schedule_id>", methods=["GET"])
def api_get_schedule(schedule_id: str):
    """获取单个定时任务。"""
    schedule = get_schedule(schedule_id)
    if schedule is None:
        return jsonify({"success": False, "error": "定时任务不存在"}), 404
    return jsonify({"success": True, "schedule": schedule})


@app.route("/api/schedules/<schedule_id>", methods=["PUT"])
def api_update_schedule(schedule_id: str):
    """更新定时任务。"""
    body = request.get_json(silent=True) or {}
    result = update_schedule(schedule_id, body)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
def api_delete_schedule(schedule_id: str):
    """删除定时任务。"""
    ok = delete_schedule(schedule_id)
    if not ok:
        return jsonify({"success": False, "error": "定时任务不存在"}), 404
    return jsonify({"success": True})


@app.route("/api/schedules/<schedule_id>/toggle", methods=["POST"])
def api_toggle_schedule(schedule_id: str):
    """启用/禁用定时任务。"""
    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled", True)
    result = toggle_schedule(schedule_id, enabled)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


# ------------------------------------------------------------------
# Webhook 管理 API
# ------------------------------------------------------------------

@app.route("/api/webhooks", methods=["GET"])
def api_list_webhooks():
    """列出所有 Webhook。"""
    return jsonify(list_webhooks())


@app.route("/api/webhooks", methods=["POST"])
def api_create_webhook():
    """创建 Webhook。"""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "")
    url = body.get("url", "")
    if not name or not url:
        return jsonify({"error": "name 和 url 为必填字段"}), 400
    result = save_webhook(
        name=name,
        url=url,
        events=body.get("events"),
        headers=body.get("headers"),
        enabled=body.get("enabled", True),
    )
    return jsonify(result), 201


@app.route("/api/webhooks/<webhook_id>", methods=["GET"])
def api_get_webhook(webhook_id: str):
    """获取单个 Webhook。"""
    wh = get_webhook(webhook_id)
    if not wh:
        return jsonify({"error": "Webhook 不存在"}), 404
    return jsonify(wh)


@app.route("/api/webhooks/<webhook_id>", methods=["PUT"])
def api_update_webhook(webhook_id: str):
    """更新 Webhook。"""
    body = request.get_json(silent=True) or {}
    result = update_webhook(webhook_id, body)
    if not result:
        return jsonify({"error": "Webhook 不存在"}), 404
    return jsonify(result)


@app.route("/api/webhooks/<webhook_id>", methods=["DELETE"])
def api_delete_webhook(webhook_id: str):
    """删除 Webhook。"""
    ok = delete_webhook(webhook_id)
    if not ok:
        return jsonify({"error": "Webhook 不存在"}), 404
    return jsonify({"success": True})


@app.route("/api/webhooks/test", methods=["POST"])
def api_test_webhook():
    """测试 Webhook 发送。"""
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    if not url:
        return jsonify({"error": "url 为必填字段"}), 400
    import urllib.request as _ur
    test_body = json.dumps({
        "event": "test",
        "timestamp": time.time(),
        "payload": {"message": "Webhook 测试消息"},
    }).encode("utf-8")
    req = _ur.Request(url, data=test_body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _ur.urlopen(req, timeout=10) as resp:
            return jsonify({"success": True, "status": resp.status})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 502


# ------------------------------------------------------------------
# 工作流版本管理 API
# ------------------------------------------------------------------

@app.route("/api/workflows/<workflow_id>/versions", methods=["GET"])
def api_list_versions(workflow_id: str):
    """列出工作流的所有版本。"""
    return jsonify(list_versions(workflow_id))


@app.route("/api/workflows/<workflow_id>/versions", methods=["POST"])
def api_create_version(workflow_id: str):
    """为工作流创建新版本。"""
    body = request.get_json(silent=True) or {}
    message = body.get("message", "")
    result = create_version(workflow_id, message)
    if not result:
        return jsonify({"error": "工作流不存在"}), 404
    return jsonify(result), 201


@app.route("/api/workflows/<workflow_id>/versions/<version_id>", methods=["GET"])
def api_get_version(workflow_id: str, version_id: str):
    """获取指定版本的完整数据。"""
    v = get_version(workflow_id, version_id)
    if not v:
        return jsonify({"error": "版本不存在"}), 404
    return jsonify(v)


@app.route("/api/workflows/<workflow_id>/versions/<version_id>/restore", methods=["POST"])
def api_restore_version(workflow_id: str, version_id: str):
    """回滚到指定版本。"""
    result = restore_version(workflow_id, version_id)
    if not result:
        return jsonify({"error": "版本或工作流不存在"}), 404
    return jsonify({"success": True, "workflow": result})


@app.route("/api/workflows/<workflow_id>/versions/diff", methods=["POST"])
def api_diff_versions(workflow_id: str):
    """比较两个版本的差异。"""
    body = request.get_json(silent=True) or {}
    va = body.get("version_a", "")
    vb = body.get("version_b", "")
    if not va or not vb:
        return jsonify({"error": "version_a 和 version_b 为必填字段"}), 400
    result = diff_versions(workflow_id, va, vb)
    if not result:
        return jsonify({"error": "版本不存在"}), 404
    return jsonify(result)


# ------------------------------------------------------------------
# 子工作流 API
# ------------------------------------------------------------------

@app.route("/api/replay-with-sub-workflows", methods=["POST"])
def api_replay_with_sub_workflows():
    """支持子工作流调用的回放。"""
    if not browser_engine or not browser_engine.is_connected:
        return jsonify({"error": "浏览器未启动"}), 400
    body = request.get_json(silent=True) or {}
    workflow_id = body.get("workflow_id", "")
    if not workflow_id:
        return jsonify({"error": "workflow_id 为必填字段"}), 400

    # 先验证子工作流链
    check = validate_sub_workflow_chain(workflow_id)
    if not check.get("valid"):
        return jsonify({"error": check.get("error", "子工作流验证失败")}), 400

    config = get_config()
    ai_config = {
        "api_key": config.get("api_key", ""),
        "base_url": config.get("base_url", ""),
        "model": config.get("model", "gpt-4o"),
    }

    def _run():
        result = replay_with_sub_workflows(
            workflow_id=workflow_id,
            browser_engine=browser_engine,
            variables=body.get("variables"),
            on_step=lambda info: broadcast_event("replay_step", info),
            ai_config=ai_config if ai_config.get("api_key") else None,
        )
        broadcast_event("replay_complete", result)
        # 触发 Webhook
        event_name = "replay_success" if result.get("success") else "replay_failed"
        fire_webhooks_async(event_name, {
            "workflow_id": workflow_id,
            "success": result.get("success", False),
            "summary": result.get("summary", ""),
        })

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"started": True, "workflow_id": workflow_id})


@app.route("/api/workflows/<workflow_id>/sub-references", methods=["GET"])
def api_sub_references(workflow_id: str):
    """获取工作流引用的子工作流列表。"""
    refs = get_sub_workflow_references(workflow_id)
    return jsonify(refs)


@app.route("/api/workflows/<workflow_id>/validate-chain", methods=["GET"])
def api_validate_chain(workflow_id: str):
    """验证子工作流调用链。"""
    result = validate_sub_workflow_chain(workflow_id)
    return jsonify(result)


# ------------------------------------------------------------------
# 多标签页 API
# ------------------------------------------------------------------

@app.route("/api/tabs", methods=["GET"])
def api_list_tabs():
    """列出所有标签页。"""
    if not browser_engine or not browser_engine.is_connected:
        return jsonify({"error": "浏览器未启动"}), 400
    return jsonify(browser_engine.list_tabs())


@app.route("/api/tabs", methods=["POST"])
def api_new_tab():
    """打开新标签页。"""
    if not browser_engine or not browser_engine.is_connected:
        return jsonify({"error": "浏览器未启动"}), 400
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    result = browser_engine.new_tab(url)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result), 201


@app.route("/api/tabs/switch", methods=["POST"])
def api_switch_tab():
    """切换标签页。"""
    if not browser_engine or not browser_engine.is_connected:
        return jsonify({"error": "浏览器未启动"}), 400
    body = request.get_json(silent=True) or {}
    index = body.get("index")
    if index is None:
        return jsonify({"error": "index 为必填字段"}), 400
    result = browser_engine.switch_tab(int(index))
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/tabs/<int:index>", methods=["DELETE"])
def api_close_tab(index: int):
    """关闭标签页。"""
    if not browser_engine or not browser_engine.is_connected:
        return jsonify({"error": "浏览器未启动"}), 400
    result = browser_engine.close_tab(index)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


# ------------------------------------------------------------------
# SSE 实时事件流
# ------------------------------------------------------------------

@app.route("/api/events")
def api_events():
    """Server-Sent Events 端点。"""
    q = queue.Queue(maxsize=100)
    with event_queues_lock:
        event_queues.append(q)

    def stream():
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        except (GeneratorExit, ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            with event_queues_lock:
                try:
                    event_queues.remove(q)
                except ValueError:
                    pass

    return Response(stream(), mimetype="text/event-stream")


# ------------------------------------------------------------------
# 路由 - 桌面自动化
# ------------------------------------------------------------------

@app.route("/api/desktop/start", methods=["POST"])
def api_desktop_start():
    """启动桌面自动化引擎。"""
    global desktop_engine
    if desktop_engine and desktop_engine.is_running:
        return jsonify({"success": True, "message": "桌面引擎已在运行"})

    try:
        desktop_engine = DesktopEngine(screenshot_dir="screenshots")
        result = desktop_engine.start()
        broadcast_event("desktop_started", result)
        return jsonify(result)
    except Exception as exc:
        logger.exception("启动桌面引擎失败")
        return jsonify({"error": f"启动失败: {exc}"}), 500


@app.route("/api/desktop/stop", methods=["POST"])
def api_desktop_stop():
    """停止桌面自动化引擎。"""
    global desktop_engine
    if not desktop_engine or not desktop_engine.is_running:
        return jsonify({"success": True, "message": "桌面引擎未运行"})

    result = desktop_engine.stop()
    broadcast_event("desktop_stopped", {})
    return jsonify(result)


@app.route("/api/desktop/status", methods=["GET"])
def api_desktop_status():
    """获取桌面引擎状态。"""
    is_running = desktop_engine is not None and desktop_engine.is_running
    result = {"running": is_running}
    if is_running:
        result["screen"] = desktop_engine.get_screen_size()
        result["mouse"] = desktop_engine.get_mouse_position()
    return jsonify(result)


@app.route("/api/desktop/screenshot", methods=["GET"])
def api_desktop_screenshot():
    """截取桌面截图。"""
    if not desktop_engine or not desktop_engine.is_running:
        return jsonify({"error": "桌面引擎未运行"}), 400

    result = desktop_engine.desktop_screenshot()
    if result.get("error"):
        return jsonify(result), 500
    return jsonify({"success": True, "base64": result["base64"], "width": result["width"], "height": result["height"]})


@app.route("/api/desktop/action", methods=["POST"])
def api_desktop_action():
    """执行桌面操作（通用接口）。"""
    if not desktop_engine or not desktop_engine.is_running:
        return jsonify({"error": "桌面引擎未运行"}), 400

    data = request.get_json() or {}
    action = data.get("action", "")
    params = data.get("params", {})
    if not action:
        return jsonify({"error": "action 不能为空"}), 400

    result = desktop_engine.execute_action(action, params)
    broadcast_event("desktop_action", {"action": action, "result": result})
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/desktop/windows", methods=["GET"])
def api_desktop_windows():
    """列出所有窗口。"""
    if not desktop_engine or not desktop_engine.is_running:
        return jsonify({"error": "桌面引擎未运行"}), 400

    visible_only = request.args.get("visible_only", "true").lower() == "true"
    result = desktop_engine.list_windows(visible_only=visible_only)
    return jsonify(result)


@app.route("/api/desktop/focus", methods=["POST"])
def api_desktop_focus():
    """聚焦窗口。"""
    if not desktop_engine or not desktop_engine.is_running:
        return jsonify({"error": "桌面引擎未运行"}), 400

    data = request.get_json() or {}
    title = data.get("title", "")
    handle = data.get("handle", 0)
    result = desktop_engine.focus_window(title=title, handle=handle)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/desktop/controls", methods=["GET"])
def api_desktop_controls():
    """列出当前窗口的控件。"""
    if not desktop_engine or not desktop_engine.is_running:
        return jsonify({"error": "桌面引擎未运行"}), 400

    title = request.args.get("title", "")
    control_type = request.args.get("control_type", "")
    max_depth = int(request.args.get("max_depth", "4"))
    result = desktop_engine.list_controls(
        title=title, control_type=control_type, max_depth=max_depth,
    )
    return jsonify(result)


@app.route("/api/desktop/history", methods=["GET"])
def api_desktop_history():
    """获取桌面操作历史。"""
    if not desktop_engine:
        return jsonify({"success": True, "history": [], "count": 0})

    limit = int(request.args.get("limit", "50"))
    history = desktop_engine.action_history[-limit:]
    return jsonify({"success": True, "history": history, "count": len(history)})


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    logger.info("启动服务: http://%s:%d", host, port)
    app.run(host=host, port=port, debug=debug, threaded=True)
