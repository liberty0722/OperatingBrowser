# -*- coding: utf-8 -*-
"""手动录制引擎 — 捕获用户真实操作并转换为工作流步骤。

两种录制模式:
1. browser: 在受控浏览器页面注入 JS 监听脚本，捕获真实点击/输入/导航，
   并生成带 CSS 选择器的步骤（回放时可直接复用 replay_workflow）。
2. desktop: 使用 pynput 全局钩子捕获鼠标/键盘事件，生成坐标步骤。
   （回放需要桌面引擎，格式与 desktop_engine.execute_action 兼容）

录制快捷键（桌面模式）:
    F10 = 停止录制并保存
    F9  = 暂停/继续录制（本次未启用，保留扩展）
"""

import json
import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# 浏览器录制注入的监听脚本（DOM 事件 → window.__recEvent push）
_INJECT_SCRIPT = r"""
(() => {
  if (window.__recInstalled) return;
  window.__recInstalled = true;
  window.__recEvent = window.__recEvent || [];

  function cssPath(el) {
    if (!(el instanceof Element)) return '';
    const path = [];
    while (el && el.nodeType === 1 && path.length < 12) {
      let sel = el.tagName.toLowerCase();
      if (el.id) { path.unshift('#' + CSS.escape(el.id)); break; }
      // 尝试用稳定的属性定位（常见测试属性）
      const attr = el.getAttribute('data-testid') || el.getAttribute('data-test')
        || el.getAttribute('name') || el.getAttribute('aria-label');
      if (attr) { path.unshift(sel + '[' + (el.getAttribute('data-testid') ? 'data-testid' : el.getAttribute('data-test') ? 'data-test' : (el.getAttribute('name') ? 'name' : 'aria-label')) + '="' + CSS.escape(attr) + '"]'); break; }
      const parent = el.parentNode;
      if (parent) {
        const sibs = Array.from(parent.children).filter(s => s.tagName === el.tagName);
        if (sibs.length > 1) {
          const idx = sibs.indexOf(el) + 1;
          sel += `:nth-of-type(${idx})`;
        }
      }
      path.unshift(sel);
      el = el.parentNode;
    }
    return path.join(' > ');
  }

  function push(type, detail) {
    try {
      window.__recEvent.push({
        t: Date.now(),
        type,
        url: location.href,
        ...detail
      });
      if (window.__recEvent.length > 500) window.__recEvent.splice(0, window.__recEvent.length - 500);
    } catch (e) { /* ignore */ }
  }

  // 输入合并: 记录 typing 会话，失焦或 800ms 无输入时提交
  let typing = null;
  function flushTyping() {
    if (!typing) return;
    if (typing.value) {
      push('input', { selector: typing.selector, text: typing.value });
    }
    typing = null;
  }
  setInterval(flushTyping, 900);

  document.addEventListener('click', e => {
    flushTyping();
    const el = e.target;
    if (!el) return;
    push('click', { selector: cssPath(el), tag: el.tagName, text: (el.innerText || '').slice(0, 30) });
  }, true);

  document.addEventListener('change', e => {
    const el = e.target;
    if (!el || !(el instanceof Element)) return;
    if (el.tagName === 'SELECT') {
      push('select', { selector: cssPath(el), value: el.value });
    }
  }, true);

  document.addEventListener('input', e => {
    const el = e.target;
    if (!el || !(el instanceof Element)) return;
    const tag = el.tagName;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'TEXTAREA' || tag === 'INPUT' && ['text', 'password', 'search', 'email', 'url', 'number', 'tel'].includes(type)) {
      const sel = cssPath(el);
      if (!typing || typing.selector !== sel) {
        flushTyping();
        typing = { selector: sel, value: el.value };
      } else {
        typing.value = el.value;
      }
    }
  }, true);

  document.addEventListener('keydown', e => {
    // 回车提交输入框
    if (e.key === 'Enter' && typing) {
      const v = typing.value;
      const sel = typing.selector;
      typing = null;
      if (v) push('input', { selector: sel, text: v });
      push('keydown', { selector: sel, key: 'Enter' });
    } else if (['Tab', 'Escape'].includes(e.key)) {
      flushTyping();
      push('keydown', { selector: '', key: e.key });
    }
  }, true);
})();
"""

