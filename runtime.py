# -*- coding: utf-8 -*-
"""runtime.py — 壁纸软件运行时：与 UI 无关的业务状态、调度与任务执行

设计目标（见 docs/architecture-refactor.md）：
- Python 后台进程是业务状态的唯一权威来源，Web UI 只展示/下发命令。
- 桌面 / 锁屏各自独立的计时器与随机序列，互不重置；一轮内不重复。
- 配置以内存一致快照为准，变更即校验并**原子**落盘；不再周期性回写。
- 图库更新互斥、异步、不阻塞调用方；状态与结果由后端记录。
- stop() 后不残留定时任务或更新子进程。

本模块不 import tkinter，可在无 GUI 的进程里长期运行。
"""
import json
import os
import random
import subprocess
import sys
import threading
import time
import traceback
from copy import deepcopy
from pathlib import Path

import core

TICK_SECONDS = 20          # 与旧版 tick 周期一致
LOG_TAIL_LINES = 200

# 可写字段的合法范围（与旧版 Spinbox 范围一致）
_INT_RANGES = {
    ("desktop", "interval"): (1, 1440),
    ("lock", "interval"): (1, 1440),
    ("update", "count"): (1, 10),
    ("update", "min_rarity"): (1, 4),
    ("update", "interval"): (1, 10080),
}
_CAP_RANGE = (1, 9999)
_REGIONS = {"jp", "cn", "en", "kr", "tw"}


def _no_window() -> int:
    return 0x08000000 if sys.platform == "win32" else 0


