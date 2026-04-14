# -*- coding: utf-8 -*-
"""浏览器操作引擎 - 封装 Playwright 提供结构化的浏览器操作能力。"""

import base64
import logging
import os
import queue
import subprocess
import threading
import time
from io import BytesIO
from typing import Any, Optional

from PIL import Image
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

logger = logging.getLogger(__name__)

# 浏览器连接模式
BROWSER_MODE_REUSE = "reuse"          # 复用用户已有浏览器（保留登录态）
BROWSER_MODE_STANDALONE = "standalone"  # 启动独立调试实例（空白 profile）
BROWSER_MODE_BUILTIN = "builtin"       # 使用 Playwright 内置 Chromium


class BrowserEngine:
    """浏览器自动化引擎，提供页面导航、元素交互、截图等能力。

    支持三种模式：
    1. reuse: 复用用户已打开的浏览器（需用户开启远程调试，保留 cookie/登录态）
    2. standalone: 启动独立 Chrome 实例（空白 profile，不影响用户浏览器）
    3. builtin: 使用 Playwright 内置 Chromium
    """

    # 常见浏览器安装路径
    _CHROME_PATHS = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    _EDGE_PATHS = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]

    def __init__(
        self,
        headless: bool = False,
        browser_type: str = "chromium",
        viewport_width: int = 1280,
        viewport_height: int = 720,
        browser_mode: str = BROWSER_MODE_REUSE,
        cdp_url: str = "http://127.0.0.1:9222",
        # 兼容旧参数
        use_local_browser: bool = True,
        connect_existing: bool = False,
    ) -> None:
        self._headless = headless
        self._browser_type = browser_type
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._cdp_url = cdp_url
        # 新模式优先；如果显式传了 browser_mode 就用它，否则从旧参数推断
        self._browser_mode = browser_mode
        # 兼容旧 .env: 如果旧配置 CONNECT_EXISTING=true，映射到 reuse 模式
        if connect_existing and browser_mode == BROWSER_MODE_REUSE:
            self._browser_mode = BROWSER_MODE_REUSE
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._action_history: list[dict[str, Any]] = []
        self._chrome_process: Optional[subprocess.Popen] = None
        # 线程安全：所有 Playwright 操作必须在创建它的线程中执行
        self._owner_thread: Optional[int] = None
        self._call_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _find_local_browser(self) -> Optional[str]:
        """查找本地已安装的 Chrome 或 Edge 路径。"""
        for path in self._CHROME_PATHS + self._EDGE_PATHS:
            if os.path.isfile(path):
                return path
        return None

    def _get_chrome_default_user_data_dir(self) -> str:
        """获取 Chrome 默认的用户数据目录（保留书签、cookie、登录状态等）。"""
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            chrome_dir = os.path.join(local_app_data, "Google", "Chrome", "User Data")
            if os.path.isdir(chrome_dir):
                return chrome_dir
        return ""

    def _get_edge_default_user_data_dir(self) -> str:
        """获取 Edge 默认的用户数据目录。"""
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            edge_dir = os.path.join(local_app_data, "Microsoft", "Edge", "User Data")
            if os.path.isdir(edge_dir):
                return edge_dir
        return ""

    def _kill_debug_chrome_only(self, port: str) -> None:
        """只关闭占用指定调试端口的 Chrome 进程，不影响用户的普通 Chrome。"""
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1].strip()
                    if pid.isdigit() and int(pid) > 0:
                        logger.info("发现占用端口 %s 的进程 PID=%s，正在关闭", port, pid)
                        subprocess.run(
                            ["taskkill", "/F", "/PID", pid],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                        )
                        time.sleep(1)
                        return
        except Exception as exc:
            logger.warning("清理旧调试端口进程时出错: %s", exc)

    def _check_cdp_available(self) -> bool:
        """检测 CDP 端口是否已开放。"""
        import urllib.request
        try:
            resp = urllib.request.urlopen(self._cdp_url + "/json/version", timeout=3)
            resp.read()
            return True
        except Exception:
            return False

    def _get_cdp_browser_info(self) -> dict:
        """从 CDP /json/version 获取浏览器信息。"""
        import json
        import urllib.request
        try:
            resp = urllib.request.urlopen(self._cdp_url + "/json/version", timeout=3)
            return json.loads(resp.read().decode())
        except Exception:
            return {}

    def _connect_reuse(self) -> str:
        """reuse 模式：连接用户已打开的、带远程调试端口的浏览器。

        用户需要自己以 --remote-debugging-port 启动 Chrome，
        或在 chrome://inspect 中开启远程调试（Chrome M144+）。
        保留 cookie、登录态、书签等所有用户数据。
        """
        if self._check_cdp_available():
            info = self._get_cdp_browser_info()
            browser_name = info.get("Browser", "未知浏览器")
            logger.info("reuse 模式：已连接到用户浏览器 — %s", browser_name)
            return self._cdp_url

        # 未检测到，给出明确指引
        port = self._cdp_url.split(":")[-1].rstrip("/")
        browser_path = self._find_local_browser()
        if browser_path:
            exe_name = os.path.basename(browser_path)
            hint = (
                f"未检测到开启远程调试的浏览器（端口 {port}）。\n"
                f"请用以下方式启动 Chrome：\n"
                f'  "{browser_path}" --remote-debugging-port={port}\n'
                f"或在 Chrome 地址栏访问 chrome://inspect/#remote-debugging 开启远程调试。\n"
                f"然后重新点击「启动浏览器」。"
            )
        else:
            hint = (
                f"未检测到开启远程调试的浏览器（端口 {port}）。\n"
                f"请先安装 Chrome/Edge 并以 --remote-debugging-port={port} 启动。"
            )
        raise RuntimeError(hint)

    def _connect_standalone(self) -> str:
        """standalone 模式：启动独立 Chrome 实例（空白 profile，不影响用户浏览器）。"""
        import urllib.request

        port = self._cdp_url.split(":")[-1].rstrip("/")

        # 如果已有调试端口开放，直接复用
        if self._check_cdp_available():
            logger.info("standalone 模式：检测到已有调试端口的浏览器")
            return self._cdp_url

        # 清理可能残留的旧调试进程
        self._kill_debug_chrome_only(port)

        browser_path = self._find_local_browser()
        if not browser_path:
            raise RuntimeError("未找到本地 Chrome/Edge，请手动安装浏览器后重试。")

        # 使用独立的 User Data 目录
        debug_profile_dir = os.path.join(
            os.environ.get("TEMP", os.path.expanduser("~")),
            "ai_browser_debug_profile",
        )
        os.makedirs(debug_profile_dir, exist_ok=True)
        logger.info("使用独立调试用户数据目录: %s", debug_profile_dir)

        cmd = [
            browser_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={debug_profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
        ]

        logger.info("启动独立 Chrome 实例: %s", " ".join(cmd))
        self._chrome_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 等待调试端口就绪
        for __ in range(30):
            time.sleep(0.5)
            try:
                resp = urllib.request.urlopen(self._cdp_url + "/json/version", timeout=2)
                resp.read()
                logger.info("独立 Chrome 实例调试端口已就绪")
                return self._cdp_url
            except Exception:
                continue
        raise RuntimeError(f"浏览器启动超时，调试端口 {self._cdp_url} 未就绪")

    def start(self) -> "BrowserEngine":
        """启动浏览器。

        三种模式:
        - reuse: 复用用户已有浏览器（保留 cookie/登录态，需开启远程调试）
        - standalone: 启动独立 Chrome 实例（空白 profile）
        - builtin: 使用 Playwright 内置 Chromium
        """
        self._playwright = sync_playwright().start()
        self._owner_thread = threading.current_thread().ident

        if self._browser_mode in (BROWSER_MODE_REUSE, BROWSER_MODE_STANDALONE):
            # CDP 连接模式
            if self._browser_mode == BROWSER_MODE_REUSE:
                cdp_url = self._connect_reuse()
            else:
                cdp_url = self._connect_standalone()

            self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)

            # 获取已有的 context 和 page，确保拿到一个可用的页面
            if self._browser.contexts:
                self._context = self._browser.contexts[0]
                live_page = None
                for p in self._context.pages:
                    try:
                        p.title()
                        live_page = p
                        break
                    except Exception:
                        continue
                if live_page:
                    self._page = live_page
                else:
                    self._page = self._context.new_page()
            else:
                self._context = self._browser.new_context(viewport=self._viewport)
                self._page = self._context.new_page()
            time.sleep(0.5)
            browser_label = f"CDP-{self._browser_mode} ({cdp_url})"
        else:
            # builtin 模式
            launch_kwargs: dict[str, Any] = {"headless": self._headless}
            launcher = getattr(self._playwright, self._browser_type)
            browser_label = f"builtin {self._browser_type}"
            self._browser = launcher.launch(**launch_kwargs)
            self._context = self._browser.new_context(viewport=self._viewport)
            self._page = self._context.new_page()

        self._record("start", {"browser": browser_label, "mode": self._browser_mode})
        return self

    def stop(self) -> None:
        """关闭浏览器并释放资源。"""
        is_cdp = self._browser_mode in (BROWSER_MODE_REUSE, BROWSER_MODE_STANDALONE)

        if is_cdp:
            # CDP 模式：断开连接（reuse 模式不关闭用户浏览器）
            if self._browser:
                try:
                    self._browser.close()
                except Exception:
                    pass
        else:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()

        if self._playwright:
            self._playwright.stop()

        # standalone 模式启动的 Chrome 进程需要关闭
        if self._chrome_process:
            try:
                self._chrome_process.terminate()
            except Exception:
                pass
            self._chrome_process = None

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._owner_thread = None

    def process_pending_calls(self, timeout: float = 0.05) -> None:
        """在拥有者线程中处理来自其他线程的挂起调用（线程安全桥）。"""
        try:
            func, args, kwargs, result_q = self._call_queue.get(timeout=timeout)
            try:
                result = func(*args, **kwargs)
                result_q.put(("ok", result))
            except Exception as exc:
                result_q.put(("error", exc))
        except queue.Empty:
            pass

    def _cross_thread_call(self, func, *args, **kwargs):
        """如果当前线程不是拥有者线程，通过队列派发调用。"""
        current = threading.current_thread().ident
        if self._owner_thread is None or current == self._owner_thread:
            return func(*args, **kwargs)
        # 派发到拥有者线程
        result_q: queue.Queue = queue.Queue()
        self._call_queue.put((func, args, kwargs, result_q))
        tag, value = result_q.get(timeout=30)
        if tag == "error":
            raise value
        return value

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("浏览器尚未启动，请先调用 start()")
        return self._page

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._action_history)

    # ------------------------------------------------------------------
    # 操作方法 —— 每个方法对应一类浏览器操作
    # ------------------------------------------------------------------

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> dict:
        """导航到指定 URL。"""
        def _do():
            self.page.goto(url, wait_until=wait_until)
            self.page.wait_for_load_state("networkidle")
            return {"url": self.page.url, "title": self.page.title()}
        result = self._cross_thread_call(_do)
        self._record("goto", {"url": url}, result)
        return result

    def click(self, selector: str) -> dict:
        """点击匹配选择器的第一个元素。"""
        def _do():
            self.page.click(selector, timeout=5000)
            time.sleep(0.5)
            return {"selector": selector, "url": self.page.url}
        result = self._cross_thread_call(_do)
        self._record("click", {"selector": selector}, result)
        return result

    def type_text(self, selector: str, text: str) -> dict:
        """向输入框填入文本。"""
        def _do():
            self.page.fill(selector, text, timeout=5000)
            return {"selector": selector, "text_length": len(text)}
        result = self._cross_thread_call(_do)
        self._record("type_text", {"selector": selector, "text": text}, result)
        return result

    def press_key(self, key: str) -> dict:
        """模拟键盘按键（如 Enter, Tab, Escape 等）。"""
        def _do():
            self.page.keyboard.press(key)
            time.sleep(0.3)
            return {"key": key}
        result = self._cross_thread_call(_do)
        self._record("press_key", {"key": key}, result)
        return result

    def scroll(self, direction: str = "down", amount: int = 500) -> dict:
        """页面滚动。direction: up / down / left / right。"""
        def _do():
            delta_map = {
                "down": (0, amount),
                "up": (0, -amount),
                "right": (amount, 0),
                "left": (-amount, 0),
            }
            dx, dy = delta_map.get(direction, (0, amount))
            self.page.mouse.wheel(dx, dy)
            time.sleep(0.3)
            return {"direction": direction, "amount": amount}
        result = self._cross_thread_call(_do)
        self._record("scroll", result, result)
        return result

    def get_text(self, selector: str) -> dict:
        """获取元素的文本内容。"""
        def _do():
            element = self.page.query_selector(selector)
            text = element.inner_text() if element else ""
            return {"selector": selector, "text": text}
        result = self._cross_thread_call(_do)
        self._record("get_text", {"selector": selector}, result)
        return result

    def get_page_info(self) -> dict:
        """获取当前页面基本信息。"""
        def _do():
            return {"url": self.page.url, "title": self.page.title()}
        result = self._cross_thread_call(_do)
        self._record("get_page_info", {}, result)
        return result

    def get_elements(self, selector: str = "a, button, input, textarea, select, [role='button']") -> dict:
        """获取页面上可交互元素的摘要列表。"""
        def _do():
            elements = self.page.query_selector_all(selector)
            items = []
            for i, el in enumerate(elements[:50]):
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                text = (el.inner_text() or "").strip()[:80]
                attrs = el.evaluate(
                    """e => {
                        const a = {};
                        ['id','class','name','href','placeholder','type','role','aria-label'].forEach(k => {
                            if (e.getAttribute(k)) a[k] = e.getAttribute(k);
                        });
                        return a;
                    }"""
                )
                items.append({"index": i, "tag": tag, "text": text, "attributes": attrs})
            return {"count": len(items), "elements": items}
        result = self._cross_thread_call(_do)
        self._record("get_elements", {"selector": selector}, {"count": result["count"]})
        return result

    def screenshot(self, full_page: bool = False, quality: int = 60) -> dict:
        """截取当前页面截图，返回 base64 编码。"""
        def _do():
            raw = self.page.screenshot(full_page=full_page, type="jpeg")
            img = Image.open(BytesIO(raw))
            max_dim = 1280
            if max(img.size) > max_dim:
                ratio = max_dim / max(img.size)
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            b64 = base64.b64encode(buf.getvalue()).decode()

            screenshot_dir = os.path.join(os.path.dirname(__file__), "screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            filename = "screenshot_{}.jpg".format(int(time.time()))
            filepath = os.path.join(screenshot_dir, filename)
            with open(filepath, "wb") as f:
                f.write(buf.getvalue())
            return {"base64": b64, "file": filepath}
        result = self._cross_thread_call(_do)
        log_result = {"base64_length": len(result["base64"]), "file": result["file"]}
        self._record("screenshot", {"full_page": full_page}, log_result)
        return result

    def execute_js(self, script: str) -> dict:
        """在页面中执行 JavaScript 代码。"""
        def _do():
            ret = self.page.evaluate(script)
            return {"return_value": ret}
        result = self._cross_thread_call(_do)
        self._record("execute_js", {"script": script[:200]}, result)
        return result

    def wait(self, seconds: float = 1.0) -> dict:
        """等待指定秒数。"""
        time.sleep(seconds)
        result = {"waited": seconds}
        self._record("wait", {"seconds": seconds}, result)
        return result

    def back(self) -> dict:
        """浏览器后退。"""
        def _do():
            self.page.go_back()
            time.sleep(0.5)
            return {"url": self.page.url}
        result = self._cross_thread_call(_do)
        self._record("back", {}, result)
        return result

    def forward(self) -> dict:
        """浏览器前进。"""
        def _do():
            self.page.go_forward()
            time.sleep(0.5)
            return {"url": self.page.url}
        result = self._cross_thread_call(_do)
        self._record("forward", {}, result)
        return result

    # ------------------------------------------------------------------
    # 工具函数定义 —— 供 AI Function Calling 使用
    # ------------------------------------------------------------------

    @staticmethod
    def get_tool_definitions() -> list[dict]:
        """返回可被 LLM function calling 使用的工具定义列表。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "goto",
                    "description": "导航到指定URL",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "目标URL"}
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "click",
                    "description": "点击页面元素。使用CSS选择器定位。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS选择器"}
                        },
                        "required": ["selector"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "type_text",
                    "description": "向输入框中输入文本",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "输入框的CSS选择器"},
                            "text": {"type": "string", "description": "要输入的文本"},
                        },
                        "required": ["selector", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "press_key",
                    "description": "模拟键盘按键，如 Enter, Tab, Escape, ArrowDown 等",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "按键名称"}
                        },
                        "required": ["key"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "scroll",
                    "description": "滚动页面",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "direction": {
                                "type": "string",
                                "enum": ["up", "down", "left", "right"],
                                "description": "滚动方向",
                            },
                            "amount": {"type": "integer", "description": "滚动像素数", "default": 500},
                        },
                        "required": ["direction"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_text",
                    "description": "获取页面元素的文本内容",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS选择器"}
                        },
                        "required": ["selector"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_page_info",
                    "description": "获取当前页面的URL和标题",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_elements",
                    "description": "获取页面上可交互元素列表（链接、按钮、输入框等）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {
                                "type": "string",
                                "description": "自定义CSS选择器，留空则获取所有可交互元素",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "screenshot",
                    "description": "截取当前页面截图",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "full_page": {
                                "type": "boolean",
                                "description": "是否截取整个页面",
                                "default": False,
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "execute_js",
                    "description": "在页面中执行JavaScript代码并返回结果",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "script": {"type": "string", "description": "JavaScript代码"}
                        },
                        "required": ["script"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "wait",
                    "description": "等待指定秒数",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "seconds": {"type": "number", "description": "等待秒数", "default": 1.0}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "back",
                    "description": "浏览器后退到上一页",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "forward",
                    "description": "浏览器前进到下一页",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "done",
                    "description": "任务完成，向用户报告结果。当你认为任务已经完成时调用此工具。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "description": "任务完成摘要"}
                        },
                        "required": ["summary"],
                    },
                },
            },
        ]

    def execute_action(self, action_name: str, params: dict) -> dict:
        """根据动作名称调度执行。"""
        action_map = {
            "goto": lambda p: self.goto(p["url"]),
            "click": lambda p: self.click(p["selector"]),
            "type_text": lambda p: self.type_text(p["selector"], p["text"]),
            "press_key": lambda p: self.press_key(p["key"]),
            "scroll": lambda p: self.scroll(p.get("direction", "down"), p.get("amount", 500)),
            "get_text": lambda p: self.get_text(p["selector"]),
            "get_page_info": lambda p: self.get_page_info(),
            "get_elements": lambda p: self.get_elements(p.get("selector", "a, button, input, textarea, select, [role='button']")),
            "screenshot": lambda p: self.screenshot(p.get("full_page", False)),
            "execute_js": lambda p: self.execute_js(p["script"]),
            "wait": lambda p: self.wait(p.get("seconds", 1.0)),
            "back": lambda p: self.back(),
            "forward": lambda p: self.forward(),
        }
        handler = action_map.get(action_name)
        if handler is None:
            return {"error": f"未知操作: {action_name}"}
        try:
            return handler(params)
        except Exception as exc:
            error_result = {"error": str(exc), "action": action_name}
            self._record(action_name, params, error_result)
            return error_result

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _record(self, action: str, params: dict, result: Optional[dict] = None) -> None:
        self._action_history.append({
            "action": action,
            "params": params,
            "result": result,
            "timestamp": time.time(),
        })