# 清理脚本（停止录制时移除全局标记，防止污染页面）
_CLEANUP_SCRIPT = "delete window.__recInstalled; delete window.__recEvent;"

# 录制事件 → 工作流步骤的映射
def _events_to_steps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    last_url: Optional[str] = None
    # 当前页签的导航栈（兜底后退识别，不依赖轮询时机）
    nav_stack: list[str] = []

    def _norm(u: Optional[str]) -> Optional[str]:
        return u.rstrip("/") if u else u

    for ev in events:
        etype = ev.get("type")
        if etype == "navigate":
            url = ev.get("url", "")
            n = _norm(url)
            # 后退导航：回放用 back() 从缓存恢复页面状态（搜索结果等），
            # goto 重载会丢失这些状态导致后续点击找不到元素
            is_back = ev.get("nav") == "back" or (
                n and len(nav_stack) >= 2 and nav_stack[-2] == n and nav_stack[-1] != n
            )
            if is_back:
                # 去重：轮询与 URL 变化检测可能对同一次后退各产生一次事件
                if not (n and nav_stack and nav_stack[-1] == n and steps and steps[-1]["action"] == "back"):
                    steps.append({"action": "back", "params": {}})
                    if len(nav_stack) >= 2:
                        nav_stack.pop()
                last_url = url if url else last_url
                continue
            if url and n != _norm(last_url):
                steps.append({"action": "goto", "params": {"url": url}})
                last_url = url
                nav_stack.append(n)
        elif etype == "switch_tab":
            url = ev.get("url", "")
            params: dict[str, Any] = {}
            if url:
                params["url"] = url
            idx = ev.get("tab_index", -1)
            if idx is not None and idx >= 0:
                params["index"] = idx
            if ev.get("popup"):
                params["popup"] = True
            if params:
                prev = steps[-1] if steps else None
                if prev and prev["action"] == "switch_tab" and prev["params"].get("url") == url:
                    prev["params"].update(params)
                else:
                    steps.append({"action": "switch_tab", "params": params})
                nav_stack = [_norm(url)] if url else []
                last_url = url or last_url
        elif etype == "new_tab":
            url = ev.get("url", "")
            steps.append({"action": "new_tab", "params": {"url": url}})
            last_url = url
            nav_stack = [_norm(url)] if url else []
        elif etype == "close_tab":
            steps.append({"action": "close_tab", "params": {}})
        elif etype == "click":
            sel = ev.get("selector", "")
            if sel:
                steps.append({"action": "click", "params": {"selector": sel}})
        elif etype == "input":
            sel = ev.get("selector", "")
            text = ev.get("text", "")
            if sel and text:
                steps.append({"action": "type_text", "params": {"selector": sel, "text": text}})
        elif etype == "select":
            sel = ev.get("selector", "")
            value = ev.get("value", "")
            if sel:
                steps.append({"action": "select_option", "params": {"selector": sel, "value": value}})
        elif etype == "keydown":
            key = ev.get("key", "")
            if key in ("Enter", "Tab", "Escape"):
                steps.append({"action": "press_key", "params": {"key": key}})

    # 合并同选择器的连续输入（输入法中间态），保留最终值
    merged: list[dict[str, Any]] = []
    for s in steps:
        if (
            s["action"] == "type_text"
            and merged
            and merged[-1]["action"] == "type_text"
            and merged[-1]["params"]["selector"] == s["params"]["selector"]
        ):
            merged[-1] = s
            continue
        merged.append(s)
    steps = merged

    # 前置起始导航：录制从既有页面开始时，回放需要先到达该页面
    first_tab_actions = ("goto", "new_tab", "switch_tab")
    if steps and steps[0]["action"] not in first_tab_actions:
        first_url = next((e.get("url") for e in events if e.get("url")), None)
        if first_url:
            steps.insert(0, {"action": "goto", "params": {"url": first_url}})

    # 去重连续重复步骤
    deduped: list[dict[str, Any]] = []
    for s in steps:
        if deduped and deduped[-1] == s:
            continue
        deduped.append(s)
    return deduped


