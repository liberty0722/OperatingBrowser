# -*- coding: utf-8 -*-
"""Flask API 服务 - 提供 Web 接口和前端页面。"""

import json
import logging
import os
import queue
import threading
import time
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS

from ai_agent import AIAgent
from browser_engine import (
    BROWSER_MODE_BUILTIN,
    BROWSER_MODE_REUSE,
    BROWSER_MODE_STANDALONE,
    BrowserEngine,
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
ai_agent: Optional[AIAgent] = None
task_thread: Optional[threading.Thread] = None
browser_thread: Optional[threading.Thread] = None
browser_cmd_queue: queue.Queue = queue.Queue()
event_queues: list[queue.Queue] = []
current_task_status = {"running": False, "task": "", "steps": []}


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
    """向所有 SSE 客户端广播事件。"""
    msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    dead = []
    for q in event_queues:
        try:
            q.put_nowait(msg)
        except queue.Full:
            dead.append(q)
    for q in dead:
        event_queues.remove(q)


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
        "browser_running": browser_engine is not None and browser_engine._browser is not None,
        "task_running": current_task_status["running"],
        "current_task": current_task_status["task"],
        "steps_count": len(current_task_status["steps"]),
    })


@app.route("/api/browser/start", methods=["POST"])
def api_browser_start():
    """启动浏览器（在专属线程中运行，解决 Playwright 线程安全问题）。"""
    global browser_engine, browser_thread
    if browser_engine and browser_engine._browser:
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
            while browser_engine is not None and browser_engine._browser is not None:
                browser_engine.process_pending_calls(timeout=0.05)
        except Exception as exc:
            error_holder[0] = str(exc)
            ready_event.set()

    browser_thread = threading.Thread(target=browser_worker, daemon=True)
    browser_thread.start()
    ready_event.wait(timeout=30)

    if error_holder[0]:
        broadcast_event("browser", {"status": "error", "message": error_holder[0]})
        return jsonify({"error": error_holder[0]}), 500

    broadcast_event("browser", {"status": "started", "mode": browser_mode})
    return jsonify({"success": True, "message": "浏览器已启动", "mode": browser_mode})


@app.route("/api/browser/stop", methods=["POST"])
def api_browser_stop():
    """停止浏览器。"""
    global browser_engine, browser_thread
    if browser_engine:
        # 通过跨线程调用执行 stop
        try:
            engine = browser_engine
            browser_engine = None  # 先置空，让 worker 线程退出循环
            time.sleep(0.2)
            engine.stop()
        except Exception:
            pass
        browser_thread = None
    broadcast_event("browser", {"status": "stopped"})
    return jsonify({"success": True, "message": "浏览器已关闭"})


@app.route("/api/browser/screenshot", methods=["GET"])
def api_browser_screenshot():
    """获取当前页面截图。"""
    if not browser_engine or not browser_engine._browser:
        return jsonify({"error": "浏览器未运行"}), 400
    result = browser_engine.screenshot()
    return jsonify({"success": True, "base64": result["base64"]})


@app.route("/api/task/run", methods=["POST"])
def api_task_run():
    """提交并执行 AI 任务。"""
    global ai_agent, task_thread

    if current_task_status["running"]:
        return jsonify({"error": "已有任务在执行中"}), 400

    data = request.get_json() or {}
    task_text = data.get("task", "").strip()
    if not task_text:
        return jsonify({"error": "任务内容不能为空"}), 400

    # 确保浏览器已启动
    if not browser_engine or not browser_engine._browser:
        return jsonify({"error": "请先启动浏览器"}), 400

    config = get_config()
    if not config["api_key"]:
        return jsonify({"error": "未配置 OPENAI_API_KEY"}), 400

    ai_agent = AIAgent(
        browser=browser_engine,
        api_key=config["api_key"],
        base_url=config["base_url"],
        model=config["model"],
    )

    current_task_status["running"] = True
    current_task_status["task"] = task_text
    current_task_status["steps"] = []

    def on_step(step_info: dict) -> None:
        current_task_status["steps"].append(step_info)
        broadcast_event("step", step_info)

    def run_in_thread() -> None:
        try:
            result = ai_agent.run_task(task_text, on_step=on_step)
            broadcast_event("task_complete", result)
        except Exception as exc:
            broadcast_event("task_error", {"error": str(exc)})
        finally:
            current_task_status["running"] = False

    task_thread = threading.Thread(target=run_in_thread, daemon=True)
    task_thread.start()

    return jsonify({"success": True, "message": "任务已开始执行"})


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
# SSE 实时事件流
# ------------------------------------------------------------------

@app.route("/api/events")
def api_events():
    """Server-Sent Events 端点。"""
    q = queue.Queue(maxsize=100)
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
        except GeneratorExit:
            if q in event_queues:
                event_queues.remove(q)

    return Response(stream(), mimetype="text/event-stream")


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("DEBUG", "true").lower() == "true"
    logger.info("启动服务: http://%s:%d", host, port)
    app.run(host=host, port=port, debug=debug, threaded=True)
