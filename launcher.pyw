# -*- coding: utf-8 -*-
"""launcher.pyw — 无控制台启动器：单实例 + 后台运行时 + HTTP 服务 + 托盘 + 前端窗口

职责（见 docs/architecture-refactor.md）：
- 复用现有实例：已有实例在跑时只唤起它的窗口，本进程退出，绝不启动第二个轮换进程。
- 承载唯一后台运行时（runtime）+ 本机 HTTP 服务（server）+ 系统托盘 + 前端窗口。
- 前端用内嵌 WebView2（pywebview）承载同一套本机 Web UI，窗口的显示状态与进程绑定：
  - 最小化 → 隐藏到系统托盘，不保留在任务栏；
  - 点关闭 → 按已保存的选择处理，未保存过则弹窗询问「退出程序 / 最小化到系统托盘」
    （对话框在页面内，见 app/app.js 的 promptClose）；
  - 托盘「退出」→ 关闭前端并结束进程。
- 窗口隐藏不影响后台轮换；只有「退出程序」或托盘「退出」才结束进程。
- 取不到 WebView 运行时时退回旧行为：用浏览器 app 窗口打开界面（此时窗口不受控，
  仅托盘「退出」能结束进程）。
"""
import atexit
import ctypes
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import core
import runtime as runtime_mod
import server as server_mod
import tray

MUTEX_NAME = "pjsk-wallpaper-mutex"
NO_WINDOW = 0x08000000
WINDOW_TITLE = "pjsk 壁纸"
# 窗口关闭行为（config.json 的 close_action）合法取值
CLOSE_ACTIONS = ("exit", "tray")


# ------------------------------------------------------------------ 浏览器回退

def _browser_candidates():
    env = os.environ
    bases = [env.get("ProgramFiles"), env.get("ProgramFiles(x86)"),
             env.get("LOCALAPPDATA")]
    out = []
    for base in bases:
        if not base:
            continue
        out.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        out.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return [p for p in out if p.is_file()]


def open_app_window(port: int) -> bool:
    """回退路径：优先用浏览器 --app 模式打开；失败则回退系统默认浏览器。"""
    url = f"http://127.0.0.1:{port}/"
    profile = core.APP_DIR / "runtime" / "browser-profile"
    for exe in _browser_candidates():
        try:
            profile.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                [str(exe), f"--app={url}", f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check",
                 "--disable-features=Translate"],
                creationflags=NO_WINDOW)
            return True
        except OSError:
            continue
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception:
        return False


def saved_close_action(cfg) -> str:
    """读取已保存的窗口关闭行为；未设置或非法 → ""（每次询问）。"""
    value = (cfg or {}).get("close_action")
    return value if value in CLOSE_ACTIONS else ""


