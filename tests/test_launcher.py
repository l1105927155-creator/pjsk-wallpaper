# -*- coding: utf-8 -*-
"""启动器单实例与实例身份校验测试（不打开浏览器、不启动托盘）。"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import runtime as rt  # noqa: E402
import server as sv  # noqa: E402

_spec = importlib.util.spec_from_file_location("pjsk_launcher", ROOT / "launcher.pyw")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = Path(self._tmp.name)
        (self.td / "library" / "pjsk").mkdir(parents=True)
        (self.td / "library" / "custom").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        self._ps = [
            mock.patch.object(core, "CONFIG_PATH", self.td / "config.json"),
            mock.patch.object(core, "PJSK_DIR", self.td / "library" / "pjsk"),
            mock.patch.object(core, "CUSTOM_DIR", self.td / "library" / "custom"),
            mock.patch.object(core, "detect_screen", lambda: [800, 600]),
            mock.patch.object(sv, "RUNTIME_DIR", self.td / "runtime"),
            mock.patch.object(sv, "PORT_FILE", self.td / "runtime" / "server.json"),
            mock.patch.object(launcher, "server_mod", sv),
        ]
        for p in self._ps:
            p.start()
            self.addCleanup(p.stop)

    def test_single_instance_guard(self):
        # 用本进程独有的互斥体名，避免与用户机器上真实运行的实例相互干扰
        name = f"pjsk-wallpaper-test-{os.getpid()}"
        with mock.patch.object(launcher, "MUTEX_NAME", name):
            self.assertTrue(launcher._first_instance())
            self.assertFalse(launcher._first_instance())  # 同进程再次调用 = 已有实例

    def test_open_app_window_falls_back_to_default_browser(self):
        with mock.patch.object(launcher, "_browser_candidates", lambda: []), \
                mock.patch("webbrowser.open") as wb:
            self.assertTrue(launcher.open_app_window(12345))
            wb.assert_called_once()

    def test_running_instance_and_identity(self):
        rt_ = rt.Runtime(tick_seconds=9999)
        srv = sv.WallpaperServer(rt_, port=18911)
        srv.bind()
        srv.start_background()
        srv.write_port_file()
        self.addCleanup(srv.shutdown)
        info = sv.running_instance()
        self.assertIsNotNone(info)
        self.assertEqual(info["port"], srv.port)
        # 端口文件带令牌：第二次启动的进程靠它唤起已有实例的窗口
        self.assertEqual(info.get("token"), srv.token)
        self.assertTrue(launcher._identity_ok(srv.port))

    def test_identity_rejects_foreign_service(self):
        """端口上不是本应用时不得误认（避免误连到别的服务）。"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"app":"something-else"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 18912), Other)
        import threading

        def _stop():
            httpd.shutdown()
            httpd.server_close()

        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(_stop)
        self.assertFalse(launcher._identity_ok(httpd.server_port))

    def test_running_instance_none_when_no_file(self):
        self.assertIsNone(sv.running_instance())

    def test_running_instance_none_when_port_dead(self):
        (self.td / "runtime").mkdir(parents=True, exist_ok=True)
        sv.PORT_FILE.write_text(json.dumps(
            {"app": "pjsk-wallpaper", "port": 18999}), "utf-8")
        self.assertIsNone(sv.running_instance())


class FakeWindow:
    """假窗口：记录被调用的动作，供 WindowHost 的事件逻辑测试使用。"""

    def __init__(self):
        self.calls = []

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def restore(self):
        self.calls.append("restore")

    def destroy(self):
        self.calls.append("destroy")


class FakeRuntime:
    """只提供 WindowHost 需要的接口。"""

    def __init__(self, close_action=None):
        self._close_action = close_action
        self.saved = []
        self.save_ok = True
        self.errors = []
        self.prompts = 0

    def config(self):
        return {"close_action": self._close_action}

    def set_close_action(self, value):
        if not self.save_ok:
            return False, self.errors or ["保存配置失败"]
        self.saved.append(value)
        self._close_action = value
        return True, []

    def request_close_prompt(self):
        self.prompts += 1


