# -*- coding: utf-8 -*-
"""启动器单实例与实例身份校验测试（不打开浏览器、不启动托盘）。"""
import importlib.util
import json
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
        self.assertTrue(launcher._first_instance())
        self.assertFalse(launcher._first_instance())   # 同进程再次调用 = 已有实例

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


if __name__ == "__main__":
    unittest.main()
