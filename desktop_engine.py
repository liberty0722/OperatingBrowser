# -*- coding: utf-8 -*-
"""Windows 桌面自动化引擎 - 使用 pyautogui + pywinauto 操作任意 Windows 应用。"""

import base64
import ctypes
import logging
import os
import subprocess
import time
from io import BytesIO
from typing import Any, Optional

import pyautogui
from PIL import Image

logger = logging.getLogger(__name__)

# pyautogui 安全设置
pyautogui.FAILSAFE = True  # 鼠标移到左上角时中止
pyautogui.PAUSE = 0.3  # 每个操作后短暂暂停

# 屏幕 DPI 感知（高分辨率屏幕适配）
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class DesktopEngine:
    """Windows 桌面自动化引擎。

    提供两层能力：
    1. 坐标级操作（pyautogui）：鼠标点击、键盘输入、截图、图像定位
    2. 控件级操作（pywinauto）：窗口管理、控件查找、UI 自动化
    """

    _MAX_ACTION_HISTORY = 500

    def __init__(self, screenshot_dir: str = "screenshots") -> None:
        self._screenshot_dir = screenshot_dir
        os.makedirs(screenshot_dir, exist_ok=True)
        self._action_history: list[dict[str, Any]] = []
        self._is_running = False
        # 延迟导入 pywinauto（可选依赖）
        self._pywinauto_app = None
        self._current_window = None
        try:
            from pywinauto import Desktop
            self._desktop = Desktop(backend="uia")
            logger.info("pywinauto UIA backend 已加载")
        except ImportError:
            self._desktop = None
            logger.warning("pywinauto 未安装，控件级操作不可用")

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def action_history(self) -> list[dict]:
        return list(self._action_history)

    def start(self) -> dict:
        """启动桌面引擎。"""
        self._is_running = True
        screen_w, screen_h = pyautogui.size()
        logger.info("桌面引擎已启动，屏幕分辨率: %dx%d", screen_w, screen_h)
        return {
            "success": True,
            "screen_width": screen_w,
            "screen_height": screen_h,
        }

    def stop(self) -> dict:
        """停止桌面引擎。"""
        self._is_running = False
        self._current_window = None
        self._pywinauto_app = None
        logger.info("桌面引擎已停止")
        return {"success": True}

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def desktop_screenshot(self, region: Optional[dict] = None) -> dict:
        """截取桌面截图。

        Args:
            region: 可选区域 {"x": int, "y": int, "width": int, "height": int}
        """
        try:
            if region:
                img = pyautogui.screenshot(
                    region=(region["x"], region["y"], region["width"], region["height"])
                )
            else:
                img = pyautogui.screenshot()

            # 压缩为 JPEG
            img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=70)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            # 保存到文件
            ts = int(time.time() * 1000)
            path = os.path.join(self._screenshot_dir, f"desktop_{ts}.jpg")
            img.save(path, quality=85)

            result = {
                "success": True,
                "base64": b64,
                "width": img.width,
                "height": img.height,
                "path": path,
            }
            self._record("desktop_screenshot", {"region": region}, result)
            return result
        except Exception as exc:
            return {"error": f"截图失败: {exc}"}

    # ------------------------------------------------------------------
    # 鼠标操作
    # ------------------------------------------------------------------

    def mouse_click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
    ) -> dict:
        """在指定坐标点击鼠标。"""
        try:
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
            result = {"success": True, "x": x, "y": y, "button": button, "clicks": clicks}
            self._record("mouse_click", {"x": x, "y": y, "button": button, "clicks": clicks}, result)
            return result
        except Exception as exc:
            return {"error": f"鼠标点击失败: {exc}"}

    def mouse_double_click(self, x: int, y: int) -> dict:
        """双击指定坐标。"""
        try:
            pyautogui.doubleClick(x=x, y=y)
            result = {"success": True, "x": x, "y": y}
            self._record("mouse_double_click", {"x": x, "y": y}, result)
            return result
        except Exception as exc:
            return {"error": f"双击失败: {exc}"}

    def mouse_right_click(self, x: int, y: int) -> dict:
        """右键点击指定坐标。"""
        try:
            pyautogui.rightClick(x=x, y=y)
            result = {"success": True, "x": x, "y": y}
            self._record("mouse_right_click", {"x": x, "y": y}, result)
            return result
        except Exception as exc:
            return {"error": f"右键点击失败: {exc}"}

    def mouse_move(self, x: int, y: int, duration: float = 0.3) -> dict:
        """移动鼠标到指定坐标。"""
        try:
            pyautogui.moveTo(x=x, y=y, duration=duration)
            result = {"success": True, "x": x, "y": y}
            self._record("mouse_move", {"x": x, "y": y, "duration": duration}, result)
            return result
        except Exception as exc:
            return {"error": f"鼠标移动失败: {exc}"}

    def mouse_drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration: float = 0.5,
    ) -> dict:
        """从 start 拖拽到 end。"""
        try:
            pyautogui.moveTo(start_x, start_y)
            pyautogui.drag(
                end_x - start_x,
                end_y - start_y,
                duration=duration,
            )
            result = {
                "success": True,
                "start": {"x": start_x, "y": start_y},
                "end": {"x": end_x, "y": end_y},
            }
            self._record("mouse_drag", {
                "start_x": start_x, "start_y": start_y,
                "end_x": end_x, "end_y": end_y,
            }, result)
            return result
        except Exception as exc:
            return {"error": f"拖拽失败: {exc}"}

    def mouse_scroll(self, amount: int, x: Optional[int] = None, y: Optional[int] = None) -> dict:
        """滚动鼠标滚轮。amount > 0 向上，< 0 向下。"""
        try:
            if x is not None and y is not None:
                pyautogui.scroll(amount, x=x, y=y)
            else:
                pyautogui.scroll(amount)
            result = {"success": True, "amount": amount}
            self._record("mouse_scroll", {"amount": amount, "x": x, "y": y}, result)
            return result
        except Exception as exc:
            return {"error": f"滚动失败: {exc}"}

    # ------------------------------------------------------------------
    # 键盘操作
    # ------------------------------------------------------------------

    def keyboard_type(self, text: str, interval: float = 0.02) -> dict:
        """输入文本（支持中文，使用剪贴板方式）。"""
        try:
            if _contains_non_ascii(text):
                # 中文等非 ASCII 字符通过剪贴板粘贴
                _clipboard_paste(text)
            else:
                pyautogui.typewrite(text, interval=interval)
            result = {"success": True, "text": text}
            self._record("keyboard_type", {"text": text}, result)
            return result
        except Exception as exc:
            return {"error": f"输入文本失败: {exc}"}

    def keyboard_press(self, key: str) -> dict:
        """按下并释放一个按键（如 enter, tab, escape, f5 等）。"""
        try:
            pyautogui.press(key)
            result = {"success": True, "key": key}
            self._record("keyboard_press", {"key": key}, result)
            return result
        except Exception as exc:
            return {"error": f"按键失败: {exc}"}

    def keyboard_hotkey(self, *keys: str) -> dict:
        """按下组合键（如 ctrl+c, alt+tab, ctrl+shift+s）。

        Args:
            keys: 按键序列，如 ("ctrl", "c")
        """
        try:
            pyautogui.hotkey(*keys)
            result = {"success": True, "keys": list(keys)}
            self._record("keyboard_hotkey", {"keys": list(keys)}, result)
            return result
        except Exception as exc:
            return {"error": f"组合键失败: {exc}"}

    # ------------------------------------------------------------------
    # 图像定位（在屏幕上查找图片位置）
    # ------------------------------------------------------------------

    def locate_on_screen(
        self,
        image_path: str,
        confidence: float = 0.8,
        region: Optional[dict] = None,
    ) -> dict:
        """在屏幕上查找图片位置。

        Args:
            image_path: 目标图片文件路径
            confidence: 匹配置信度 (0~1)
            region: 搜索区域 {"x","y","width","height"}
        """
        try:
            search_region = None
            if region:
                search_region = (region["x"], region["y"], region["width"], region["height"])

            location = pyautogui.locateOnScreen(
                image_path,
                confidence=confidence,
                region=search_region,
            )
            if location is None:
                return {"found": False, "message": "未在屏幕上找到目标图片"}

            center = pyautogui.center(location)
            result = {
                "found": True,
                "x": center.x,
                "y": center.y,
                "left": location.left,
                "top": location.top,
                "width": location.width,
                "height": location.height,
            }
            self._record("locate_on_screen", {"image_path": image_path}, result)
            return result
        except Exception as exc:
            return {"error": f"图像定位失败: {exc}"}

    def click_image(self, image_path: str, confidence: float = 0.8) -> dict:
        """在屏幕上查找图片并点击其中心。"""
        loc = self.locate_on_screen(image_path, confidence=confidence)
        if loc.get("error"):
            return loc
        if not loc.get("found"):
            return {"error": "未找到目标图片，无法点击"}
        return self.mouse_click(loc["x"], loc["y"])

    # ------------------------------------------------------------------
    # 应用程序管理
    # ------------------------------------------------------------------

    def launch_app(self, path: str, args: Optional[list[str]] = None, wait: float = 2.0) -> dict:
        """启动应用程序。

        Args:
            path: 可执行文件路径或程序名（如 notepad, calc）
            args: 命令行参数
            wait: 启动后等待秒数
        """
        try:
            cmd = [path] + (args or [])
            proc = subprocess.Popen(
                cmd,
                shell=False,
                creationflags=subprocess.DETACHED_PROCESS,
            )
            time.sleep(wait)
            result = {"success": True, "pid": proc.pid, "path": path}
            self._record("launch_app", {"path": path, "args": args}, result)
            return result
        except Exception as exc:
            return {"error": f"启动应用失败: {exc}"}

    def kill_app(self, pid: int = 0, name: str = "") -> dict:
        """关闭应用程序（通过 PID 或进程名）。

        Args:
            pid: 进程 ID（优先使用）
            name: 进程名称（如 notepad.exe），支持不带 .exe 后缀
        """
        try:
            if pid:
                os.kill(pid, 9)
                result = {"success": True, "pid": pid}
                self._record("kill_app", {"pid": pid}, result)
                return result
            if not name:
                return {"error": "必须提供 pid 或 name"}

            # 补全 .exe 后缀
            if not name.lower().endswith(".exe"):
                name = name + ".exe"

            ret = subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if ret.returncode == 0:
                result = {"success": True, "name": name, "output": ret.stdout.strip()}
            else:
                result = {"success": False, "name": name, "error": ret.stderr.strip() or ret.stdout.strip()}
            self._record("kill_app", {"name": name}, result)
            return result
        except Exception as exc:
            return {"error": f"关闭应用失败: {exc}"}

    def window_screenshot(self, title: str = "", handle: int = 0) -> dict:
        """截取指定窗口的区域截图（不截全屏，节省 LLM token）。

        Args:
            title: 窗口标题（模糊匹配）
            handle: 窗口句柄
        """
        try:
            hwnd = self._resolve_hwnd(title, handle)
            if not hwnd:
                return {"error": "未找到目标窗口"}

            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))

            region = {
                "x": max(rect.left, 0),
                "y": max(rect.top, 0),
                "width": rect.right - rect.left,
                "height": rect.bottom - rect.top,
            }
            result = self.desktop_screenshot(region=region)
            if result.get("success"):
                result["window_title"] = title or str(handle)
            self._record("window_screenshot", {"title": title, "handle": handle}, {
                "success": result.get("success", False),
                "width": result.get("width"),
                "height": result.get("height"),
            })
            return result
        except Exception as exc:
            return {"error": f"窗口截图失败: {exc}"}

    # ------------------------------------------------------------------
    # 窗口管理（pywinauto）
    # ------------------------------------------------------------------

    def list_windows(self, visible_only: bool = True) -> dict:
        """列出当前桌面所有窗口。"""
        try:
            if self._desktop is None:
                return self._list_windows_fallback(visible_only)

            windows = []
            for win in self._desktop.windows():
                try:
                    if visible_only and not win.is_visible():
                        continue
                    title = win.window_text()
                    if not title.strip():
                        continue
                    rect = win.rectangle()
                    windows.append({
                        "title": title,
                        "class_name": win.class_name(),
                        "handle": win.handle,
                        "rect": {
                            "left": rect.left,
                            "top": rect.top,
                            "right": rect.right,
                            "bottom": rect.bottom,
                        },
                        "is_enabled": win.is_enabled(),
                    })
                except Exception:
                    continue

            result = {"success": True, "windows": windows, "count": len(windows)}
            self._record("list_windows", {"visible_only": visible_only}, {"count": len(windows)})
            return result
        except Exception as exc:
            return {"error": f"列出窗口失败: {exc}"}

    def _list_windows_fallback(self, visible_only: bool = True) -> dict:
        """不使用 pywinauto 的窗口列表回退方案。"""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        windows = []

        def enum_handler(hwnd, _):
            if visible_only and not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if title.strip():
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                windows.append({
                    "title": title,
                    "handle": hwnd,
                    "rect": {
                        "left": rect.left,
                        "top": rect.top,
                        "right": rect.right,
                        "bottom": rect.bottom,
                    },
                })
            return True

        enum_func_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM,
        )
        user32.EnumWindows(enum_func_type(enum_handler), 0)

        return {"success": True, "windows": windows, "count": len(windows)}

    def focus_window(self, title: str = "", handle: int = 0) -> dict:
        """激活/聚焦指定窗口。

        Args:
            title: 窗口标题（模糊匹配）
            handle: 窗口句柄（精确匹配，优先于 title）
        """
        try:
            if handle:
                import ctypes
                user32 = ctypes.windll.user32
                user32.SetForegroundWindow(handle)
                result = {"success": True, "handle": handle}
                self._record("focus_window", {"handle": handle}, result)
                return result

            if not title:
                return {"error": "必须提供 title 或 handle"}

            if self._desktop is not None:
                from pywinauto import findwindows
                try:
                    wins = findwindows.find_windows(title_re=f".*{title}.*", visible_only=True)
                    if not wins:
                        return {"error": f"未找到标题包含 '{title}' 的窗口"}
                    hwnd = wins[0]
                    from pywinauto import Application
                    app = Application(backend="uia").connect(handle=hwnd)
                    win = app.window(handle=hwnd)
                    win.set_focus()
                    self._current_window = win
                    self._pywinauto_app = app
                    actual_title = win.window_text()
                    result = {"success": True, "title": actual_title, "handle": hwnd}
                    self._record("focus_window", {"title": title}, result)
                    return result
                except Exception as exc:
                    return {"error": f"聚焦窗口失败: {exc}"}
            else:
                # 回退：使用 Win32 API
                return self._focus_window_fallback(title)
        except Exception as exc:
            return {"error": f"聚焦窗口失败: {exc}"}

    def _focus_window_fallback(self, title: str) -> dict:
        """Win32 API 回退的窗口聚焦。"""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found_hwnd = None

        def enum_handler(hwnd, _):
            nonlocal found_hwnd
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title.lower() in buf.value.lower():
                found_hwnd = hwnd
                return False  # 找到即停止
            return True

        enum_func_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM,
        )
        user32.EnumWindows(enum_func_type(enum_handler), 0)

        if not found_hwnd:
            return {"error": f"未找到标题包含 '{title}' 的窗口"}

        user32.SetForegroundWindow(found_hwnd)
        result = {"success": True, "handle": found_hwnd, "title": title}
        self._record("focus_window", {"title": title}, result)
        return result

    def minimize_window(self, title: str = "", handle: int = 0) -> dict:
        """最小化窗口。"""
        return self._window_action("minimize", title, handle)

    def maximize_window(self, title: str = "", handle: int = 0) -> dict:
        """最大化窗口。"""
        return self._window_action("maximize", title, handle)

    def close_window(self, title: str = "", handle: int = 0) -> dict:
        """关闭窗口。"""
        return self._window_action("close", title, handle)

    def resize_window(
        self,
        width: int,
        height: int,
        title: str = "",
        handle: int = 0,
    ) -> dict:
        """调整窗口大小。"""
        try:
            hwnd = self._resolve_hwnd(title, handle)
            if not hwnd:
                return {"error": "未找到目标窗口"}

            import ctypes
            user32 = ctypes.windll.user32
            from ctypes import wintypes
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            user32.MoveWindow(hwnd, rect.left, rect.top, width, height, True)
            result = {"success": True, "width": width, "height": height}
            self._record("resize_window", {"width": width, "height": height, "title": title}, result)
            return result
        except Exception as exc:
            return {"error": f"调整窗口大小失败: {exc}"}

    def move_window(
        self,
        x: int,
        y: int,
        title: str = "",
        handle: int = 0,
    ) -> dict:
        """移动窗口位置。"""
        try:
            hwnd = self._resolve_hwnd(title, handle)
            if not hwnd:
                return {"error": "未找到目标窗口"}

            import ctypes
            user32 = ctypes.windll.user32
            from ctypes import wintypes
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            user32.MoveWindow(hwnd, x, y, w, h, True)
            result = {"success": True, "x": x, "y": y}
            self._record("move_window", {"x": x, "y": y, "title": title}, result)
            return result
        except Exception as exc:
            return {"error": f"移动窗口失败: {exc}"}

    # ------------------------------------------------------------------
    # 控件操作（pywinauto UIA）
    # ------------------------------------------------------------------

    def find_control(
        self,
        title: str = "",
        control_type: str = "",
        auto_id: str = "",
        name: str = "",
        depth: int = 10,
    ) -> dict:
        """在当前聚焦窗口中查找控件。

        Args:
            title: 窗口标题（如果需要先切换窗口）
            control_type: 控件类型（Button, Edit, Text, ListItem 等）
            auto_id: AutomationId
            name: 控件名称（Name 属性）
            depth: 搜索深度
        """
        if self._desktop is None:
            return {"error": "pywinauto 未安装，控件级操作不可用"}

        try:
            if title:
                focus_result = self.focus_window(title=title)
                if focus_result.get("error"):
                    return focus_result

            if self._current_window is None:
                return {"error": "未聚焦任何窗口，请先调用 focus_window"}

            criteria = {}
            if control_type:
                criteria["control_type"] = control_type
            if auto_id:
                criteria["auto_id"] = auto_id
            if name:
                criteria["title"] = name
            if not criteria:
                return {"error": "至少需要指定 control_type / auto_id / name 之一"}

            criteria["depth"] = depth
            try:
                ctrl = self._current_window.child_window(**criteria)
                ctrl_info = {
                    "found": True,
                    "name": ctrl.window_text(),
                    "control_type": ctrl.friendly_class_name(),
                    "is_enabled": ctrl.is_enabled(),
                    "is_visible": ctrl.is_visible(),
                }
                rect = ctrl.rectangle()
                ctrl_info["rect"] = {
                    "left": rect.left,
                    "top": rect.top,
                    "right": rect.right,
                    "bottom": rect.bottom,
                }
                self._record("find_control", criteria, ctrl_info)
                return ctrl_info
            except Exception as exc:
                return {"found": False, "error": f"未找到匹配的控件: {exc}"}
        except Exception as exc:
            return {"error": f"查找控件失败: {exc}"}

    def click_control(
        self,
        control_type: str = "",
        auto_id: str = "",
        name: str = "",
        title: str = "",
    ) -> dict:
        """点击指定控件。"""
        if self._desktop is None:
            return {"error": "pywinauto 未安装，控件级操作不可用"}

        try:
            if title:
                focus_result = self.focus_window(title=title)
                if focus_result.get("error"):
                    return focus_result

            if self._current_window is None:
                return {"error": "未聚焦任何窗口"}

            criteria = {}
            if control_type:
                criteria["control_type"] = control_type
            if auto_id:
                criteria["auto_id"] = auto_id
            if name:
                criteria["title"] = name

            ctrl = self._current_window.child_window(**criteria)
            ctrl.click_input()
            result = {"success": True, "control": ctrl.window_text()}
            self._record("click_control", {**criteria, "title": title}, result)
            return result
        except Exception as exc:
            return {"error": f"点击控件失败: {exc}"}

    def type_in_control(
        self,
        text: str,
        control_type: str = "",
        auto_id: str = "",
        name: str = "",
        title: str = "",
        clear_first: bool = True,
    ) -> dict:
        """在指定控件中输入文本。"""
        if self._desktop is None:
            return {"error": "pywinauto 未安装，控件级操作不可用"}

        try:
            if title:
                focus_result = self.focus_window(title=title)
                if focus_result.get("error"):
                    return focus_result

            if self._current_window is None:
                return {"error": "未聚焦任何窗口"}

            criteria = {}
            if control_type:
                criteria["control_type"] = control_type
            if auto_id:
                criteria["auto_id"] = auto_id
            if name:
                criteria["title"] = name

            ctrl = self._current_window.child_window(**criteria)
            if clear_first:
                ctrl.set_edit_text("")
            ctrl.type_keys(text, with_spaces=True)
            result = {"success": True, "text": text}
            self._record("type_in_control", {**criteria, "text": text}, result)
            return result
        except Exception as exc:
            return {"error": f"控件输入失败: {exc}"}

    def get_control_text(
        self,
        control_type: str = "",
        auto_id: str = "",
        name: str = "",
        title: str = "",
    ) -> dict:
        """获取指定控件的文本内容。"""
        if self._desktop is None:
            return {"error": "pywinauto 未安装，控件级操作不可用"}

        try:
            if title:
                focus_result = self.focus_window(title=title)
                if focus_result.get("error"):
                    return focus_result

            if self._current_window is None:
                return {"error": "未聚焦任何窗口"}

            criteria = {}
            if control_type:
                criteria["control_type"] = control_type
            if auto_id:
                criteria["auto_id"] = auto_id
            if name:
                criteria["title"] = name

            ctrl = self._current_window.child_window(**criteria)
            text = ctrl.window_text()
            result = {"success": True, "text": text}
            self._record("get_control_text", criteria, result)
            return result
        except Exception as exc:
            return {"error": f"获取控件文本失败: {exc}"}

    def list_controls(self, title: str = "", control_type: str = "", max_depth: int = 4) -> dict:
        """列出当前窗口中的控件树（简化版）。"""
        if self._desktop is None:
            return {"error": "pywinauto 未安装，控件级操作不可用"}

        try:
            if title:
                focus_result = self.focus_window(title=title)
                if focus_result.get("error"):
                    return focus_result

            if self._current_window is None:
                return {"error": "未聚焦任何窗口"}

            controls = []
            self._collect_controls(self._current_window, controls, depth=0, max_depth=max_depth, filter_type=control_type)
            result = {"success": True, "controls": controls, "count": len(controls)}
            self._record("list_controls", {"title": title, "control_type": control_type}, {"count": len(controls)})
            return result
        except Exception as exc:
            return {"error": f"列出控件失败: {exc}"}

    def _collect_controls(self, parent, result_list, depth, max_depth, filter_type=""):
        """递归收集控件信息。"""
        if depth > max_depth:
            return
        try:
            children = parent.children()
        except Exception:
            return

        for child in children:
            try:
                ctype = child.friendly_class_name()
                if filter_type and ctype.lower() != filter_type.lower():
                    # 仍然递归搜索子控件
                    self._collect_controls(child, result_list, depth + 1, max_depth, filter_type)
                    continue

                cname = child.window_text()
                auto_id = ""
                try:
                    auto_id = child.automation_id()
                except Exception:
                    pass

                is_enabled = False
                try:
                    is_enabled = child.is_enabled()
                except Exception:
                    pass

                entry = {
                    "name": cname,
                    "type": ctype,
                    "auto_id": auto_id,
                    "enabled": is_enabled,
                    "depth": depth,
                }
                result_list.append(entry)
            except Exception:
                pass

            self._collect_controls(child, result_list, depth + 1, max_depth, filter_type)

    # ------------------------------------------------------------------
    # 屏幕/系统信息
    # ------------------------------------------------------------------

    def get_screen_size(self) -> dict:
        """获取屏幕分辨率。"""
        w, h = pyautogui.size()
        return {"width": w, "height": h}

    def get_mouse_position(self) -> dict:
        """获取当前鼠标位置。"""
        x, y = pyautogui.position()
        return {"x": x, "y": y}

    def get_pixel_color(self, x: int, y: int) -> dict:
        """获取指定坐标的像素颜色。"""
        try:
            img = pyautogui.screenshot(region=(x, y, 1, 1))
            r, g, b = img.getpixel((0, 0))
            return {"r": r, "g": g, "b": b, "hex": f"#{r:02x}{g:02x}{b:02x}"}
        except Exception as exc:
            return {"error": f"获取像素颜色失败: {exc}"}

    def wait(self, seconds: float = 1.0) -> dict:
        """等待指定秒数。"""
        time.sleep(seconds)
        return {"success": True, "seconds": seconds}

    # ------------------------------------------------------------------
    # LLM 工具定义
    # ------------------------------------------------------------------

    @staticmethod
    def get_tool_definitions() -> list[dict]:
        """返回桌面自动化操作的 LLM function calling 工具定义。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "desktop_screenshot",
                    "description": "截取桌面截图。可指定区域，不指定则截取全屏。用于AI观察当前桌面状态。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "region": {
                                "type": "object",
                                "description": "截图区域（可选）",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "width": {"type": "integer"},
                                    "height": {"type": "integer"},
                                },
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mouse_click",
                    "description": "在屏幕指定坐标 (x, y) 点击鼠标。可指定按键和点击次数。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "description": "X 坐标"},
                            "y": {"type": "integer", "description": "Y 坐标"},
                            "button": {
                                "type": "string",
                                "enum": ["left", "right", "middle"],
                                "description": "鼠标按键",
                                "default": "left",
                            },
                            "clicks": {"type": "integer", "description": "点击次数", "default": 1},
                        },
                        "required": ["x", "y"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mouse_double_click",
                    "description": "在屏幕指定坐标双击。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "description": "X 坐标"},
                            "y": {"type": "integer", "description": "Y 坐标"},
                        },
                        "required": ["x", "y"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mouse_right_click",
                    "description": "在屏幕指定坐标右键点击。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "description": "X 坐标"},
                            "y": {"type": "integer", "description": "Y 坐标"},
                        },
                        "required": ["x", "y"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mouse_move",
                    "description": "将鼠标移动到指定坐标。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "description": "X 坐标"},
                            "y": {"type": "integer", "description": "Y 坐标"},
                            "duration": {"type": "number", "description": "移动耗时（秒）", "default": 0.3},
                        },
                        "required": ["x", "y"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mouse_drag",
                    "description": "从起点拖拽鼠标到终点。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_x": {"type": "integer"},
                            "start_y": {"type": "integer"},
                            "end_x": {"type": "integer"},
                            "end_y": {"type": "integer"},
                            "duration": {"type": "number", "default": 0.5},
                        },
                        "required": ["start_x", "start_y", "end_x", "end_y"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mouse_scroll",
                    "description": "滚动鼠标滚轮。正值向上滚，负值向下滚。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "integer", "description": "滚动量（正=上，负=下）"},
                            "x": {"type": "integer", "description": "滚动位置 X（可选）"},
                            "y": {"type": "integer", "description": "滚动位置 Y（可选）"},
                        },
                        "required": ["amount"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "keyboard_type",
                    "description": "输入文本。支持英文直接输入和中文（通过剪贴板粘贴）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "要输入的文本"},
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "keyboard_press",
                    "description": "按下并释放一个按键。如 enter, tab, escape, f5, delete, backspace, space 等。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "按键名称"},
                        },
                        "required": ["key"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "keyboard_hotkey",
                    "description": "按下组合键。如 ['ctrl','c'], ['alt','tab'], ['ctrl','shift','s']。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keys": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "按键序列",
                            },
                        },
                        "required": ["keys"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "launch_app",
                    "description": "启动一个 Windows 应用程序。可以是完整路径或程序名(如 notepad, calc, mspaint)。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "可执行文件路径或程序名"},
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "命令行参数（可选）",
                            },
                            "wait": {"type": "number", "description": "启动后等待秒数", "default": 2.0},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_windows",
                    "description": "列出桌面上所有可见窗口（标题、类名、位置、大小）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "visible_only": {"type": "boolean", "default": True},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "focus_window",
                    "description": "激活/聚焦指定窗口（标题模糊匹配）。操作窗口中的控件前必须先聚焦。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "窗口标题（模糊匹配）"},
                            "handle": {"type": "integer", "description": "窗口句柄（精确匹配，可选）"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "maximize_window",
                    "description": "最大化指定窗口。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "窗口标题"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "minimize_window",
                    "description": "最小化指定窗口。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "窗口标题"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "close_window",
                    "description": "关闭指定窗口。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "窗口标题"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "find_control",
                    "description": "在当前窗口中查找控件。需先 focus_window 聚焦目标窗口。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "control_type": {
                                "type": "string",
                                "description": "控件类型：Button, Edit, Text, ListItem, ComboBox, CheckBox, RadioButton, Menu, MenuItem, TreeItem, Tab, TabItem 等",
                            },
                            "auto_id": {"type": "string", "description": "AutomationId"},
                            "name": {"type": "string", "description": "控件显示名称（Name 属性）"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "click_control",
                    "description": "点击窗口中的指定控件。需先 focus_window 聚焦目标窗口。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "control_type": {"type": "string", "description": "控件类型"},
                            "auto_id": {"type": "string", "description": "AutomationId"},
                            "name": {"type": "string", "description": "控件显示名称"},
                            "title": {"type": "string", "description": "窗口标题（可选，自动聚焦）"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "type_in_control",
                    "description": "在指定控件中输入文本。需先 focus_window 聚焦目标窗口。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "要输入的文本"},
                            "control_type": {"type": "string", "description": "控件类型"},
                            "auto_id": {"type": "string", "description": "AutomationId"},
                            "name": {"type": "string", "description": "控件显示名称"},
                            "title": {"type": "string", "description": "窗口标题（可选）"},
                            "clear_first": {"type": "boolean", "description": "输入前是否清空", "default": True},
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_controls",
                    "description": "列出当前窗口中的所有控件。需先 focus_window 聚焦目标窗口。可按控件类型过滤。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "窗口标题（可选，自动聚焦）"},
                            "control_type": {"type": "string", "description": "按控件类型过滤（可选）"},
                            "max_depth": {"type": "integer", "description": "搜索深度", "default": 4},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_screen_size",
                    "description": "获取屏幕分辨率（宽度和高度）。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_mouse_position",
                    "description": "获取当前鼠标坐标位置。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "desktop_wait",
                    "description": "等待指定秒数。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "seconds": {"type": "number", "description": "等待秒数", "default": 1.0},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_control_text",
                    "description": "获取指定控件的文本内容。需先 focus_window 聚焦目标窗口。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "control_type": {"type": "string", "description": "控件类型"},
                            "auto_id": {"type": "string", "description": "AutomationId"},
                            "name": {"type": "string", "description": "控件显示名称"},
                            "title": {"type": "string", "description": "窗口标题（可选，自动聚焦）"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "locate_on_screen",
                    "description": "在屏幕上查找目标图片的位置。返回图片中心坐标和边界矩形。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "image_path": {"type": "string", "description": "目标图片文件路径"},
                            "confidence": {"type": "number", "description": "匹配置信度(0~1)", "default": 0.8},
                            "region": {
                                "type": "object",
                                "description": "搜索区域（可选）",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "width": {"type": "integer"},
                                    "height": {"type": "integer"},
                                },
                            },
                        },
                        "required": ["image_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "click_image",
                    "description": "在屏幕上查找目标图片并点击其中心位置。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "image_path": {"type": "string", "description": "目标图片文件路径"},
                            "confidence": {"type": "number", "description": "匹配置信度(0~1)", "default": 0.8},
                        },
                        "required": ["image_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resize_window",
                    "description": "调整指定窗口的大小。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "width": {"type": "integer", "description": "新宽度"},
                            "height": {"type": "integer", "description": "新高度"},
                            "title": {"type": "string", "description": "窗口标题"},
                            "handle": {"type": "integer", "description": "窗口句柄（可选）"},
                        },
                        "required": ["width", "height"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "move_window",
                    "description": "移动指定窗口到新位置。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "description": "新 X 坐标"},
                            "y": {"type": "integer", "description": "新 Y 坐标"},
                            "title": {"type": "string", "description": "窗口标题"},
                            "handle": {"type": "integer", "description": "窗口句柄（可选）"},
                        },
                        "required": ["x", "y"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "kill_app",
                    "description": "关闭/杀死应用程序。可通过进程 PID 或进程名(如 notepad.exe)关闭。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pid": {"type": "integer", "description": "进程 ID"},
                            "name": {"type": "string", "description": "进程名称（如 notepad.exe 或 notepad）"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "window_screenshot",
                    "description": "截取指定窗口区域的截图（不截全屏，更节省资源）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "窗口标题（模糊匹配）"},
                            "handle": {"type": "integer", "description": "窗口句柄（可选）"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_pixel_color",
                    "description": "获取屏幕指定坐标的像素颜色（RGB 和 HEX 值）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "description": "X 坐标"},
                            "y": {"type": "integer", "description": "Y 坐标"},
                        },
                        "required": ["x", "y"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "done",
                    "description": "任务完成，向用户报告结果。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "description": "任务完成摘要"},
                        },
                        "required": ["summary"],
                    },
                },
            },
        ]

    # ------------------------------------------------------------------
    # 统一调度接口（与 BrowserEngine 保持一致）
    # ------------------------------------------------------------------

    def execute_action(self, action_name: str, params: dict) -> dict:
        """根据动作名称调度执行。"""
        action_map = {
            "desktop_screenshot": lambda p: self.desktop_screenshot(p.get("region")),
            "mouse_click": lambda p: self.mouse_click(
                p["x"], p["y"], p.get("button", "left"), p.get("clicks", 1),
            ),
            "mouse_double_click": lambda p: self.mouse_double_click(p["x"], p["y"]),
            "mouse_right_click": lambda p: self.mouse_right_click(p["x"], p["y"]),
            "mouse_move": lambda p: self.mouse_move(p["x"], p["y"], p.get("duration", 0.3)),
            "mouse_drag": lambda p: self.mouse_drag(
                p["start_x"], p["start_y"], p["end_x"], p["end_y"], p.get("duration", 0.5),
            ),
            "mouse_scroll": lambda p: self.mouse_scroll(p["amount"], p.get("x"), p.get("y")),
            "keyboard_type": lambda p: self.keyboard_type(p["text"], p.get("interval", 0.02)),
            "keyboard_press": lambda p: self.keyboard_press(p["key"]),
            "keyboard_hotkey": lambda p: self.keyboard_hotkey(*p["keys"]),
            "launch_app": lambda p: self.launch_app(p["path"], p.get("args"), p.get("wait", 2.0)),
            "list_windows": lambda p: self.list_windows(p.get("visible_only", True)),
            "focus_window": lambda p: self.focus_window(p.get("title", ""), p.get("handle", 0)),
            "maximize_window": lambda p: self.maximize_window(p.get("title", "")),
            "minimize_window": lambda p: self.minimize_window(p.get("title", "")),
            "close_window": lambda p: self.close_window(p.get("title", "")),
            "resize_window": lambda p: self.resize_window(
                p["width"], p["height"], p.get("title", ""), p.get("handle", 0),
            ),
            "move_window": lambda p: self.move_window(
                p["x"], p["y"], p.get("title", ""), p.get("handle", 0),
            ),
            "find_control": lambda p: self.find_control(
                title=p.get("title", ""),
                control_type=p.get("control_type", ""),
                auto_id=p.get("auto_id", ""),
                name=p.get("name", ""),
            ),
            "click_control": lambda p: self.click_control(
                control_type=p.get("control_type", ""),
                auto_id=p.get("auto_id", ""),
                name=p.get("name", ""),
                title=p.get("title", ""),
            ),
            "type_in_control": lambda p: self.type_in_control(
                text=p["text"],
                control_type=p.get("control_type", ""),
                auto_id=p.get("auto_id", ""),
                name=p.get("name", ""),
                title=p.get("title", ""),
                clear_first=p.get("clear_first", True),
            ),
            "get_control_text": lambda p: self.get_control_text(
                control_type=p.get("control_type", ""),
                auto_id=p.get("auto_id", ""),
                name=p.get("name", ""),
                title=p.get("title", ""),
            ),
            "list_controls": lambda p: self.list_controls(
                title=p.get("title", ""),
                control_type=p.get("control_type", ""),
                max_depth=p.get("max_depth", 4),
            ),
            "get_screen_size": lambda p: self.get_screen_size(),
            "get_mouse_position": lambda p: self.get_mouse_position(),
            "get_pixel_color": lambda p: self.get_pixel_color(p["x"], p["y"]),
            "desktop_wait": lambda p: self.wait(p.get("seconds", 1.0)),
            "locate_on_screen": lambda p: self.locate_on_screen(
                p["image_path"], p.get("confidence", 0.8), p.get("region"),
            ),
            "click_image": lambda p: self.click_image(
                p["image_path"], p.get("confidence", 0.8),
            ),
            "kill_app": lambda p: self.kill_app(p.get("pid", 0), p.get("name", "")),
            "window_screenshot": lambda p: self.window_screenshot(
                p.get("title", ""), p.get("handle", 0),
            ),
        }
        handler = action_map.get(action_name)
        if handler is None:
            return {"error": f"未知桌面操作: {action_name}"}
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
        if len(self._action_history) > self._MAX_ACTION_HISTORY:
            self._action_history = self._action_history[-self._MAX_ACTION_HISTORY:]

    def _window_action(self, action: str, title: str, handle: int) -> dict:
        """通用窗口操作（最小化/最大化/关闭）。"""
        try:
            hwnd = self._resolve_hwnd(title, handle)
            if not hwnd:
                return {"error": "未找到目标窗口"}

            import ctypes
            user32 = ctypes.windll.user32

            if action == "minimize":
                user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            elif action == "maximize":
                user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            elif action == "close":
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            else:
                return {"error": f"未知窗口操作: {action}"}

            result = {"success": True, "action": action}
            self._record(f"{action}_window", {"title": title, "handle": handle}, result)
            return result
        except Exception as exc:
            return {"error": f"窗口{action}失败: {exc}"}

    def _resolve_hwnd(self, title: str, handle: int) -> Optional[int]:
        """解析窗口句柄。"""
        if handle:
            return handle

        if not title:
            return None

        # 使用 Win32 API 查找
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found_hwnd = None

        def enum_handler(hwnd, _):
            nonlocal found_hwnd
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title.lower() in buf.value.lower():
                found_hwnd = hwnd
                return False
            return True

        enum_func_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM,
        )
        user32.EnumWindows(enum_func_type(enum_handler), 0)
        return found_hwnd


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------

def _contains_non_ascii(text: str) -> bool:
    """检测文本是否包含非 ASCII 字符。"""
    return any(ord(c) > 127 for c in text)


def _clipboard_paste(text: str) -> None:
    """通过剪贴板粘贴文本（支持中文等 Unicode 字符）。"""
    import ctypes

    CF_UNICODETEXT = 13
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    # OpenClipboard 可能被其他程序占用，重试最多 5 次
    for _attempt in range(5):
        if user32.OpenClipboard(0):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("无法打开剪贴板（被其他程序占用）")

    try:
        user32.EmptyClipboard()
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        h_mem = kernel32.GlobalAlloc(0x0042, len(encoded))  # GMEM_MOVEABLE | GMEM_ZEROINIT
        if not h_mem:
            raise RuntimeError("GlobalAlloc 失败")
        p_mem = kernel32.GlobalLock(h_mem)
        ctypes.memmove(p_mem, encoded, len(encoded))
        kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
    finally:
        user32.CloseClipboard()

    # 模拟 Ctrl+V 粘贴
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.1)