def request_show_window(info) -> bool:
    """请已有实例唤起它的前端窗口（单实例：第二次启动不再另开一个窗口）。"""
    try:
        port = int(info.get("port") or 0)
    except (TypeError, ValueError):
        return False
    token = str(info.get("token") or "")
    if not port or not token:
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/window/show", data=b"", method="POST",
        headers={server_mod.TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


# ------------------------------------------------------------------ 前端窗口

class WindowHost:
    """内嵌 WebView 窗口：把前端窗口的显示状态绑到后台进程。

    不向页面注入脚本求值（pywebview 的 `evaluate_js` 需要跨线程同步等待 JS 结果，
    在窗口管理场景下并不可靠）：需要页面配合的动作都走「状态推送 + 本机 HTTP」
    —— 请求弹框经 `Runtime.request_close_prompt()` 由长轮询送到页面，
    用户的确认结果经 `POST /api/window/close` 回来。窗口方法本身可在任意线程调用。
    """

    def __init__(self, rt, port: int):
        self._rt = rt
        self._port = port
        self.window = None
        self._exiting = False
        self._loaded = False        # 页面是否成功加载过（决定关窗时能否弹确认框）

    # ---------- 创建 ----------

    def create(self, webview_mod):
        """创建窗口并挂事件；必须在 webview.start() 之前调用。"""
        self.window = webview_mod.create_window(
            WINDOW_TITLE, f"http://127.0.0.1:{self._port}/",
            width=1180, height=800, min_size=(760, 560))
        self.window.events.closing += self._on_closing
        self.window.events.minimized += self._on_minimized
        self.window.events.loaded += self._on_loaded
        return self.window

    # ---------- 供托盘 / 页面调用 ----------

    def show_window(self):
        """从托盘/二次启动唤起前端窗口。

        顺序必须是先 show 再 restore：restore 在隐藏状态下不生效（实测窗体仍处于
        最小化，只剩任务栏图标、桌面上不出窗口），而 show 会把它置为可见但仍最小化，
        随后 restore 才能真正还原到最小化前的位置与尺寸。
        """
        if self.window is None:
            return
        self.window.show()
        self.window.restore()

    def hide_to_tray(self):
        if self.window is not None:
            self.window.hide()

    def request_exit(self):
        """结束进程：关闭前端窗口，GUI 主循环随即结束（见 Launcher.run）。"""
        if self._exiting:
            return
        self._exiting = True
        if self.window is not None:
            self.window.destroy()

    def close_window(self, action, remember):
        """页面确认框的确认结果（经 POST /api/window/close）；返回值回给页面。"""
        if action not in CLOSE_ACTIONS:
            return {"ok": False, "error": "无效的关闭操作"}
        if remember:
            ok, errors = self._rt.set_close_action(action)
            if not ok:
                return {"ok": False, "error": "；".join(errors)}
        if action == "exit":
            self.request_exit()
        else:
            self.hide_to_tray()
        return {"ok": True, "action": action, "saved": bool(remember)}

    # ---------- 窗口事件（GUI 线程） ----------

    def _on_loaded(self):
        self._loaded = True

    def _on_minimized(self):
        # 最小化不保留在任务栏，只留系统托盘图标
        self.hide_to_tray()

    def _on_closing(self):
        """用户点关闭按钮。pywebview 约定：**返回 False 表示取消本次关闭**。

        返回 True/None 即放行：窗口真的关闭 → GUI 主循环结束 → 进程退出。
        """
        if self._exiting:
            return True                 # 已决定退出：放行
        action = saved_close_action(self._rt.config())
        if action == "exit":
            self._exiting = True
            return True                 # 放行 → 主循环结束 → 进程退出
        if action == "tray":
            self.hide_to_tray()
            return False                # 取消关闭，留在托盘
        if not self._loaded:
            # 页面没加载成功，问不出结果：退回旧版「关窗 = 隐藏到托盘」
            self.hide_to_tray()
            return False
        self._rt.request_close_prompt()  # 取消关闭，让页面弹确认框
        return False


# ------------------------------------------------------------------ 单实例

def _identity_ok(port: int) -> bool:
    """校验目标端口确实是本应用的实例，避免误连到别的服务。"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/identity", timeout=2) as r:
            import json
            info = json.loads(r.read().decode("utf-8"))
        return info.get("app") == server_mod.APP_NAME
    except Exception:
        return False


def _first_instance() -> bool:
    """命名互斥体守卫；返回 False 表示已有实例在运行。

    用 use_last_error=True 的 WinDLL + ctypes.get_last_error()，避免其它 ctypes
    调用覆盖线程最后错误码导致误判。
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ERROR_ALREADY_EXISTS = 183
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    globals()["_mutex"] = k32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


class Launcher:
    def __init__(self):
        self.rt = runtime_mod.Runtime()
        self.srv = server_mod.WallpaperServer(self.rt, ui=self)
        self.tray = None
        self.host = None
        self.port = None
        self._stop = threading.Event()

    def run(self):
        self.rt.start()
        self.port = self.srv.bind()
        self.srv.write_port_file()
        atexit.register(self._cleanup_files)
        self.srv.start_background()

        self.tray = tray.Tray(core.tray_icon_path(), WINDOW_TITLE, self._on_tray)
        self.tray.start()

        if self._run_ui():
            # 内嵌窗口的 GUI 主循环结束（「退出程序」或托盘「退出」）后才返回
            self.shutdown()
        else:
            # 回退到浏览器窗口：窗口不受本进程控制，等托盘「退出」
            self._stop.wait()

    def _run_ui(self) -> bool:
        """承载前端窗口并运行 GUI 主循环；返回 False 表示已退回浏览器窗口。

        模块不可用、create_window 失败、GUI 主循环启动失败都走同一条回退：
        尝试清掉半创建的窗口、丢掉宿主引用（不留「假装有可控窗口」的状态）、
        打开浏览器窗口；托盘/HTTP 服务/调度线程/更新子进程继续运行，仍由
        托盘「退出」统一收尾，不会出现半启动无人管理的状态。
        """
        try:
            import webview
        except Exception:
            open_app_window(self.port)
            return False
        try:
            self.host = WindowHost(self.rt, self.port)
            self.host.create(webview)
            icon = core.APP_DIR / "icon.ico"
            webview.start(icon=str(icon) if icon.is_file() else None)
        except Exception:
            if self.host is not None and self.host.window is not None:
                try:
                    self.host.window.destroy()
                except Exception:
                    pass
            self.host = None
            open_app_window(self.port)
            return False
        return True

    def _on_tray(self, action):
        if action == "show":
            self.show_window()
        elif action == "next_desk":
            self.rt.switch("desktop", manual=True)
        elif action == "next_lock":
            self.rt.switch("lock", manual=True)
        elif action == "exit":
            self.exit_app()

    def show_window(self):
        if self.host is not None:
            self.host.show_window()
        else:
            open_app_window(self.port)

    def close_window(self, action, remember):
        """前端关闭确认框的结果（经 POST /api/window/close 到达）。"""
        if self.host is None:
            return {"ok": False, "error": "当前实例没有可控制的前端窗口"}
        return self.host.close_window(action, remember)

    def exit_app(self):
        """托盘「退出」：结束进程，前端窗口一并关闭。"""
        if self.host is not None:
            self.host.request_exit()
        else:
            self.shutdown()

    def shutdown(self):
        if self._stop.is_set():
            return
        self.rt.stop()
        try:
            if self.tray:
                self.tray.stop()
                self.tray._thread.join(timeout=3)
        except Exception:
            pass
        self.srv.shutdown()
        self._stop.set()

    def _cleanup_files(self):
        self.srv.clear_port_file()


def main():
    if not _first_instance():
        info = server_mod.running_instance()
        if info and _identity_ok(int(info["port"])):
            if not request_show_window(info):
                open_app_window(int(info["port"]))
        sys.exit(0)
    Launcher().run()


if __name__ == "__main__":
    main()
