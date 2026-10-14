# -*- coding: utf-8 -*-
"""launcher.pyw — 无控制台启动器：单实例 + 后台运行时 + HTTP 服务 + 托盘 + 浏览器应用窗口

职责（见 docs/architecture-refactor.md）：
- 复用现有实例：已有实例在跑时只打开/聚焦它的浏览器应用窗口，本进程退出，
  绝不启动第二个轮换进程。
- 承载唯一后台运行时（runtime）+ 本机 HTTP 服务（server）+ 系统托盘。
- 用 Edge/Chrome 的 `--app` 模式打开界面（优先复用已安装浏览器，找不到时回退默认浏览器）。
- 浏览器窗口关闭/刷新/崩溃都不影响后台轮换；只有托盘「退出」才结束进程。
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
    """优先用浏览器 --app 模式打开；失败则回退系统默认浏览器。"""
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
        self.srv = server_mod.WallpaperServer(self.rt)
        self.tray = None
        self.port = None
        self._stop = threading.Event()

    def run(self):
        self.rt.start()
        self.port = self.srv.bind()
        self.srv.write_port_file()
        atexit.register(self._cleanup_files)
        self.srv.start_background()

        self.tray = tray.Tray(core.tray_icon_path(), "pjsk 壁纸", self._on_tray)
        self.tray.start()

        open_app_window(self.port)
        self._stop.wait()

    def _on_tray(self, action):
        if action == "show":
            open_app_window(self.port)
        elif action == "next_desk":
            self.rt.switch("desktop", manual=True)
        elif action == "next_lock":
            self.rt.switch("lock", manual=True)
        elif action == "exit":
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
            open_app_window(int(info["port"]))
        sys.exit(0)
    Launcher().run()


if __name__ == "__main__":
    main()