class Runtime:
    """常驻运行时。线程安全：公有方法均可从 HTTP 线程或调度线程调用。"""

    def __init__(self, tick_seconds: int = TICK_SECONDS):
        self._lock = threading.RLock()
        self._cfg = core.config_load()
        self._rng = random.Random()
        self._desk_rot = core.Rotator()
        self._lock_rot = core.Rotator()
        self._desk_last = None          # 上次「触发」桌面切换的时间（含手动）
        self._lock_last = None
        self._up_anchor = time.time()   # 间隔更新锚点（进程内）
        _up_last = self._cfg.get("update", {}).get("last_run") or 0.0
        self._update = {
            "running": False,
            "last_run": float(_up_last),
            "started_at": None,
            "finished_at": None,
            "ok": None,
            "message": "",
        }
        self._desk_busy = False
        self._lock_busy = False
        self._proc = None
        self._stop = threading.Event()
        self._thread = None
        self._tick = max(1, int(tick_seconds))
        self._status = "就绪"
        # 当前壁纸（受控 ID），用于概览预览；存于 runtime/state.json 以跨进程重启保留
        self._last_state_path = core.APP_DIR / "runtime" / "state.json"
        saved = self._load_last_state()
        self._desk_current = saved.get("desktop") or core.path_to_image_id(
            core.get_desktop_wallpaper())
        self._lock_current = saved.get("lock")

    def _load_last_state(self) -> dict:
        try:
            data = json.loads(self._last_state_path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_last_state(self):
        try:
            self._last_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._last_state_path.with_name("state.json.tmp")
            tmp.write_text(json.dumps({"desktop": self._desk_current,
                                       "lock": self._lock_current},
                                      ensure_ascii=False), "utf-8")
            os.replace(tmp, self._last_state_path)
        except OSError:
            pass

    # ------------------------------------------------------------------ 生命周期

    def start(self):
        """开启淡入、启动调度线程并立即跑一次 tick（与旧版启动即检查定时一致）。"""
        core.enable_desktop_fade()
        self._thread = threading.Thread(target=self._loop, name="runtime-sched",
                                        daemon=True)
        self._thread.start()

    def _loop(self):
        # 旧版启动后 0.5s 即做首次检查；这里启动即检查一次
        try:
            self._tick_once()
        except Exception:
            pass
        while not self._stop.wait(self._tick):
            try:
                self._tick_once()
            except Exception:
                self._set_status("错误: " + traceback.format_exc(limit=1))

    def stop(self):
        """停止调度并清理更新子进程；可重复调用。"""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.communicate(timeout=5)
            except Exception:
                pass

    # ------------------------------------------------------------------ 调度

    def _tick_once(self):
        now = time.time()
        with self._lock:
            d, l, up = self._cfg["desktop"], self._cfg["lock"], self._cfg["update"]
            due_desk = d.get("enabled") and (
                self._desk_last is None or now - self._desk_last >= int(d["interval"]) * 60)
            due_lock = l.get("enabled") and (
                self._lock_last is None or now - self._lock_last >= int(l["interval"]) * 60)
            if not up.get("interval_enabled"):
                self._up_anchor = now     # 关闭期间不累计，重新启用后从头计时
                due_up = False
            else:
                due_up = now - self._up_anchor >= int(up["interval"]) * 60
        if due_desk:
            self.switch("desktop", manual=False)
        if due_lock:
            self.switch("lock", manual=False)
        if due_up:
            with self._lock:
                self._up_anchor = time.time()
            self.start_update(manual=False)

    # ------------------------------------------------------------------ 状态

    def state(self) -> dict:
        """给前端/托盘的完整状态快照（不含任何绝对路径以外的可写能力）。"""
        now = time.time()
        with self._lock:
            cfg = deepcopy(self._cfg)
            desk = cfg["desktop"]
            lock = cfg["lock"]
            up = cfg["update"]
            desk_next = (self._desk_last + int(desk["interval"]) * 60
                         if self._desk_last else None)
            lock_next = (self._lock_last + int(lock["interval"]) * 60
                         if self._lock_last else None)
            up_next = (self._up_anchor + int(up["interval"]) * 60
                       if up.get("interval_enabled") else None)
        counts = core.library_counts()
        return {
            "server_time": now,
            "status": self._status,
            "config": cfg,
            "desktop": {
                "enabled": bool(cfg["desktop"]["enabled"]),
                "interval": int(cfg["desktop"]["interval"]),
                "last": self._desk_last,
                "next": desk_next,
                "busy": self._desk_busy,
                "current": self._public_current(self._desk_current),
            },
            "lock": {
                "enabled": bool(cfg["lock"]["enabled"]),
                "interval": int(cfg["lock"]["interval"]),
                "last": self._lock_last,
                "next": lock_next,
                "busy": self._lock_busy,
                "current": self._public_current(self._lock_current),
            },
            "gallery": counts,
            "update": {
                "running": self._update["running"],
                "last_run": self._update["last_run"],
                "started_at": self._update["started_at"],
                "finished_at": self._update["finished_at"],
                "ok": self._update["ok"],
                "message": self._update["message"],
                "next": up_next if up_next else None,
                "interval_enabled": bool(cfg["update"]["interval_enabled"]),
            },
            "system": core.system_info(cfg["screen"]),
        }

    def _set_status(self, msg: str):
        self._status = msg

    @staticmethod
    def _public_current(image_id):
        """把受控 ID 转成前端可用的对象；无法识别时返回 None（前端展示占位）。"""
        if not image_id or "/" not in image_id:
            return None
        source, name = image_id.split("/", 1)
        return {"id": image_id, "source": source, "name": name}

    # ------------------------------------------------------------------ 配置

    def apply_config(self, patch: dict):
        """校验并应用部分配置；返回 (ok, errors, warnings)。成功则原子落盘并生效。

        只允许已定义字段；类型/范围非法整体拒绝，不做部分写入。
        """
        errors, warnings = [], []
        if not isinstance(patch, dict):
            return False, ["请求体必须是对象"], []
        allowed_top = {"desktop", "lock", "use_pjsk", "use_custom", "cap_pjsk",
                       "screen", "update"}
        for key in patch:
            if key not in allowed_top:
                errors.append(f"{key} 不是可配置项")
        with self._lock:
            new = deepcopy(self._cfg)

        def _bool(path, value):
            if not isinstance(value, bool):
                errors.append(f"{path} 需要布尔值")
                return None
            return value

        def _int(path, value, rng):
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"{path} 需要整数")
                return None
            if not (rng[0] <= value <= rng[1]):
                errors.append(f"{path} 需在 {rng[0]}–{rng[1]} 之间")
                return None
            return value

        # desktop / lock
        for target, allowed in (("desktop", ("enabled", "interval")),
                                ("lock", ("enabled", "interval"))):
            if target in patch:
                sub = patch[target]
                if not isinstance(sub, dict):
                    errors.append(f"{target} 需要对象")
                    continue
                for key in sub:
                    if key not in allowed:
                        errors.append(f"{target}.{key} 不是可配置项")
                        continue
                    if key == "enabled":
                        v = _bool(f"{target}.enabled", sub[key])
                    else:
                        v = _int(f"{target}.interval", sub[key], _INT_RANGES[(target, key)])
                    if v is not None:
                        new[target][key] = v

        # 顶层布尔
        for key in ("use_pjsk", "use_custom"):
            if key in patch:
                v = _bool(key, patch[key])
                if v is not None:
                    new[key] = v

        if "cap_pjsk" in patch:
            v = _int("cap_pjsk", patch["cap_pjsk"], _CAP_RANGE)
            if v is not None:
                new["cap_pjsk"] = v

        if "screen" in patch:
            v = patch["screen"]
            if v is None:
                new["screen"] = None
            elif (isinstance(v, (list, tuple)) and len(v) == 2
                  and all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in v)):
                new["screen"] = [int(v[0]), int(v[1])]
            else:
                errors.append("screen 需要 [宽,高] 正整数或 null")

        # update
        if "update" in patch:
            if not isinstance(patch["update"], dict):
                errors.append("update 需要对象")
            else:
                up_allowed = {"count", "min_rarity", "interval", "interval_enabled",
                              "chara", "unit", "region", "trained"}
                for key, value in patch["update"].items():
                    if key not in up_allowed:
                        errors.append(f"update.{key} 不是可配置项")
                        continue
                    if key in ("count", "min_rarity", "interval"):
                        v = _int(f"update.{key}", value, _INT_RANGES[("update", key)])
                        if v is not None:
                            new["update"][key] = v
                    elif key == "interval_enabled":
                        v = _bool("update.interval_enabled", value)
                        if v is not None:
                            new["update"][key] = v
                    elif key == "trained":
                        v = _bool("update.trained", value)
                        if v is not None:
                            new["update"][key] = v
                    elif key == "region":
                        if not isinstance(value, str) or value not in _REGIONS:
                            errors.append("update.region 非法")
                        else:
                            new["update"]["region"] = value
                    else:  # chara / unit
                        if not isinstance(value, str):
                            errors.append(f"update.{key} 需要字符串")
                        elif len(value) > 40:
                            errors.append(f"update.{key} 过长")
                        else:
                            new["update"][key] = value.strip()

        if errors:
            return False, errors, []

        # 图库来源都关闭：保留旧版语义（允许保存，但明确提示切换会无图可用）
        if not new.get("use_pjsk") and not new.get("use_custom"):
            warnings.append("pjsk 与自定义图库均已关闭：壁纸切换将因图库为空而无动作")

        with self._lock:
            # interval_enabled 由关到开：重设锚点，避免立即触发
            was_on = bool(self._cfg["update"].get("interval_enabled"))
            now_on = bool(new["update"].get("interval_enabled"))
            if now_on and not was_on:
                self._up_anchor = time.time()
            self._cfg = new
        core.config_save(self._cfg)
        return True, [], warnings

    def config(self) -> dict:
        with self._lock:
            return deepcopy(self._cfg)

    # ------------------------------------------------------------------ 切换

    def switch(self, target: str, manual: bool) -> dict:
        """切换桌面/锁屏壁纸。target ∈ {'desktop','lock'}。

        返回 {ok, name, error, busy, empty}；busy=已有同名切换在进行。
        """
        if target not in ("desktop", "lock"):
            return {"ok": False, "busy": False, "empty": False, "name": None,
                    "error": "未知目标"}
        with self._lock:
            if target == "desktop":
                if self._desk_busy:
                    return {"ok": False, "busy": True, "empty": False,
                            "name": None, "error": "桌面切换进行中"}
                self._desk_busy = True
            else:
                if self._lock_busy:
                    return {"ok": False, "busy": True, "empty": False,
                            "name": None, "error": "锁屏切换进行中"}
                self._lock_busy = True
            cfg = deepcopy(self._cfg)
            rot = self._desk_rot if target == "desktop" else self._lock_rot

        try:
            files = core.scan_library(cfg)
            pick = rot.next(files, self._rng)
            if pick is None:
                self._set_status("图库为空：请先添加图片或更新 pjsk 图库")
                return {"ok": False, "busy": False, "empty": True,
                        "name": None, "error": "图库为空"}
            word = "桌面" if target == "desktop" else "锁屏"
            try:
                if target == "desktop":
                    core.enable_desktop_fade()
                    core.set_desktop(pick)
                else:
                    core.set_lockscreen(pick)
            except Exception as e:
                msg = f"{word}失败: {e}"
                self._set_status(msg)
                return {"ok": False, "busy": False, "empty": False,
                        "name": pick.name, "error": str(e)}
            # 手动与定时一致：触发即重置该目标的计时器
            cur = core.path_to_image_id(pick)
            with self._lock:
                if target == "desktop":
                    self._desk_last = time.time()
                    self._desk_current = cur
                else:
                    self._lock_last = time.time()
                    self._lock_current = cur
                self._save_last_state()
            self._set_status(f"{word} ✓ {pick.name}")
            return {"ok": True, "busy": False, "empty": False,
                    "name": pick.name, "error": ""}
        finally:
            with self._lock:
                if target == "desktop":
                    self._desk_busy = False
                else:
                    self._lock_busy = False

    # ------------------------------------------------------------------ 图库

    def gallery_page(self, offset: int = 0, limit: int = 60, source: str = "all",
                     keyword: str = "") -> dict:
        items = core.library_list(source=source, keyword=keyword)
        offset = max(0, int(offset))
        limit = min(200, max(1, int(limit)))
        page = items[offset:offset + limit]
        return {"total": len(items), "offset": offset, "limit": limit,
                "counts": core.library_counts(), "items": page}

    def resolve_image(self, image_id: str):
        return core.resolve_image_id(image_id)

    def add_images(self, paths):
        """把本地图片导入 custom 图库（服务端已完成路径与类型校验）。"""
        cfg = self.config()
        screen = cfg.get("screen") or core.detect_screen()
        ok, fails = 0, []
        for p in paths:
            try:
                core.import_image(Path(p), core.CUSTOM_DIR, screen)
                ok += 1
            except Exception as e:
                fails.append({"name": Path(p).name, "error": str(e)})
        if ok:
            self._set_status(f"已导入 {ok} 张" + (f"，失败 {len(fails)} 张" if fails else ""))
        return {"ok": ok, "failed": fails}

    # ------------------------------------------------------------------ 图库更新

    def start_update(self, manual: bool = False) -> dict:
        with self._lock:
            if self._update["running"]:
                return {"ok": False, "busy": True, "message": "更新已在进行中"}
            now = time.time()
            self._update.update(running=True, started_at=now, finished_at=None,
                                ok=None, message="更新中…")
            self._update["last_run"] = now
            self._up_anchor = now
            self._cfg["update"]["last_run"] = now
        core.config_save(self._cfg)
        threading.Thread(target=self._run_update, name="gallery-update",
                         daemon=True).start()
        return {"ok": True, "busy": False, "message": "已开始更新"}

    def _run_update(self):
        msg, ok = "更新失败", False
        try:
            cmd = [sys.executable, "-X", "utf8", str(core.APP_DIR / "gallery_update.py")]
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", creationflags=_no_window())
            try:
                out, err = self._proc.communicate(timeout=1200)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                out, err = self._proc.communicate()
            rc = self._proc.returncode
            tail = (out or "").strip().splitlines()
            if rc == 0:
                ok, msg = True, f"完成: {tail[-1] if tail else '无输出'}"
            else:
                msg = "失败: " + ((err or "").strip()[-200:] or f"退出码 {rc}")
        except Exception as e:
            msg = f"失败: {e}"
        finally:
            self._proc = None
        with self._lock:
            self._update.update(running=False, finished_at=time.time(),
                                ok=ok, message=msg)
        self._set_status(msg)

    def update_status(self) -> dict:
        with self._lock:
            return dict(self._update)

    def update_log_tail(self, lines: int = 80):
        return core.read_update_log(lines)

    # ------------------------------------------------------------------ 系统

    def set_autostart(self, on: bool) -> dict:
        try:
            core.set_autostart(bool(on))
            return {"ok": True, "autostart": core.autostart_on()}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def create_shortcuts(self) -> dict:
        try:
            results = core.create_shortcuts()
        except OSError as e:
            return {"ok": False, "error": str(e)}
        failed = [name for name, (ok, _m) in results if not ok]
        return {"ok": not failed, "created": [n for n, (ok, _m) in results if ok],
                "failed": failed}

    def open_folder(self, which: str) -> dict:
        d = {"pjsk": core.PJSK_DIR, "custom": core.CUSTOM_DIR}.get(which)
        if d is None:
            return {"ok": False, "error": "未知目录"}
        try:
            d.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer", str(d)])
            return {"ok": True}
        except OSError as e:
            return {"ok": False, "error": str(e)}
