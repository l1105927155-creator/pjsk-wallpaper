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
            ctype="application/json", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if host:
            headers["Host"] = host
        if token:
            headers[sv.TOKEN_HEADER] = self.token
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

    def test_state_requires_token(self):
        status, _ = self.req("GET", "/api/state", token=False)
        self.assertEqual(status, 403)
        status, body = self.req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["gallery"]["total"], 2)

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
        res = json.loads(body)
        self.assertTrue(res["ok"])
        # 同一次响应必须带回最新 state，前端据此直接渲染（不依赖后续轮询）
        self.assertIn("state", res)
        self.assertEqual(res["state"]["desktop"]["current"]["name"], res["name"])
        self.assertEqual(res["state"]["desktop"]["last"],
                         res["state"]["desktop"]["last"])   # 已写入计时
        self.assertIsNotNone(res["state"]["desktop"]["last"])

        status, body = self.req("POST", "/api/wallpaper/lock/next")
        self.assertEqual(status, 200)
        res = json.loads(body)
        self.assertTrue(res["ok"])
        self.assertIn("state", res)
        self.assertEqual(res["state"]["lock"]["current"]["name"], res["name"])

    def test_next_returns_state_even_when_busy(self):
        """繁忙响应也要带 state，界面才能显示真实进度而不是停在旧状态。"""
        self.rt._desk_busy = True
        try:
            status, body = self.req("POST", "/api/wallpaper/desktop/next")
            res = json.loads(body)
            self.assertTrue(res["busy"])
            self.assertIn("state", res)
        finally:
            self.rt._desk_busy = False

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

    def test_upload_valid_and_invalid(self):
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), (9, 9, 9)).save(buf, format="PNG")
        valid = buf.getvalue()
        boundary = "----wbk"
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
