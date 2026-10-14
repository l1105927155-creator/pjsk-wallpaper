# -*- coding: utf-8 -*-
"""tools/preview_server.py — 本地开发/联调用：以「无副作用」运行时启动服务

用途：在不真正修改桌面/锁屏壁纸、也不动你的 config.json 的前提下，
      把 Web UI 跑起来做界面检查（配合 tools/ui_check.js）。

做法：把 `core.set_desktop / set_lockscreen / enable_desktop_fade` 换成空实现，
      并把配置读写指向临时目录，因此不会影响系统壁纸与用户数据。

用法：
    python tools/preview_server.py [端口]        # 默认 8799
    # 然后在另一个终端：
    node tools/ui_check.js http://127.0.0.1:8799/
"""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import runtime as runtime_mod  # noqa: E402
import server as server_mod  # noqa: E402

DEFAULT_PORT = 8799
SPI_DELAY = 0.8      # 模拟 SystemParametersInfoW 阻塞到 shell 开始淡入的耗时


def _fake_set_desktop(path):
    """真实的 SPI_SETDESKWALLPAPER（带淡入）会阻塞到 shell 开始过渡，
    这里用等长延时复现该阻塞，用来验证「预览没有等它返回才更新」。"""
    time.sleep(SPI_DELAY)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    tmp = Path(tempfile.mkdtemp(prefix="pjsk-preview-"))
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps({
        "screen": [2560, 1440],
        "desktop": {"enabled": True, "interval": 30},
        "lock": {"enabled": True, "interval": 60},
        "use_pjsk": True, "use_custom": False, "cap_pjsk": 20,
        "update": {"count": 3, "min_rarity": 3, "chara": "", "unit": "",
                   "region": "jp", "trained": True, "interval_enabled": False,
                   "interval": 360, "last_run": 0.0},
        "autostart": False,
    }, ensure_ascii=False, indent=2), "utf-8")

    mock.patch.object(core, "CONFIG_PATH", cfg_path).start()
    mock.patch.object(core, "set_desktop", _fake_set_desktop).start()
    mock.patch.object(core, "set_lockscreen", lambda p: None).start()
    mock.patch.object(core, "enable_desktop_fade", lambda: True).start()

    rt = runtime_mod.Runtime(tick_seconds=99999)
    rt._tick_once()                     # 触发一次（无副作用），让概览有预览图
    srv = server_mod.WallpaperServer(rt, port=port)
    srv.bind()
    print(f"预览服务: http://127.0.0.1:{srv.port}/", flush=True)
    print(f"配置(临时): {cfg_path}", flush=True)
    print(f"token: {srv.token}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        rt.stop()
        srv.shutdown()


if __name__ == "__main__":
    main()
