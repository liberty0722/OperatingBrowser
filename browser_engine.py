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

    # 截图保留上限与操作历史上限
    _MAX_SCREENSHOTS = 50
    _MAX_ACTION_HISTORY = 500

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
        cross_thread_timeout: float = 30.0,
        # 兼容旧参数
        use_local_browser: bool = True,
        connect_existing: bool = False,
    ) -> None:
        self._headless = headless
        self._browser_type = browser_type
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._cdp_url = cdp_url
        self._cross_thread_timeout = cross_thread_timeout
        # 新模式优先；如果显式传了 browser_mode 就用它，否则从旧参数推断
        self._browser_mode = browser_mode
        # 兼容旧 .env: 如果旧配置 CONNECT_EXISTING=true，映射到 reuse 模式
        if connect_existing and browser_mode == BROWSER_MODE_REUSE:
            self._browser_mode = BROWSER_MODE_REUSE
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._pages: list = []  # 多标签页管理: [(page, label)]
        self._active_tab_index: int = 0
        self._action_history: list[dict[str, Any]] = []
        self._chrome_process: Optional[subprocess.Popen] = None
        # 线程安全：所有 Playwright 操作必须在创建它的线程中执行
        self._owner_thread: Optional[int] = None
        self._call_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        # 停止信号：由外部线程设置，owner 线程在 process_pending_calls 中检测
        self._stop_requested = threading.Event()

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

    @staticmethod
    def _prepare_debug_user_data_dir(original_dir: str) -> str:
        """为远程调试创建专用的 user-data-dir。

        Chrome 新版不允许在默认 user-data-dir 上开启 --remote-debugging-port，
        会报 "DevTools remote debugging requires a non-default data directory"。

        解决方案：创建一个 ``<原目录>-Debug`` 目录，把原始 profile 子目录
        （Default、Profile 1 等）用 NTFS junction 链接过去，这样 Chrome 使用
        非默认路径启动调试端口，但实际读写的仍是用户原始 profile 数据。

        Args:
            original_dir: Chrome 默认的 User Data 目录路径。

        Returns:
            可用于 --user-data-dir 的调试专用目录路径。
        """
        import shutil

        debug_dir = original_dir.rstrip("\\/") + "-Debug"
        os.makedirs(debug_dir, exist_ok=True)

        # 需要复制的顶层文件（小文件，不含 profile 数据）
        top_files = ["Local State", "First Run"]
        for fname in top_files:
            src = os.path.join(original_dir, fname)
            dst = os.path.join(debug_dir, fname)
            if os.path.isfile(src):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass

        # 把 profile 目录用 junction 链接（Default、Profile 1、Profile 2...）
        try:
            for entry in os.scandir(original_dir):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                name_lower = entry.name.lower()
                # 只链接 profile 目录
                is_profile = (
                    name_lower == "default"
                    or name_lower.startswith("profile ")
                    or name_lower == "guest profile"
                    or name_lower == "system profile"
                )
                if not is_profile:
                    continue
                link_path = os.path.join(debug_dir, entry.name)
                # 已经是 junction / 已存在则跳过
                if os.path.exists(link_path):
                    continue
                # 创建 NTFS junction（不需要管理员权限）
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", link_path, entry.path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                logger.info("创建 profile junction: %s -> %s", link_path, entry.path)
        except Exception as exc:
            logger.warning("创建 profile junction 时出错: %s", exc)

        return debug_dir

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

    def _kill_orphan_debug_chrome(self) -> None:
        """清理残留的调试专用 Chrome 孤儿进程。

        服务重启后会丢失对 Chrome 进程的引用，此时由本服务启动的
        调试 Chrome（user-data-dir 含 "-Debug" 或独立调试目录）会变成
        孤儿进程并持续占用调试端口，导致下次启动失败。

        判定规则（在 PowerShell 中一次性完成）：
        - 命令行带 remote-debugging-port 且 user-data-dir 是调试专用目录
        - 父进程不是 chrome（说明是主进程而非渲染子进程）
        - 父进程 PID 在当前系统中已不存在（说明启动它的服务已退出）
        满足以上条件即为孤儿根进程，树形终止（连带所有子进程）。
        不影响用户的普通 Chrome（不带 remote-debugging-port）。
        """
        ps_script = r"""
        $procs = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'"
        $allIds = @(Get-Process).Id
        $killed = 0
        foreach ($d in $procs) {
            $cmd = $d.CommandLine
            if (-not $cmd) { continue }
            if ($cmd -notlike '*remote-debugging-port*') { continue }
            if ($cmd -notlike '*User Data-Debug*' -and $cmd -notlike '*ai_browser_debug_profile*') { continue }
            $pp = [int]$d.ParentProcessId
            $parentIsChrome = $null -ne ($procs | Where-Object { $_.ProcessId -eq $pp })
            $parentExists = ($allIds -contains $pp)
            if (-not $parentIsChrome -and -not $parentExists) {
                taskkill /F /T /PID $d.ProcessId 2>$null | Out-Null
                $killed++
            }
        }
        Write-Output $killed
        """
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=30,
            )
            killed = result.stdout.strip()
            if killed and killed.isdigit() and int(killed) > 0:
                logger.info("stop(): 已清理 %s 个孤儿调试 Chrome 进程", killed)
        except Exception as exc:
            logger.warning("清理孤儿调试 Chrome 进程时出错: %s", exc)

    @staticmethod
    def _is_port_listening(port: int) -> bool:
        """检测指定端口是否有进程在监听（TCP socket 级检测）。"""
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            result = sock.connect_ex(("127.0.0.1", port))
            return result == 0
        except Exception:
            return False
        finally:
            sock.close()

    @staticmethod
    def _get_port_owner(port: int) -> str:
        """获取占用指定端口的进程信息。"""
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1].strip()
                    if pid.isdigit():
                        # 获取进程名
                        proc_result = subprocess.run(
                            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                            capture_output=True, text=True, timeout=5,
                        )
                        proc_name = proc_result.stdout.strip().split()[0] if proc_result.stdout.strip() else "unknown"
                        return f"{proc_name} (PID={pid})"
            return ""
        except Exception:
            return ""

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

    def _is_chrome_running(self) -> bool:
        """检测是否有 Chrome 进程正在运行（不含带调试端口的实例）。"""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return "chrome.exe" in result.stdout.lower()
        except Exception:
            return False

    def _graceful_close_chrome(self, timeout: int = 15) -> bool:
        """优雅关闭所有 Chrome 进程，等待其完全退出。

        先尝试 taskkill（无 /F），让 Chrome 正常保存数据后退出；
        超时后再强制 taskkill /F。

        Returns:
            True 表示 Chrome 已全部退出，False 表示仍有残留。
        """
        if not self._is_chrome_running():
            return True

        logger.info("reuse 模式：正在优雅关闭已有 Chrome 进程...")

        # 先尝试正常关闭（Chrome 会保存 session 数据）
        try:
            subprocess.run(
                ["taskkill", "/IM", "chrome.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass

        # 等待退出
        for __ in range(timeout):
            time.sleep(1)
            if not self._is_chrome_running():
                logger.info("reuse 模式：Chrome 已正常退出")
                return True

        # 超时，强制关闭
        logger.warning("reuse 模式：Chrome 未在 %ds 内退出，强制关闭", timeout)
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            pass
        time.sleep(2)
        return not self._is_chrome_running()

    def _connect_reuse(self) -> str:
        """reuse 模式：连接到用户真实 Chrome（保留全部登录态和 cookie）。

        策略：
        1. 先检测 CDP 端口是否已开放 → 直接连接（用户已手动开启调试端口）
        2. 检测是否有 Chrome 在运行 → 优雅关闭后用调试端口重启（同一 profile）
        3. 没有 Chrome 在运行 → 直接用调试端口启动（同一 profile）

        关键点：Chrome 的 profile 目录有锁机制，同一 user-data-dir 只能有一个实例。
        所以必须先关闭已有 Chrome，再用 --remote-debugging-port 重新启动。
        """
        import urllib.request

        port = self._cdp_url.split(":")[-1].rstrip("/")

        # 1. 已有 CDP 端口开放 → 直接连接
        if self._check_cdp_available():
            info = self._get_cdp_browser_info()
            browser_name = info.get("Browser", "未知浏览器")
            logger.info("reuse 模式：已连接到用户浏览器 — %s", browser_name)
            return self._cdp_url

        # 2. 查找本地浏览器
        browser_path = self._find_local_browser()
        if not browser_path:
            raise RuntimeError(
                "未找到本地 Chrome/Edge 浏览器。\n"
                "请安装 Chrome 或 Edge 后重试。"
            )

        # 3. 获取用户真实的 profile 目录
        original_user_data_dir = self._get_chrome_default_user_data_dir()
        if not original_user_data_dir and "edge" in browser_path.lower():
            original_user_data_dir = self._get_edge_default_user_data_dir()

        if not original_user_data_dir:
            raise RuntimeError(
                "未找到 Chrome/Edge 的用户数据目录。\n"
                "请确认已安装并至少运行过一次 Chrome 或 Edge。"
            )

        # 3.5 Chrome 新版不允许在默认 user-data-dir 上开启远程调试，
        #     创建专用调试目录（通过 junction 共享原始 profile）
        user_data_dir = self._prepare_debug_user_data_dir(original_user_data_dir)
        logger.info("reuse 模式：使用调试专用 profile 目录: %s", user_data_dir)

        # 4. 如果有 Chrome 正在运行 → 先关闭（否则 profile 被锁，无法以调试端口启动）
        if self._is_chrome_running():
            logger.info("reuse 模式：检测到 Chrome 正在运行，需要先关闭再以调试模式重启")
            if not self._graceful_close_chrome(timeout=15):
                raise RuntimeError(
                    "无法关闭已运行的 Chrome 进程。\n"
                    "请手动关闭所有 Chrome 窗口后重试。"
                )
            # 确保 profile 锁文件被释放
            time.sleep(2)

        # 5. 清理可能残留的旧调试端口占用
        self._kill_debug_chrome_only(port)

        # 5.5 确保端口已完全释放
        for wait_i in range(10):
            if not self._is_port_listening(int(port)):
                break
            logger.info("reuse 模式：端口 %s 仍被占用，等待释放...（%d/10）", port, wait_i + 1)
            time.sleep(1)
        else:
            owner = self._get_port_owner(int(port))
            raise RuntimeError(
                f"端口 {port} 被占用且无法释放（{owner}）。\n"
                f"请手动关闭占用端口的进程后重试。"
            )

        # 6. 用调试端口 + 用户真实 profile 启动 Chrome
        cmd = [
            browser_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--restore-last-session",  # 恢复上次的标签页
            # 禁用 SharedWorker —— Playwright connect_over_cdp 在 attach 到
            # shared_worker 类型的 target 时会断言失败导致 Node.js 进程崩溃
            "--disable-features=SharedWorker",
        ]

        logger.info("reuse 模式：启动 Chrome（调试端口 %s，profile: %s）", port, user_data_dir)
        logger.info("reuse 模式：启动命令: %s", " ".join(cmd))
        # 捕获 stderr 用于诊断 CDP 端口问题
        self._chrome_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # 7. 等待调试端口就绪（Chrome 启动+加载 profile 可能需要较长时间）
        max_attempts = 120  # 最多等 60 秒（120 * 0.5s）
        port_ever_listened = False
        for attempt in range(max_attempts):
            time.sleep(0.5)
            # 检查 Chrome 进程是否还活着
            if self._chrome_process and self._chrome_process.poll() is not None:
                stderr_output = ""
                try:
                    stderr_output = self._chrome_process.stderr.read().decode(
                        errors="replace"
                    )[:2000]
                except Exception:
                    pass
                raise RuntimeError(
                    f"Chrome 进程已退出（退出码 {self._chrome_process.returncode}）。\n"
                    f"可能是 profile 目录被锁定或损坏。请关闭所有 Chrome 后重试。\n"
                    f"Chrome stderr: {stderr_output}"
                )

            # 先用 socket 检测端口是否在监听
            port_listening = self._is_port_listening(int(port))
            if port_listening and not port_ever_listened:
                port_ever_listened = True
                logger.info("reuse 模式：检测到端口 %s 已在监听（TCP 层），等待 HTTP 就绪...", port)

            try:
                resp = urllib.request.urlopen(
                    self._cdp_url + "/json/version", timeout=2,
                )
                resp.read()
                info = self._get_cdp_browser_info()
                browser_name = info.get("Browser", "Chrome")
                logger.info("reuse 模式：Chrome 调试端口就绪 — %s（等待了 %.1fs）",
                            browser_name, attempt * 0.5)
                return self._cdp_url
            except Exception:
                if attempt % 10 == 9:
                    logger.info(
                        "reuse 模式：仍在等待 CDP 端口就绪...（已等 %.1fs，TCP端口%s）",
                        (attempt + 1) * 0.5,
                        "已监听" if port_ever_listened else "未监听",
                    )
                continue

        # 超时 — 尝试读取 Chrome stderr 诊断信息
        stderr_output = ""
        try:
            if self._chrome_process and self._chrome_process.stderr:
                # Windows 上 pipe 的 read() 会阻塞，用线程读取
                import io
                buf = io.BytesIO()

                def _drain():
                    try:
                        while True:
                            chunk = self._chrome_process.stderr.read(1024)
                            if not chunk:
                                break
                            buf.write(chunk)
                    except Exception:
                        pass

                t = threading.Thread(target=_drain, daemon=True)
                t.start()
                t.join(timeout=2)
                stderr_output = buf.getvalue().decode(errors="replace")[:2000]
        except Exception:
            pass

        # 检查端口是否被其他进程占用
        port_owner = self._get_port_owner(int(port))
        diag_info = (
            f"TCP端口{'已' if port_ever_listened else '未'}监听 | "
            f"端口占用进程: {port_owner or '无'}"
        )
        if stderr_output:
            diag_info += f" | Chrome stderr: {stderr_output[:500]}"

        logger.error("reuse 模式：Chrome 启动超时！诊断信息: %s", diag_info)
        raise RuntimeError(
            f"Chrome 启动超时，调试端口 {port} 未就绪。\n"
            f"诊断: {diag_info}\n"
            f"请手动关闭所有 Chrome 后重试，或切换到「独立实例」模式。"
        )

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

    def _cdp_connect_with_retry(self, cdp_url: str, max_retries: int = 3) -> None:
        """通过 CDP 连接浏览器，遇到 Playwright crash 时重试。

        Playwright ``connect_over_cdp`` 在连接到含有 shared_worker 等
        非标准 target 的浏览器时，可能因断言失败导致 Node.js 进程崩溃。
        此方法会在崩溃后重建 Playwright 实例并重试连接。
        """
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "connect_over_cdp 失败（第 %d/%d 次）: %s",
                    attempt, max_retries, exc,
                )
                # Playwright Node.js 进程可能已崩溃，需要重建
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                time.sleep(1)
                self._playwright = sync_playwright().start()
                self._owner_thread = threading.current_thread().ident

        raise RuntimeError(
            f"Playwright 连接 CDP ({cdp_url}) 多次失败。\n"
            f"最后一次错误: {last_exc}\n"
            f"可能是浏览器中有不兼容的 worker 类型。"
            f"请关闭包含 GitHub 等使用 SharedWorker 的标签页后重试。"
        )

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

            self._cdp_connect_with_retry(cdp_url)

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
        # 初始化多标签页列表
        self._pages = [(self._page, self._page.url if self._page else "")]
        self._active_tab_index = 0
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

        # reuse/standalone 模式启动的 Chrome 进程需要关闭
        if self._chrome_process:
            try:
                # 树形终止：连同所有渲染子进程一起结束，避免残留
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self._chrome_process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except Exception:
                pass
            self._chrome_process = None

        # 清理可能残留的调试专用 Chrome 孤儿进程（如服务重启后引用丢失），
        # 避免其一直占用调试端口导致下次启动失败
        if is_cdp:
            self._kill_orphan_debug_chrome()

        # 清除停止信号（stop 可能由兜底路径直接调用，复用对象时不残留状态）
        self._stop_requested.clear()

        self._page = None
        self._pages = []
        self._active_tab_index = 0
        self._context = None
        self._browser = None
        self._playwright = None
        self._owner_thread = None

    def process_pending_calls(self, timeout: float = 0.05) -> None:
        """在拥有者线程中处理来自其他线程的挂起调用（线程安全桥）。

        当检测到停止信号时，在 owner 线程中执行 stop() 并返回。
        """
        # 优先检测停止信号
        if self._stop_requested.is_set():
            self.stop()
            return

        try:
            func, args, kwargs, result_q = self._call_queue.get(timeout=timeout)
            try:
                result = func(*args, **kwargs)
                result_q.put(("ok", result))
            except Exception as exc:
                result_q.put(("error", exc))
        except queue.Empty:
            pass

    def request_stop(self) -> None:
        """从任意线程安全地请求停止浏览器。

        设置停止信号后，owner 线程在下一次 process_pending_calls 循环中
        会检测到并在正确的线程里执行 stop()。
        """
        self._stop_requested.set()

    def _cross_thread_call(self, func, *args, timeout=None):
        """如果当前线程不是拥有者线程，通过队列派发调用。可指定超时（秒）。"""
        current = threading.current_thread().ident
        if self._owner_thread is None or current == self._owner_thread:
            return func(*args)
        # 派发到拥有者线程
        if timeout is not None:
            call_timeout = timeout
        else:
            call_timeout = self._cross_thread_timeout
        result_q: queue.Queue = queue.Queue()
        self._call_queue.put((func, args, {}, result_q))
        try:
            tag, value = result_q.get(timeout=call_timeout)
        except queue.Empty:
            # 检查 Playwright 是否已 crash（Node.js 进程不在了）
            pw_alive = False
            try:
                if self._playwright and self._playwright._connection._transport._proc:
                    pw_alive = self._playwright._connection._transport._proc.poll() is None
            except Exception:
                pass
            if not pw_alive:
                raise RuntimeError(
                    "Playwright 进程已崩溃，无法执行浏览器操作。\n"
                    "请重新启动浏览器连接（点击'启动浏览器'）。"
                )
            raise RuntimeError(
                f"浏览器操作超时（{call_timeout}s），可能是页面加载过慢或连接断开。"
            )
        if tag == "error":
            raise value
        return value

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("浏览器尚未启动，请先调用 start()")
        # 检测页面是否已关闭/崩溃，失效则自动切换到可用的活页面。
        # 注意：本属性可能被非 owner 线程访问（如录制轮询线程），
        # 健康检查必须经 _cross_thread_call 派发到 owner 线程执行，
        # 否则 Playwright 同步 API 会因线程违例抛错，误判页面失效，
        # 进而触发兜底 new_page() 反复新建标签页。
        def _check(p):
            try:
                p.evaluate("1")
                return True
            except Exception:
                return False

        try:
            alive = self._cross_thread_call(_check, self._page, timeout=5)
        except Exception:
            alive = False
        if not alive:
            recovered = self._cross_thread_call(self._recover_page, timeout=15)
            if recovered is None:
                raise RuntimeError("当前页面已关闭，且无可用标签页，请重新启动浏览器")
        return self._page

    def _recover_page(self) -> Optional[Page]:
        """主页面失效后，从 context 中找一个活页面接管；找不到则新建空白页。

        该方法在拥有者线程（worker）中执行，可直接操作 Playwright 对象。
        返回恢复后的页面对象；完全无上下文时返回 None。
        """
        if self._context is None:
            return None
        # 优先选 _pages 中已知的活页面（保持用户先前的活跃页签）
        known = [p for p, __ in self._pages]
        candidates = known + [p for p in self._context.pages if p not in known]
        for p in candidates:
            try:
                p.evaluate("1")
                self._page = p
                # 同步索引
                for i, (pg, __) in enumerate(self._pages):
                    if pg is p:
                        self._active_tab_index = i
                        break
                else:
                    self._pages.append((p, p.url))
                    self._active_tab_index = len(self._pages) - 1
                return p
            except Exception:
                continue
        # 没有活页面 → 新开一个
        try:
            p = self._context.new_page()
            self._page = p
            self._pages.append((p, p.url))
            self._active_tab_index = len(self._pages) - 1
            return p
        except Exception:
            return None

    @property
    def is_connected(self) -> bool:
        """浏览器是否已连接且可用。"""
        return self._browser is not None

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._action_history)

    # ------------------------------------------------------------------
    # 多标签页管理
    # ------------------------------------------------------------------

    def _sync_pages_list(self) -> None:
        """同步内部 _pages 列表与 context.pages，保持一致性。"""
        if self._context is None:
            return
        ctx_pages = self._context.pages
        known = {id(p) for p, __ in self._pages}
        for p in ctx_pages:
            if id(p) not in known:
                self._pages.append((p, ""))

    def new_tab(self, url: str = "") -> dict:
        """在当前 context 中打开新标签页，可选导航到 url。"""
        def _do():
            if self._context is None:
                return {"error": "浏览器上下文未就绪"}
            new_page = self._context.new_page()
            label = url if url else "blank"
            self._pages.append((new_page, label))
            idx = len(self._pages) - 1
            self._active_tab_index = idx
            self._page = new_page
            if url:
                new_page.goto(url)
                new_page.wait_for_load_state("domcontentloaded")
            return {
                "tab_index": idx,
                "url": new_page.url,
                "title": new_page.title(),
                "total_tabs": len(self._pages),
            }
        return self._cross_thread_call(_do)

    def switch_tab(self, index: int, url: str = "", popup: bool = False) -> dict:
        """切换到指定索引的标签页；若提供 url 则优先按 URL 匹配（更稳定）。

        popup=True 表示目标页签是上一步点击弹出的：会等待它出现并加载到目标 URL，
        URL 始终匹配不上时（如带时间戳参数）兜底切到最新打开的页签。
        """
        def _alive(p) -> bool:
            try:
                return not p.is_closed()
            except Exception:
                return False

        def _find(norm: str):
            exact, prefix = None, None
            for i, (p, __) in enumerate(self._pages):
                if not _alive(p):
                    continue
                try:
                    pu = p.url.rstrip("/")
                except Exception:
                    continue
                if pu == norm:
                    exact = i
                elif prefix is None and pu.startswith(norm):
                    prefix = i
            return exact if exact is not None else prefix

        def _do():
            self._sync_pages_list()
            target = None
            if url:
                norm = url.rstrip("/")
                deadline = time.time() + (10 if popup else 3)
                target = _find(norm)
                while target is None and time.time() < deadline:
                    try:
                        self._pages[self._active_tab_index][0].wait_for_timeout(300)
                    except Exception:
                        time.sleep(0.3)
                    self._sync_pages_list()
                    target = _find(norm)
                if target is None and popup:
                    # 兜底：切到最新的、非当前的活页签
                    for i in range(len(self._pages) - 1, -1, -1):
                        p = self._pages[i][0]
                        if i != self._active_tab_index and _alive(p):
                            target = i
                            break
                if target is None:
                    return {"error": f"未找到 URL 匹配的标签页: {url}，当前共 {len(self._pages)} 个标签页"}
            if target is None:
                target = index
            if target < 0 or target >= len(self._pages):
                return {"error": f"无效标签页索引: {target}，当前共 {len(self._pages)} 个标签页"}
            page, __ = self._pages[target]
            try:
                page.title()
            except Exception:
                return {"error": f"标签页 {target} 已关闭或不可用"}
            self._page = page
            self._active_tab_index = target
            page.bring_to_front()
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            return {
                "tab_index": target,
                "url": page.url,
                "title": page.title(),
                "total_tabs": len(self._pages),
            }
        return self._cross_thread_call(_do)

    def close_tab(self, index: Optional[int] = None) -> dict:
        """关闭指定索引的标签页（默认关闭当前标签页）。"""
        def _do():
            self._sync_pages_list()
            target = index if index is not None else self._active_tab_index
            if target < 0 or target >= len(self._pages):
                return {"error": f"无效标签页索引: {target}"}
            if len(self._pages) <= 1:
                return {"error": "不能关闭最后一个标签页"}
            page, __ = self._pages.pop(target)
            try:
                page.close()
            except Exception:
                pass
            # 修正活跃索引
            if self._active_tab_index >= len(self._pages):
                self._active_tab_index = len(self._pages) - 1
            if target <= self._active_tab_index and target != self._active_tab_index:
                self._active_tab_index = max(0, self._active_tab_index - 1)
            self._page = self._pages[self._active_tab_index][0]
            return {
                "closed_index": target,
                "active_tab": self._active_tab_index,
                "url": self._page.url,
                "total_tabs": len(self._pages),
            }
        return self._cross_thread_call(_do)

    def list_tabs(self) -> dict:
        """列出所有打开的标签页信息。"""
        def _do():
            self._sync_pages_list()
            tabs = []
            for i, (p, label) in enumerate(self._pages):
                try:
                    tabs.append({
                        "index": i,
                        "url": p.url,
                        "title": p.title(),
                        "active": i == self._active_tab_index,
                    })
                except Exception:
                    tabs.append({
                        "index": i,
                        "url": "closed",
                        "title": label or "closed",
                        "active": i == self._active_tab_index,
                    })
            return {"tabs": tabs, "active_tab": self._active_tab_index}
        return self._cross_thread_call(_do)
    # 操作方法 —— 每个方法对应一类浏览器操作
    # ------------------------------------------------------------------

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> dict:
        """导航到指定 URL。

        若上一步点击已触发跳转到同一 URL，则不再重复加载：重复 goto 会刷新页面
        并多出一条历史记录，导致后续 back 退回到错误页面（丢失搜索状态）。
        """
        def _norm(u: str) -> str:
            return (u or "").rstrip("/")

        def _do():
            target = _norm(url)
            if _norm(self.page.url) != target:
                try:
                    self.page.wait_for_url(lambda u: _norm(u) == target, timeout=2000)
                except Exception:
                    pass
            if _norm(self.page.url) == target:
                try:
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                return {"url": self.page.url, "title": self.page.title(), "skipped": True}
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

            # 自动清理旧截图，保留最新 _MAX_SCREENSHOTS 张
            self._cleanup_screenshots(screenshot_dir)

            return {"base64": b64, "file": filepath}
        result = self._cross_thread_call(_do)
        log_result = {"base64_length": len(result["base64"]), "file": result["file"]}
        self._record("screenshot", {"full_page": full_page}, log_result)
        return result

    def _cleanup_screenshots(self, screenshot_dir: str) -> None:
        """清理旧截图，保留最新的 _MAX_SCREENSHOTS 张。"""
        try:
            files = [
                os.path.join(screenshot_dir, f)
                for f in os.listdir(screenshot_dir)
                if f.startswith("screenshot_") and f.endswith(".jpg")
            ]
            if len(files) <= self._MAX_SCREENSHOTS:
                return
            files.sort(key=os.path.getmtime)
            for f in files[:-self._MAX_SCREENSHOTS]:
                try:
                    os.remove(f)
                except OSError:
                    pass
        except Exception as exc:
            logger.debug("清理截图时出错: %s", exc)

    def execute_js(self, script: str) -> dict:
        """在页面中执行 JavaScript 代码。"""
        def _do():
            ret = self.page.evaluate(script)
            return {"return_value": ret}
        result = self._cross_thread_call(_do)
        self._record("execute_js", {"script": script[:200]}, result)
        return result

    def wait(self, seconds: float = 1.0) -> dict:
        """等待指定秒数（线程安全）。"""
        def _do():
            time.sleep(seconds)
            return {"waited": seconds}
        result = self._cross_thread_call(_do)
        self._record("wait", {"seconds": seconds}, result)
        return result

    def hover(self, selector: str) -> dict:
        """悬停在匹配选择器的元素上。"""
        def _do():
            self.page.hover(selector, timeout=5000)
            time.sleep(0.3)
            return {"selector": selector, "url": self.page.url}
        result = self._cross_thread_call(_do)
        self._record("hover", {"selector": selector}, result)
        return result

    def select_option(self, selector: str, value: str) -> dict:
        """在下拉列表中选择指定值。"""
        def _do():
            self.page.select_option(selector, value, timeout=5000)
            time.sleep(0.3)
            return {"selector": selector, "value": value}
        result = self._cross_thread_call(_do)
        self._record("select_option", {"selector": selector, "value": value}, result)
        return result

    def back(self) -> dict:
        """浏览器后退。"""
        def _do():
            self.page.go_back()
            try:
                self.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
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
                    "name": "hover",
                    "description": "悬停在页面元素上",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS选择器"},
                        },
                        "required": ["selector"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "select_option",
                    "description": "在下拉列表（select元素）中选择指定值",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "select元素的CSS选择器"},
                            "value": {"type": "string", "description": "要选择的选项值"},
                        },
                        "required": ["selector", "value"],
                    },
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
            "hover": lambda p: self.hover(p["selector"]),
            "select_option": lambda p: self.select_option(p["selector"], p["value"]),
            # 多标签页操作
            "new_tab": lambda p: self.new_tab(p.get("url", "")),
            "switch_tab": lambda p: self.switch_tab(p.get("index", 0), p.get("url", ""), bool(p.get("popup", False))),
            "close_tab": lambda p: self.close_tab(p.get("index")),
            "list_tabs": lambda p: self.list_tabs(),
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
        # 超出上限时丢弃最早的记录
        if len(self._action_history) > self._MAX_ACTION_HISTORY:
            self._action_history = self._action_history[-self._MAX_ACTION_HISTORY:]