class ManualRecorder:
    """手动录制管理器 — 单例，管理浏览器或桌面录制会话。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mode: Optional[str] = None          # "browser" | "desktop"
        self.recording: bool = False
        self.paused: bool = False
        self.events: list[dict[str, Any]] = []   # 原始事件
        self.browser_engine = None               # 浏览器录制时引用
        self._context_ref = None                 # 浏览器 context（用于 add_init_script）
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._listener = None                    # pynput listener
        self._on_event: Optional[Callable[[dict], None]] = None
        self._last_desktop_pos: Optional[tuple[int, int]] = None
        self._desktop_typing: dict = {}          # {buffer, last_time}
        self._last_scroll: float = 0.0

    # ------------------------------------------------------------------
    # 浏览器录制
    # ------------------------------------------------------------------

    def start_browser_recording(self, browser_engine, on_event: Optional[Callable[[dict], None]] = None) -> dict:
        """注入监听脚本并开始轮询页面事件。"""
        with self._lock:
            if self.recording:
                return {"error": "已有录制会话在进行中"}
            if not browser_engine or not getattr(browser_engine, "is_connected", False):
                return {"error": "请先启动浏览器"}
            self.mode = "browser"
            self.recording = True
            self.paused = False
            self.events = []
            self.browser_engine = browser_engine
            self._on_event = on_event
            self._stop_flag.clear()

        # 注入脚本到当前页面（通过引擎的跨线程调用桥）
        try:
            browser_engine.execute_action("execute_js", {"script": _INJECT_SCRIPT})
        except Exception as exc:
            with self._lock:
                self.recording = False
            return {"error": f"注入监听脚本失败: {exc}"}

        # 在 context 级别注册初始化脚本：之后所有新页面/导航都会自动注入
        try:
            ctx = getattr(browser_engine, "_context", None)
            if ctx is not None:
                def _install_init():
                    ctx.add_init_script(_INJECT_SCRIPT)
                browser_engine._cross_thread_call(_install_init)
                self._context_ref = ctx
        except Exception:
            pass  # init script 安装失败不影响已有页面录制

        # 记录起始 URL
        try:
            info = browser_engine.execute_action("get_page_info", {})
            url = info.get("url", "")
            if url:
                self._push_event({"type": "navigate", "url": url, "t": time.time()})
        except Exception:
            pass

        # 启动轮询线程（在浏览器 owner 线程之外的普通线程轮询队列）
        self._poll_thread = threading.Thread(target=self._browser_poll_loop, daemon=True)
        self._poll_thread.start()
        logger.info("浏览器手动录制已开始")
        return {"success": True, "message": "浏览器录制已开始，请在浏览器中正常操作"}

    def _browser_poll_loop(self) -> None:
        """轮询页面 window.__recEvent 队列，拉取新事件。"""
        poll_script = "(function(){var q=window.__recEvent;return JSON.stringify({e:q&&q.length?q.splice(0):[],u:location.href})})()"
        fail_count = 0
        warned = False
        # 前台页签跟踪：用于检测用户切换页签并生成 switch_tab 事件
        active_url: Optional[str] = None
        active_page = None
        known_tabs: set = set()
        dead_pages: set = set()  # 已关闭页面的 id，避免反复重试拉高失败计数
        # 每页 URL 访问栈：用于检测后退导航（回到曾访问过的 URL = back）
        # 覆盖 bfcache 恢复、整页重载、SPA pushState 等所有情况，
        # 比 popstate 监听更可靠（整页重载后监听脚本丢失，popstate 不会触发）
        page_hist: dict[int, list[str]] = {}
        # 页签身份跟踪（供 _handle_browser_event 识别页签切换）
        self._seen_tabs = set()
        self._last_tab = None
        prev_ids: set = set()
        while not self._stop_flag.is_set():
            time.sleep(0.5)
            engine = self.browser_engine
            if not engine or not getattr(engine, "is_connected", False):
                continue
            if self.paused:
                continue

            # 收集要轮询的页面：主页面 + 其他标签页（跳过已判定关闭的）
            # 注意：ctx.pages 等 Playwright 对象访问必须派发到 owner 线程
            pages = []
            ctx_ok = False
            try:
                if id(engine.page) not in dead_pages:
                    pages.append(engine.page)
            except Exception:
                pass
            ctx = getattr(engine, "_context", None)
            if ctx is not None:
                try:
                    ctx_pages = engine._cross_thread_call(
                        lambda: list(ctx.pages), timeout=3
                    )
                    ctx_ok = True
                    for p in ctx_pages:
                        if id(p) not in dead_pages and p not in pages:
                            pages.append(p)
                except Exception:
                    pass

            # 检测页签关闭：上次还在、这次消失的页签；若正是当前操作页签 → close_tab
            if ctx_ok:
                cur_ids = {id(p) for p in pages}
                for gone in prev_ids - cur_ids:
                    if gone == self._last_tab:
                        self._handle_browser_event({"type": "close_tab", "t": time.time() * 1000})
                prev_ids = cur_ids

            # 检测前台页签变化（visibilityState: 前台为 visible，后台为 hidden）
            # 用户切换页签后，新前台页签的 visibilityState 变为 visible
            for p in pages:
                try:
                    res = engine._cross_thread_call(
                        lambda pg=p: {"return_value": pg.evaluate(
                            "document.visibilityState === 'visible' ? location.href : null"
                        )},
                        timeout=3,
                    )
                    vis_url = res.get("return_value")
                except Exception:
                    continue
                if not vis_url:
                    continue
                # 前台页签发生变化（排除同一页签内的导航，那由 navigate 事件处理）
                if active_page is not None and p is not active_page and vis_url:
                    if p in known_tabs:
                        # 已知页签 → 切换
                        self._handle_browser_event({
                            "type": "switch_tab",
                            "url": vis_url,
                            "tab_index": self._find_page_index(engine, p),
                            "t": time.time() * 1000,
                            "_tab": id(p),
                        })
                    else:
                        # 录制期间新出现的页签 → 视为点击弹出的页签，回放时切过去
                        self._handle_browser_event({
                            "type": "switch_tab",
                            "url": vis_url,
                            "popup": True,
                            "t": time.time() * 1000,
                            "_tab": id(p),
                        })
                        known_tabs.add(p)
                active_url = vis_url
                active_page = p
                known_tabs.add(p)
                break  # 同一时刻只有一个页签可见

            got_any = False
            live_failed = False
            for page in pages:
                try:
                    res = engine._cross_thread_call(
                        lambda p=page: {"return_value": p.evaluate(poll_script)},
                        timeout=5,
                    )
                    raw = res.get("return_value") or "{}"
                    try:
                        payload = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
                    except Exception:
                        payload = {}
                    batch = payload.get("e") or []
                    cur_url = payload.get("u") or ""
                    if not isinstance(batch, list):
                        batch = []
                    for ev in batch:
                        if isinstance(ev, dict):
                            ev["_tab"] = id(page)
                        self._handle_browser_event(ev)
                    if batch:
                        got_any = True
                    # 页面导航后监听脚本可能丢失，检测并重注入
                    if self._needs_reinject(page):
                        self._reinject(page)

                    # --- 后退导航检测（URL 历史栈，复用同一次调用的 URL） ---
                    # 拉取当前 URL，与该页的访问历史比对：
                    # 回到曾经访问过的 URL → 用户按了后退（或 history.back）
                    if cur_url:
                        pid = id(page)
                        hist = page_hist.setdefault(pid, [])
                        # 去掉末尾斜杠再比对，避免 /path 与 /path/ 误判
                        norm = cur_url.rstrip("/")
                        if not hist:
                            hist.append(norm)
                        elif hist[-1] != norm:
                            if norm in hist[:-1]:
                                # 回到历史中的旧 URL → 后退
                                self._handle_browser_event({
                                    "type": "navigate",
                                    "nav": "back",
                                    "url": cur_url,
                                    "t": time.time() * 1000,
                                })
                                # 弹栈到该位置（模拟浏览器历史行为）
                                while hist and hist[-1] != norm:
                                    hist.pop()
                            else:
                                # 新 URL → 前进/正常导航，入栈
                                hist.append(norm)
                    else:
                        page_hist.pop(id(page), None)
                except Exception:
                    # 页面可能已关闭：探测确认后加入死页面名单，不再反复轮询
                    try:
                        engine._cross_thread_call(lambda p=page: {"return_value": p.evaluate("1")}, timeout=3)
                        live_failed = True  # 页面还活着，只是事件脚本调用失败
                    except Exception:
                        dead_pages.add(id(page))
                    continue

            if live_failed and not pages:
                # 所有页面都被判定死亡
                live_failed = True

            if live_failed:
                fail_count += 1
                if fail_count >= 10 and not warned:
                    warned = True
                    cb = self._on_event
                    if cb:
                        try:
                            cb({"type": "warn", "message": "轮询页面事件失败超过 10 次，录制可能中断；请检查页面是否已关闭或浏览器是否正常"})
                        except Exception:
                            pass
            else:
                fail_count = 0
                warned = False

    def _find_page_index(self, engine, page) -> int:
        """返回页面对象在引擎 _pages 列表中的索引（找不到返回 -1）。"""
        try:
            res = engine._cross_thread_call(
                lambda: {"return_value": [
                    i for i, (pg, __) in enumerate(engine._pages) if pg is page
                ]},
                timeout=3,
            )
            idx = res.get("return_value") or [-1]
            return idx[0] if isinstance(idx, list) and idx else -1
        except Exception:
            return -1

    def _needs_reinject(self, page) -> bool:
        """检查页面是否已丢失监听脚本（导航到新文档后 __recInstalled 会消失）。"""
        try:
            res = self.browser_engine._cross_thread_call(
                lambda: {"return_value": page.evaluate("!!window.__recInstalled")},
                timeout=3,
            )
            return not res.get("return_value")
        except Exception:
            return False

    def _reinject(self, page) -> None:
        """向页面重新注入监听脚本。"""
        try:
            self.browser_engine._cross_thread_call(
                lambda: {"return_value": page.evaluate(_INJECT_SCRIPT)},
                timeout=3,
            )
        except Exception:
            pass

    def _handle_browser_event(self, ev: dict) -> None:
        """处理单条浏览器事件: URL 变化 → navigate，页签变化 → switch_tab，其余原样存储并回调。

        ev 可带内部字段 _tab（来源页签 id，由轮询附加）。点击打开新标签页的网站
        （如超星），后续操作发生在另一页签，必须记录成 switch_tab，否则会被误判为
        同页导航，回放时在原页签整页跳转，冲掉列表页的搜索状态。
        """
        tab = ev.pop("_tab", None)
        url = ev.get("url", "")
        etype = ev.get("type")
        if not hasattr(self, "_seen_tabs"):
            self._seen_tabs = set()
            self._last_tab = None

        if etype in ("switch_tab", "new_tab"):
            if tab is not None:
                self._last_tab = tab
                self._seen_tabs.add(tab)
            self._push_event(ev)
            return
        if etype == "close_tab":
            self._last_tab = "__closed__"
            self._push_event(ev)
            return

        if etype != "navigate" and tab is not None:
            if self._last_tab is None:
                self._last_tab = tab
                self._seen_tabs.add(tab)
            elif tab != self._last_tab:
                # 操作发生在另一个页签 → 页签切换（新出现的页签视为点击弹出）
                self._push_event({
                    "type": "switch_tab",
                    "url": url,
                    "popup": tab not in self._seen_tabs,
                    "t": ev.get("t", time.time() * 1000),
                })
                self._last_tab = tab
                self._seen_tabs.add(tab)

        if etype not in ("navigate", "switch_tab", "new_tab", "close_tab") and url:
            # 检测导航（点击链接导致 URL 变化）
            # 注意：带 nav 标记的事件（popstate 已带方向信息）跳过，避免覆盖
            if ev.get("nav"):
                pass
            else:
                last = None
                for e in reversed(self.events):
                    if e.get("type") in ("navigate", "new_tab", "switch_tab"):
                        last = e.get("url")
                        break
                if last and url != last:
                    self._push_event({"type": "navigate", "url": url, "t": ev.get("t", time.time() * 1000)})
        self._push_event(ev)

    def _push_event(self, ev: dict) -> None:
        with self._lock:
            self.events.append(ev)
        cb = self._on_event
        if cb:
            try:
                cb(ev)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 桌面录制
    # ------------------------------------------------------------------

    def start_desktop_recording(self, desktop_engine=None, on_event: Optional[Callable[[dict], None]] = None) -> dict:
        """使用 pynput 全局钩子开始桌面录制。F10 停止。"""
        try:
            from pynput import keyboard, mouse
        except ImportError:
            return {"error": "缺少 pynput 依赖，请先安装: pip install pynput"}

        with self._lock:
            if self.recording:
                return {"error": "已有录制会话在进行中"}
            self.mode = "desktop"
            self.recording = True
            self.paused = False
            self.events = []
            self._on_event = on_event
            self._last_desktop_pos = None
            self._desktop_typing = {"buffer": [], "last_time": 0.0}
            self._last_scroll = 0.0
            self._stop_flag.clear()

        recorder = self

        def on_click(x, y, button, pressed):
            if pressed:
                recorder._flush_desktop_typing()
                btn = "left" if button.name == "left" else "right" if button.name == "right" else "middle"
                recorder._push_event({"type": "mouse_click", "x": x, "y": y, "button": btn, "t": time.time()})

        def on_scroll(x, y, dx, dy):
            now = time.time()
            if now - recorder._last_scroll < 0.4:
                return  # 滚轮合并
            recorder._last_scroll = now
            recorder._flush_desktop_typing()
            recorder._push_event({"type": "mouse_scroll", "amount": int(dy * 3 if abs(dy) <= 3 else dy), "x": x, "y": y, "t": now})

        def on_key(key):
            # F10 停止录制
            if key == keyboard.Key.f10:
                recorder.stop()
                return
            try:
                ch = key.char
                if ch is not None:
                    recorder._desktop_typing["buffer"].append(ch)
                    recorder._desktop_typing["last_time"] = time.time()
            except AttributeError:
                name = getattr(key, "name", "")
                recorder._flush_desktop_typing()
                if name in ("enter", "tab", "esc", "space", "backspace", "delete", "up", "down", "left", "right"):
                    recorder._push_event({"type": "key", "key": name, "t": time.time()})
                elif name in ("ctrl_l", "ctrl_r", "alt_l", "alt_gr", "shift", "shift_l", "shift_r", "cmd", "win_l", "win_r"):
                    recorder._push_event({"type": "modkey", "key": name, "t": time.time()})

        try:
            self._listener = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
            self._listener.start()
            kbd = keyboard.Listener(on_press=on_key)
            kbd.start()
            self._kbd_listener = kbd
        except Exception as exc:
            with self._lock:
                self.recording = False
            return {"error": f"启动全局钩子失败: {exc}"}

        logger.info("桌面手动录制已开始（F10 停止）")
        return {"success": True, "message": "桌面录制已开始，请正常操作，按 F10 结束"}

    def _flush_desktop_typing(self) -> None:
        """把连续字符输入合并为一条 keyboard_type 事件。"""
        buf = self._desktop_typing.get("buffer") or []
        if buf:
            text = "".join(buf)
            if text.strip():
                self._push_event({"type": "keyboard_type", "text": text, "t": time.time()})
        self._desktop_typing["buffer"] = []
        self._desktop_typing["last_time"] = 0.0

    # ------------------------------------------------------------------
    # 通用控制
    # ------------------------------------------------------------------

    def pause(self) -> dict:
        with self._lock:
            if not self.recording:
                return {"error": "当前没有录制会话"}
            self.paused = not self.paused
            return {"success": True, "paused": self.paused}

    def stop(self, save: bool = False) -> dict:
        """停止录制。save=True 时返回步骤列表（不落盘，由调用方保存）。"""
        with self._lock:
            was_recording = self.recording
            self.recording = False
            self.paused = False
            self._stop_flag.set()
            mode = self.mode
            engine = self.browser_engine
            self.browser_engine = None
            ctx_ref = getattr(self, "_context_ref", None)
            self._context_ref = None

        # 停止 pynput 监听
        for lst in (getattr(self, "_listener", None), getattr(self, "_kbd_listener", None)):
            if lst:
                try:
                    lst.stop()
                except Exception:
                    pass
        self._listener = None
        self._kbd_listener = None

        # 桌面模式: 冲刷残留输入
        if mode == "desktop":
            self._flush_desktop_typing()

        # 浏览器模式: 清理注入脚本（有超时保护，页面阻塞时最多等 3 秒）
        if mode == "browser" and engine and getattr(engine, "is_connected", False):
            try:
                def _cleanup():
                    engine.page.evaluate(_CLEANUP_SCRIPT)
                engine._cross_thread_call(_cleanup, timeout=3)
            except Exception:
                pass
            # 移除 init script 注册（Playwright 无移除 API，通过停止轮询即可，脚本不再被读取）
            _ = ctx_ref

        with self._lock:
            events = list(self.events)
            self.events = []

        if not was_recording:
            return {"error": "当前没有录制会话"}

        steps = self._convert(mode, events)
        return {"success": True, "mode": mode, "events_count": len(events), "steps": steps}

    def _convert(self, mode: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """原始事件 → 可回放工作流步骤。"""
        if mode == "browser":
            return _events_to_steps(events)
        # 桌面模式
        steps: list[dict[str, Any]] = []
        hotkey_buf: list[str] = []
        last_mod_time = 0.0
        for ev in events:
            etype = ev.get("type")
            if etype == "mouse_click":
                params = {"x": ev["x"], "y": ev["y"]}
                if ev.get("button") and ev["button"] != "left":
                    params["button"] = ev["button"]
                steps.append({"action": "mouse_click", "params": params})
            elif etype == "mouse_scroll":
                steps.append({"action": "mouse_scroll", "params": {"amount": ev.get("amount", 3), "x": ev.get("x"), "y": ev.get("y")}})
            elif etype == "keyboard_type":
                steps.append({"action": "keyboard_type", "params": {"text": ev["text"]}})
            elif etype == "key":
                steps.append({"action": "keyboard_press", "params": {"key": ev["key"].upper()}})
            elif etype == "modkey":
                # 连续修饰键 → keyboard_hotkey（简化处理: 相邻合并）
                now = ev.get("t", 0)
                if now - last_mod_time > 0.5 and hotkey_buf:
                    steps.append({"action": "keyboard_hotkey", "params": {"keys": hotkey_buf}})
                    hotkey_buf = []
                hotkey_buf.append(_normalize_modkey(ev["key"]))
                last_mod_time = now
        if hotkey_buf:
            steps.append({"action": "keyboard_hotkey", "params": {"keys": hotkey_buf}})
        return steps

    def get_status(self) -> dict:
        with self._lock:
            return {
                "recording": self.recording,
                "paused": self.paused,
                "mode": self.mode,
                "events_count": len(self.events),
            }


def _normalize_modkey(name: str) -> str:
    """pynput 修饰键名 → pyautogui 键名。"""
    mapping = {
        "ctrl_l": "ctrl", "ctrl_r": "ctrl",
        "alt_l": "alt", "alt_gr": "alt",
        "shift": "shift", "shift_l": "shift", "shift_r": "shift",
        "cmd": "win", "win_l": "win", "win_r": "win",
    }
    return mapping.get(name, name)


# 全局单例
manual_recorder = ManualRecorder()