class WindowHostTest(unittest.TestCase):
    """前端窗口的显示状态与进程绑定：最小化/关闭/托盘退出的行为。"""

    def _host(self, close_action=None, loaded=True):
        rt_ = FakeRuntime(close_action)
        host = launcher.WindowHost(rt_, 18999)
        host.window = FakeWindow()
        host._loaded = loaded
        return host, rt_

    # ---------------- 显示 / 隐藏 ----------------

    def test_show_restores_before_showing(self):
        host, _ = self._host()
        host.show_window()
        # 必须先还原再显示，否则隐藏前若处于最小化会一直留在最小化状态
        self.assertEqual(host.window.calls, ["restore", "show"])

    def test_minimize_hides_to_tray(self):
        host, _ = self._host()
        host._on_minimized()
        self.assertEqual(host.window.calls, ["hide"])

    # ---------------- 关闭行为 ----------------

    def test_close_with_saved_tray_hides_and_cancels(self):
        host, _ = self._host(close_action="tray")
        self.assertFalse(host._on_closing())         # False = 取消关闭（pywebview 约定）
        self.assertEqual(host.window.calls, ["hide"])

    def test_close_with_saved_exit_allows_close(self):
        host, _ = self._host(close_action="exit")
        self.assertTrue(host._on_closing())          # True = 放行 → 进程退出
        self.assertEqual(host.window.calls, [])
        self.assertTrue(host._exiting)

    def test_close_without_saved_choice_asks_page(self):
        """未保存过选择：取消关闭并请求页面弹确认框（经状态推送，不注入脚本）。"""
        host, rt_ = self._host()
        self.assertFalse(host._on_closing())
        self.assertEqual(rt_.prompts, 1)
        self.assertEqual(host.window.calls, [])      # 不隐藏

    def test_close_falls_back_to_tray_when_page_never_loaded(self):
        """页面没加载成功就问不出结果：退回旧版「关窗 = 隐藏到托盘」。"""
        host, rt_ = self._host(loaded=False)
        self.assertFalse(host._on_closing())
        self.assertEqual(rt_.prompts, 0)
        self.assertEqual(host.window.calls, ["hide"])

    # ---------------- 页面确认框的回传 ----------------

    def test_decision_tray_remembers_choice(self):
        host, rt_ = self._host()
        res = host.close_window("tray", True)
        self.assertEqual(res, {"ok": True, "action": "tray", "saved": True})
        self.assertEqual(rt_.saved, ["tray"])
        self.assertEqual(host.window.calls, ["hide"])

    def test_decision_exit_without_remember_exits(self):
        host, rt_ = self._host()
        res = host.close_window("exit", False)
        self.assertTrue(res["ok"])
        self.assertEqual(rt_.saved, [])              # 未勾选「不再显示」→ 不保存
        self.assertEqual(host.window.calls, ["destroy"])
        self.assertTrue(host._exiting)

    def test_decision_rejects_unknown_action(self):
        host, rt_ = self._host()
        res = host.close_window("nope", True)
        self.assertFalse(res["ok"])
        self.assertEqual(rt_.saved, [])
        self.assertEqual(host.window.calls, [])

    def test_decision_reports_save_failure(self):
        host, rt_ = self._host()
        rt_.save_ok = False
        res = host.close_window("tray", True)
        self.assertFalse(res["ok"])
        self.assertEqual(host.window.calls, [])      # 保存失败则不执行动作

    # ---------------- 托盘「退出」 ----------------

    def test_request_exit_destroys_window_once(self):
        host, _ = self._host()
        host.request_exit()
        host.request_exit()
        self.assertEqual(host.window.calls, ["destroy"])
        self.assertTrue(host._on_closing())           # 退出途中不再拦截关闭

    # ---------------- 已保存值的读取 ----------------

    def test_saved_close_action_tolerates_bad_values(self):
        self.assertEqual(launcher.saved_close_action({"close_action": "exit"}), "exit")
        self.assertEqual(launcher.saved_close_action({"close_action": "tray"}), "tray")
        self.assertEqual(launcher.saved_close_action({"close_action": "boo"}), "")
        self.assertEqual(launcher.saved_close_action({}), "")
        self.assertEqual(launcher.saved_close_action(None), "")

    # ---------------- 第二次启动：唤起已有实例的窗口 ----------------

    def test_request_show_window_posts_token(self):
        seen = {}

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["token"] = next((v for k, v in req.headers.items()
                                  if k.lower() == "x-pjsk-token"), None)
            return FakeResp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertTrue(launcher.request_show_window(
                {"port": 18911, "token": "abc"}))
        self.assertTrue(seen["url"].endswith("/api/window/show"))
        self.assertEqual(seen["token"], "abc")

    def test_request_show_window_needs_port_and_token(self):
        for info in ({}, {"port": 1}, {"token": "abc"}, {"port": "x", "token": "y"}):
            self.assertFalse(launcher.request_show_window(info))

    def test_request_show_window_failure_is_reported_not_raised(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            self.assertFalse(launcher.request_show_window({"port": 1, "token": "t"}))


if __name__ == "__main__":
    unittest.main()
