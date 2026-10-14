# -*- coding: utf-8 -*-
"""server.py — 仅绑定本机的 HTTP 服务 + 静态 Web UI

安全边界（见 docs/architecture-refactor.md「本地安全边界」）：
- 只监听 127.0.0.1，不开放局域网/公网，无登录系统。
- 写接口要求自定义头 `X-Pjsk-Token`（同源页面才拿得到 token），并校验 Host/Origin，
  拒绝跨站表单与不可信 CORS —— 仅靠 loopback 不足以防止恶意网页访问。
- 图库以服务端受控 ID 映射到已知目录，阻断 `../`、符号链接越界与任意文件读取。
- 上传限制体积/数量/类型，非法文件拒绝；不提供任意路径/命令执行。
"""
import json
import mimetypes
import os
import posixpath
import secrets
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import core

APP_NAME = "pjsk-wallpaper"
APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "app"
RUNTIME_DIR = APP_DIR / "runtime"
PORT_FILE = RUNTIME_DIR / "server.json"

DEFAULT_PORT = 8788
PORT_TRIES = 20
MAX_JSON_BODY = 1 << 20          # 1 MiB
MAX_UPLOAD_BODY = 64 << 20       # 64 MiB
MAX_UPLOAD_FILES = 20
TOKEN_HEADER = "X-Pjsk-Token"


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "pjsk-wallpaper"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- 基础工具

    @property
    def rt(self):
        return self.server.runtime

    def log_message(self, fmt, *args):  # 静默；避免污染 stdout
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8",
              extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send(code, _json_bytes(obj))

    def _error(self, code, message):
        self._send_json({"ok": False, "error": message}, code)

    # ------------------------------------------------------------- 安全校验

    def _local_host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "::1", "[::1]")

    def _origin_ok(self) -> bool:
        """拒绝跨站：无 Origin（同源 fetch/直连）或 Origin 指向本机端口。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            o = urlparse(origin)
        except ValueError:
            return False
        return (o.scheme == "http" and (o.hostname in ("127.0.0.1", "localhost"))
                and o.port == self.server.server_port)

    def _token_ok(self) -> bool:
        return secrets.compare_digest(
            self.headers.get(TOKEN_HEADER, ""), self.server.token)

    def _query_token_ok(self, qs) -> bool:
        tok = (qs.get("t") or [""])[0]
        return secrets.compare_digest(tok, self.server.token)

    def _guard(self, need_token=True, need_origin=True) -> bool:
        if not self._local_host_ok():
            self._error(403, "Host 校验失败")
            return False
        if need_origin and not self._origin_ok():
            self._error(403, "跨站请求被拒绝")
            return False
        if need_token and not self._token_ok():
            self._error(403, "缺少或无效的请求令牌")
            return False
        return True

    # ------------------------------------------------------------- 路由

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path == "/api/identity":
            # 无令牌：仅供启动器校验实例身份（不含敏感信息）
            self._send_json({"app": APP_NAME, "port": self.server.server_port,
                             "pid": os.getpid()})
            return

        if path.startswith("/api/images/"):
            if not self._query_token_ok(qs):
                self._error(403, "缺少或无效的请求令牌")
                return
            self._serve_image(unquote(path[len("/api/images/"):]))
            return

        if path.startswith("/api/"):
            if not self._guard():
                return
            self._api_get(path, qs)
            return

        # 静态资源
        self._serve_static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_PATCH(self):
        self.do_POST()

    def do_POST(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            self._error(404, "not found")
            return
        if not self._guard():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "Content-Length 非法")
            return
        ctype = (self.headers.get("Content-Type") or "").lower()

        if u.path == "/api/gallery/custom":
            if "multipart/form-data" not in ctype:
                self._error(400, "需要 multipart/form-data 上传")
                return
            if length > MAX_UPLOAD_BODY:
                self._error(413, "上传体积超限")
                return
            self._upload(ctype, length)
            return

        if length > MAX_JSON_BODY:
            self._error(413, "请求体过大")
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._error(400, "请求体不是合法 JSON")
            return
        if not isinstance(payload, dict):
            self._error(400, "请求体必须是对象")
            return
        self._api_post(u.path, payload)

    # ------------------------------------------------------------- API 实现

    def _api_get(self, path, qs):
        if path == "/api/state":
            self._send_json(self.rt.state())
        elif path == "/api/gallery":
            self._send_json(self.rt.gallery_page(
                offset=int((qs.get("offset") or ["0"])[0] or 0),
                limit=int((qs.get("limit") or ["60"])[0] or 60),
                source=(qs.get("source") or ["all"])[0],
                keyword=(qs.get("keyword") or [""])[0]))
        elif path == "/api/gallery/update/status":
            self._send_json(self.rt.update_status())
        elif path == "/api/gallery/update/log":
            self._send_json({"lines": self.rt.update_log_tail(
                int((qs.get("lines") or ["80"])[0] or 80))})
        elif path == "/api/system":
            self._send_json(core.system_info(self.rt.config().get("screen")))
        else:
            self._error(404, "not found")

    def _api_post(self, path, payload):
        if path == "/api/config":
            ok, errors, warnings = self.rt.apply_config(payload)
            self._send_json({"ok": ok, "errors": errors, "warnings": warnings,
                             "state": self.rt.state() if ok else None},
                            200 if ok else 400)
        elif path == "/api/wallpaper/desktop/next":
            self._send_json(self.rt.switch("desktop", manual=True))
        elif path == "/api/wallpaper/lock/next":
            self._send_json(self.rt.switch("lock", manual=True))
        elif path == "/api/gallery/update":
            res = self.rt.start_update(manual=True)
            self._send_json(res, 200 if res["ok"] else 409)
        elif path == "/api/system/autostart":
            self._send_json(self.rt.set_autostart(bool(payload.get("on"))))
        elif path == "/api/system/open-folder":
            res = self.rt.open_folder(str(payload.get("which") or ""))
            self._send_json(res, 200 if res["ok"] else 400)
        elif path == "/api/system/create-shortcuts":
            res = self.rt.create_shortcuts()
            self._send_json(res, 200 if res["ok"] else 500)
        else:
            self._error(404, "not found")

    # ------------------------------------------------------------- 图片 / 上传

    def _serve_image(self, image_id):
        p = self.rt.resolve_image(image_id)
        if p is None:
            self._error(404, "图片不存在")
            return
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        try:
            data = p.read_bytes()
        except OSError:
            self._error(500, "读取失败")
            return
        self._send(200, data, ctype)

    def _upload(self, ctype, length):
        boundary = None
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip('"')
        if not boundary:
            self._error(400, "缺少 multipart boundary")
            return
        body = self.rfile.read(length) if length else b""
        files = _parse_multipart(body, boundary.encode("latin-1"))
        if not files:
            self._error(400, "未收到文件")
            return
        if len(files) > MAX_UPLOAD_FILES:
            self._error(413, "单次上传文件数超限")
            return

        cfg = self.rt.config()
        screen = cfg.get("screen") or core.detect_screen()
        tmp_dir = RUNTIME_DIR / ("upload_" + secrets.token_hex(8))
        imported, failed = [], []
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            for name, data in files:
                name = os.path.basename(name or "").strip()
                if not name:
                    continue
                ext = Path(name).suffix.lower()
                if ext not in core.IMAGE_EXTS:
                    failed.append({"name": name, "error": "不支持的图片格式"})
                    continue
                if not _valid_image(data):
                    failed.append({"name": name, "error": "文件不是有效图片"})
                    continue
                # 以原始文件名落临时文件，import_image 按其 stem 命名入库
                safe = Path(name).name
                tmp = tmp_dir / safe
                try:
                    tmp.write_bytes(data)
                    dest = core.import_image(tmp, core.CUSTOM_DIR, screen)
                    imported.append(dest.name)
                except Exception as e:
                    failed.append({"name": name, "error": str(e)})
        finally:
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
        self.rt._set_status(f"已导入 {len(imported)} 张"
                            + (f"，失败 {len(failed)} 张" if failed else ""))
        self._send_json({"ok": len(imported), "imported": imported, "failed": failed},
                        200 if imported else 400)

    # ------------------------------------------------------------- 静态资源

    def _serve_static(self, path):
        rel = unquote(path).lstrip("/") or "index.html"
        if posixpath.normpath("/" + rel).startswith("/.."):
            self._error(404, "not found")
            return
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._error(404, "not found")
            return
        if not target.is_file():
            self._error(404, "not found")
            return
        data = target.read_bytes()
        if target.name == "index.html":
            data = data.replace(b"__PJSK_TOKEN__", self.server.token.encode("ascii"))
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)


def _parse_multipart(body: bytes, boundary: bytes):
    """极简 multipart/form-data 解析：返回 [(filename, bytes), ...]。"""
    out = []
    delim = b"--" + boundary
    for chunk in body.split(delim):
        if not chunk or chunk in (b"--", b"--\r\n", b"\r\n"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        if chunk.startswith(b"--"):
            break
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        filename = ""
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition:"):
                for token in line.split(b";"):
                    token = token.strip()
                    if token.startswith(b"filename="):
                        filename = token[len(b"filename="):].strip(b'"').decode(
                            "utf-8", "replace")
        out.append((filename, data))
    return out


def _valid_image(data: bytes) -> bool:
    if not data:
        return False
    try:
        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            im.verify()
        return True
    except Exception:
        return False


class WallpaperServer:
    """HTTP 服务 + 运行时生命周期管理。"""

    def __init__(self, runtime, port: int = DEFAULT_PORT, host="127.0.0.1"):
        self.runtime = runtime
        self.host = host
        self.token = secrets.token_hex(16)
        self.httpd = None
        self.port = None
        self._port_start = port

    def bind(self) -> int:
        last = None
        for i in range(PORT_TRIES):
            port = self._port_start + i
            try:
                httpd = ThreadingHTTPServer((self.host, port), _Handler)
            except OSError as e:
                last = e
                continue
            httpd.daemon_threads = True
            httpd.runtime = self.runtime
            httpd.token = self.token
            self.httpd = httpd
            self.port = port
            return port
        raise OSError(f"无法绑定 {self._port_start}-{self._port_start + PORT_TRIES - 1} "
                      f"端口: {last}")

    def serve_forever(self):
        self.httpd.serve_forever(poll_interval=0.5)

    def start_background(self):
        threading.Thread(target=self.serve_forever, name="http", daemon=True).start()

    def write_port_file(self):
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(json.dumps(
            {"app": APP_NAME, "port": self.port, "pid": os.getpid(),
             "started": time.time()}), "utf-8")

    def clear_port_file(self):
        try:
            PORT_FILE.unlink()
        except OSError:
            pass

    def shutdown(self):
        self.clear_port_file()
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            try:
                self.httpd.server_close()
            except Exception:
                pass


def running_instance():
    """读取端口文件并确认本机存在本应用实例；返回其 dict 或 None。"""
    try:
        info = json.loads(PORT_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if info.get("app") != APP_NAME or not info.get("port"):
        return None
    try:
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=1.5):
            pass
    except OSError:
        return None
    return info


if __name__ == "__main__":  # 便于单机调试：python server.py
    import runtime as runtime_mod
    rt = runtime_mod.Runtime()
    rt.start()
    srv = WallpaperServer(rt)
    srv.bind()
    srv.write_port_file()
    print(f"http://127.0.0.1:{srv.port}/  (token {srv.token})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        rt.stop()
        srv.shutdown()
