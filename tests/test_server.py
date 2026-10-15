# -*- coding: utf-8 -*-
"""HTTP 服务单测：鉴权、Host/Origin 校验、受控图片路由、配置校验、上传。

在随机端口起真实服务，用 http.client 直连（127.0.0.1）。
"""
import http.client
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import core  # noqa: E402
import runtime as rt  # noqa: E402
import server as sv  # noqa: E402


class ServerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = Path(self._tmp.name)
        self.pjsk = self.td / "library" / "pjsk"
        self.custom = self.td / "library" / "custom"
        self.pjsk.mkdir(parents=True)
        self.custom.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        self._ps = [
            mock.patch.object(core, "CONFIG_PATH", self.td / "config.json"),
            mock.patch.object(core, "PJSK_DIR", self.pjsk),
            mock.patch.object(core, "CUSTOM_DIR", self.custom),
            mock.patch.object(core, "detect_screen", lambda: [800, 600]),
            mock.patch.object(core, "enable_desktop_fade", lambda: True),
            mock.patch.object(core, "set_desktop", lambda p: None),
            mock.patch.object(core, "set_lockscreen", lambda p: None),
            mock.patch.object(sv, "RUNTIME_DIR", self.td / "runtime"),
            mock.patch.object(sv, "WEB_DIR", Path(__file__).resolve().parent.parent / "app"),
        ]
        for p in self._ps:
            p.start()
            self.addCleanup(p.stop)
        # 造两张图片
        self._mkimg(self.pjsk / "card_1.png")
        self._mkimg(self.custom / "mine.png")
        self.rt = rt.Runtime(tick_seconds=9999)
        self.srv = sv.WallpaperServer(self.rt, port=18800 + (id(self) % 100))
        self.srv.bind()
        self.srv.start_background()
        self.addCleanup(self.srv.shutdown)
        self.token = self.srv.token
        self.port = self.srv.port

    @staticmethod
    def _mkimg(path):
        Image.new("RGB", (8, 8), (1, 2, 3)).save(path)

    # ------------------------------------------------------------- helpers

    def req(self, method, path, body=None, token=True, host=None, origin=None,
            ctype="application/json", raw=None, timeout=5, port=None, tok=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=timeout)
        headers = {}
        if host:
            headers["Host"] = host
        if token:
            headers[sv.TOKEN_HEADER] = tok or self.token
        if origin:
            headers["Origin"] = origin
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = ctype
            headers["Content-Length"] = str(len(data))
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r.status, payload

    # ------------------------------------------------------------- tests

    def test_identity_needs_no_token(self):
        status, body = self.req("GET", "/api/identity", token=False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["app"], "pjsk-wallpaper")
        # 身份应答不得泄露令牌（令牌只写在 runtime/server.json 里）
        self.assertNotIn(self.token.encode(), body)

    def test_state_requires_token(self):
        status, _ = self.req("GET", "/api/state", token=False)
        self.assertEqual(status, 403)
        status, body = self.req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["gallery"]["total"], 2)

    def test_state_long_poll_returns_immediately_on_change(self):
        """带 since 时挂起，切换一发生就立刻返回新快照。

        这是预览能与系统切换同步的机制：前端不再等固定周期轮询，
        而是被后端在状态变化的同一时刻唤醒。
        """
        status, body = self.req("GET", "/api/state")
        rev0 = json.loads(body)["rev"]
        box = {}

        def waiter():
            t0 = time.time()
            st, bd = self.req("GET", f"/api/state?since={rev0}&wait=8", timeout=15)
            box["status"] = st
            box["rev"] = json.loads(bd)["rev"]
            box["elapsed"] = time.time() - t0

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.4)                      # 确保长轮询已经挂起
        self.req("POST", "/api/wallpaper/desktop/next", body={})
        t.join(10)
        self.assertEqual(box.get("status"), 200, box)
        self.assertGreater(box["rev"], rev0)
        self.assertLess(box["elapsed"], 5, "变化后应立刻返回，而不是等满 wait")

    def test_state_long_poll_times_out_without_change(self):
        status, body = self.req("GET", "/api/state")
        rev0 = json.loads(body)["rev"]
        t0 = time.time()
        status, body = self.req("GET", f"/api/state?since={rev0}&wait=0.6", timeout=15)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(time.time() - t0, 0.5)
        self.assertEqual(json.loads(body)["rev"], rev0)

    def test_state_without_since_returns_at_once(self):
        t0 = time.time()
        status, body = self.req("GET", "/api/state?wait=8", timeout=15)
        self.assertEqual(status, 200)
        self.assertLess(time.time() - t0, 2, "不带 since 必须立即返回")
        self.assertIn("rev", json.loads(body))

    def test_state_bad_since_falls_back_to_immediate(self):
        status, body = self.req("GET", "/api/state?since=abc", timeout=15)
        self.assertEqual(status, 200)
        self.assertIn("rev", json.loads(body))

    def test_bad_host_rejected(self):
        status, _ = self.req("GET", "/api/state", host="evil.example.com:1234")
        self.assertEqual(status, 403)

    def test_cross_origin_rejected(self):
        status, _ = self.req("PATCH", "/api/config", body={"use_pjsk": True},
                             origin="https://evil.example.com")
        self.assertEqual(status, 403)
        status, _ = self.req("PATCH", "/api/config", body={"use_pjsk": True},
                             origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200)

    def test_patch_validation(self):
        status, body = self.req("PATCH", "/api/config",
                                body={"desktop": {"interval": 99999}})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])
        status, body = self.req("PATCH", "/api/config",
                                body={"desktop": {"interval": 5}})
        self.assertEqual(status, 200)
        status, body = self.req("GET", "/api/state")
        self.assertEqual(json.loads(body)["config"]["desktop"]["interval"], 5)

    def test_next_buttons(self):
        status, body = self.req("POST", "/api/wallpaper/desktop/next")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        status, body = self.req("POST", "/api/wallpaper/lock/next")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_image_requires_token(self):
        status, _ = self.req("GET", "/api/images/pjsk/card_1.png", token=False)
        self.assertEqual(status, 403)
        status, body = self.req(
            "GET", f"/api/images/pjsk/card_1.png?t={self.token}", token=False)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_image_traversal_blocked(self):
        for bad in ("pjsk/..%2Fconfig.json", "..%2Fsecret.png", "pjsk%2F..%2Fconfig.json"):
            status, _ = self.req("GET", f"/api/images/{bad}?t={self.token}", token=False)
            self.assertEqual(status, 404, f"应拒绝 {bad}")

    def test_gallery_paging_and_filter(self):
        status, body = self.req("GET", "/api/gallery?limit=1&source=custom")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["id"], "custom/mine.png")

    def test_gallery_bad_query_params_return_400(self):
        for q in ("offset=abc", "offset=-1", "offset=1.5",
                  "limit=abc", "limit=0", "limit=-5", "limit=100000",
                  "offset=99999999999999999999"):
            status, body = self.req("GET", f"/api/gallery?{q}")
            self.assertEqual(status, 400, f"{q} 应返回 400")
            data = json.loads(body)
            self.assertFalse(data["ok"])
            self.assertTrue(data["error"])
        # 非法参数不得中断服务：后续正常请求照常可用
        status, body = self.req("GET", "/api/gallery")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 2)
        status, _ = self.req("GET", "/api/state")
        self.assertEqual(status, 200)

    def test_gallery_empty_and_duplicate_params_are_deterministic(self):
        status, body = self.req("GET", "/api/gallery?offset=&limit=")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(data["limit"], 60)
        status, body = self.req("GET", "/api/gallery?limit=1&limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["limit"], 1)   # 重复参数取第一个

    def test_update_log_bad_lines_returns_400(self):
        for q in ("lines=abc", "lines=0", "lines=-3", "lines=100000"):
            status, body = self.req("GET", f"/api/gallery/update/log?{q}")
            self.assertEqual(status, 400, f"{q} 应返回 400")
            self.assertFalse(json.loads(body)["ok"])
        status, body = self.req("GET", "/api/gallery/update/log?lines=5")
        self.assertEqual(status, 200)
        self.assertIn("lines", json.loads(body))

    def test_upload_valid_and_invalid(self):
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), (9, 9, 9)).save(buf, format="PNG")
        valid = buf.getvalue()
        # 浏览器生成的 boundary 含大小写混合字符，必须原样保留才能匹配
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        parts = []
        for name, data in (("pic.png", valid), ("evil.txt", b"not an image")):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                .encode() + data + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        status, resp = self.req("POST", "/api/gallery/custom", raw=body,
                                ctype=f"multipart/form-data; boundary={boundary}")
        self.assertEqual(status, 200)
        data = json.loads(resp)
        self.assertEqual(data["imported"], ["pic.png"])
        self.assertEqual(len(data["failed"]), 1)
        self.assertTrue((self.custom / "pic.png").is_file())

    def test_static_index_token_injected(self):
        status, body = self.req("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.token.encode(), body)
        self.assertNotIn(b"__PJSK_TOKEN__", body)

    def test_window_show_needs_token(self):
        status, _ = self.req("POST", "/api/window/show", token=False)
        self.assertEqual(status, 403)

    def test_window_endpoints_without_ui_report_unsupported(self):
        """没有前端窗口的实例（无 UI / 测试环境）如实返回不支持，而不是假装成功。"""
        for path in ("/api/window/show", "/api/window/close"):
            status, body = self.req("POST", path, body={"action": "tray"})
            self.assertEqual(status, 501)
            self.assertFalse(json.loads(body)["ok"])

    def test_window_show_calls_launcher_hook(self):
        class FakeUi:
            def __init__(self):
                self.shown = []

            def show_window(self):
                self.shown.append(1)

            def close_window(self, action, remember):
                return {"ok": False, "error": "unused"}

        ui = FakeUi()
        srv = sv.WallpaperServer(self.rt, port=18960, ui=ui)
        srv.bind()
        srv.start_background()
        self.addCleanup(srv.shutdown)
        # 另一个实例的令牌无效：只有本实例的令牌能唤起本实例的窗口
        status, _ = self.req("POST", "/api/window/show", port=srv.port)
        self.assertEqual(status, 403)
        status, body = self.req("POST", "/api/window/show", port=srv.port,
                                tok=srv.token)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(ui.shown, [1])

    def test_window_close_forwards_decision_to_launcher(self):
        class FakeUi:
            def __init__(self):
                self.calls = []

            def show_window(self):
                pass

            def close_window(self, action, remember):
                self.calls.append((action, remember))
                if action not in ("exit", "tray"):
                    return {"ok": False, "error": "无效的关闭操作"}
                return {"ok": True, "action": action, "saved": remember}

        ui = FakeUi()
        srv = sv.WallpaperServer(self.rt, port=18970, ui=ui)
        srv.bind()
        srv.start_background()
        self.addCleanup(srv.shutdown)

        status, _ = self.req("POST", "/api/window/close", port=srv.port,
                             token=False, body={"action": "tray", "remember": True})
        self.assertEqual(status, 403)   # 仍需令牌
        self.assertEqual(ui.calls, [])

        status, body = self.req("POST", "/api/window/close", port=srv.port,
                                tok=srv.token, body={"action": "tray", "remember": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "action": "tray", "saved": True})
        self.assertEqual(ui.calls, [("tray", True)])

        status, body = self.req("POST", "/api/window/close", port=srv.port,
                                tok=srv.token, body={"action": "nope"})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_config_export_requires_token(self):
        status, _ = self.req("GET", "/api/config/export", token=False)
        self.assertEqual(status, 403)
        status, body = self.req("GET", "/api/config/export")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["app"], "pjsk-wallpaper")
        self.assertIn("config", data)
        self.assertNotIn("screen", data["config"])

    def test_config_import_raw_config(self):
        raw = {"desktop": {"enabled": True, "interval": 9},
               "lock": {"enabled": True, "interval": 45},
               "screen": [800, 600], "autostart": False,
               "update": {"count": 4, "min_rarity": 3, "chara": "kanade",
                          "unit": "", "region": "jp", "trained": True,
                          "interval_enabled": False, "interval": 360,
                          "last_run": 1.0}}
        status, body = self.req("POST", "/api/config/import", body=raw)
        self.assertEqual(status, 200, body)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIn("desktop", data["applied"])
        self.assertIn("update", data["applied"])
        self.assertEqual(data["state"]["config"]["desktop"]["interval"], 9)

    def test_config_import_invalid_rejected(self):
        for bad in ({"nope": 1}, {"desktop": {"interval": 0}},
                    {"desktop": {"interval": "x"}}, {"update": {"region": "zz"}}):
            status, body = self.req("POST", "/api/config/import", body=bad)
            self.assertEqual(status, 400, f"应拒绝 {bad}")
            self.assertFalse(json.loads(body)["ok"])

    def test_config_import_needs_token_and_same_origin(self):
        status, _ = self.req("POST", "/api/config/import",
                             body={"use_custom": True}, token=False)
        self.assertEqual(status, 403)
        status, _ = self.req("POST", "/api/config/import",
                             body={"use_custom": True},
                             origin="https://evil.example.com")
        self.assertEqual(status, 403)

    def test_update_status_endpoint(self):
        status, body = self.req("GET", "/api/gallery/update/status")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["running"])

    def test_open_folder_unknown(self):
        status, body = self.req("POST", "/api/system/open-folder", body={"which": "x"})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])


if __name__ == "__main__":
    unittest.main()
