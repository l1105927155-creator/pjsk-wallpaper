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
import re
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
EXPORT_VERSION = 1         # 设置文件格式版本
STATE_WAIT_MAX = 25        # /api/state 长轮询最长挂起秒数
PREVIEW_WIDTH = 1024       # 概览预览的缩略图宽度（与前端 imgUrl(id, 1024) 一致）
UPDATE_TIMEOUT_SECONDS = 1200   # 图库更新子进程的最长运行时间（超时则终止）
# 图库更新子进程的进度标记，格式由 gallery_update.progress() 产出：
#   [进度] 已下载/总计
_UPDATE_PROGRESS_RE = re.compile(r"\[进度\]\s*(\d+)/(\d+)")

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

# ---- 设置导入：可迁移项与本机项 ----
# 可迁移（会真正应用）的顶层字段
_IMPORTABLE_TOP = {"desktop", "lock", "use_pjsk", "use_custom", "cap_pjsk", "update"}
# 本机相关：识别但忽略（不随配置迁移）
_IGNORED_TOP = {"screen": "本机屏幕分辨率", "autostart": "本机开机自启",
                "close_action": "本机关闭行为"}
_IMPORTABLE_UPDATE = {"count", "min_rarity", "interval", "interval_enabled",
                      "chara", "unit", "region", "trained"}
_IGNORED_UPDATE = {"last_run": "上次更新时间（运行时状态）"}

# 前端窗口关闭行为（close_action）的合法取值；None = 每次询问
CLOSE_ACTIONS = ("exit", "tray")


def _no_window() -> int:
    return 0x08000000 if sys.platform == "win32" else 0


class Runtime:
    """常驻运行时。线程安全：公有方法均可从 HTTP 线程或调度线程调用。"""

    def __init__(self, tick_seconds: int = TICK_SECONDS):
        self._lock = threading.RLock()
        # 状态修订号：任何会影响前端的变更都 +1 并唤醒等待中的长轮询，
        # 使前端能在变更发生的同一时刻刷新，而不必等下一次定时轮询。
        self._cv = threading.Condition(self._lock)
        self._rev = 0
        self._pending = {"desktop": None, "lock": None}   # 正在应用的目标（受控 ID）
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
            "progress": None,       # {"done": 已下载, "total": 总计}，仅更新中有效
        }
        self._desk_busy = False
        self._lock_busy = False
        self._proc = None
        self._close_prompt = 0          # 待处理的关闭确认请求身份（0 = 无）
        self._close_prompt_seq = 0      # 关闭请求身份号的发号器
        self._stop = threading.Event()
        self._thread = None
        self._tick = max(1, int(tick_seconds))
        self._status = "就绪"
        # 当前壁纸（受控 ID），用于概览预览；存于 runtime/state.json 以跨进程重启保留
        self._last_state_path = core.APP_DIR / "runtime" / "state.json"
        saved = self._load_last_state()
        # 系统当前壁纸可能读不到（纯色壁纸，或原壁纸文件已被删除/换盘符），
        # get_desktop_wallpaper() 此时如实返回 None：视为「当前壁纸未知」，不做推断。
        wallpaper = core.get_desktop_wallpaper()
        self._desk_current = saved.get("desktop") or (
            core.path_to_image_id(wallpaper) if wallpaper else None)
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

    def _bump(self):
        """标记状态已变化并唤醒等待中的长轮询。调用方需持有 self._lock。"""
        self._rev += 1
        self._cv.notify_all()

    def revision(self) -> int:
        with self._lock:
            return self._rev

    def wait_state(self, since: int, timeout: float = STATE_WAIT_MAX) -> dict:
        """长轮询：直到状态修订号不等于 `since`，或挂起超时，才返回最新快照。

        前端带着自己已知的修订号来等，切换/配置/更新任一发生即被唤醒，
        因此预览能在系统壁纸开始过渡的同一时刻更新。
        """
        try:
            timeout = max(0.0, min(float(timeout), float(STATE_WAIT_MAX)))
        except (TypeError, ValueError):
            timeout = float(STATE_WAIT_MAX)
        with self._cv:
            if int(since) == self._rev:
                self._cv.wait(timeout)
        return self.state()

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
            rev = self._rev
            pend = dict(self._pending)
            # 关闭确认是粘性待办：读取（无论来自谁）不清除，由前端带身份号确认后清除。
            # /api/state 的读取者不止内嵌页面一个，若读到就清零，无关请求或
            # 并发竞态会抢先消费掉，真正要弹框的页面就收不到。
            close_prompt = self._close_prompt
        counts = core.library_counts()
        return {
            "server_time": now,
            "rev": rev,
            "status": self._status,
            "close_prompt": close_prompt,
            "config": cfg,
            "desktop": {
                "enabled": bool(cfg["desktop"]["enabled"]),
                "interval": int(cfg["desktop"]["interval"]),
                "last": self._desk_last,
                "next": desk_next,
                "busy": self._desk_busy,
                "current": self._public_current(self._desk_current),
                "pending": pend["desktop"],
            },
            "lock": {
                "enabled": bool(cfg["lock"]["enabled"]),
                "interval": int(cfg["lock"]["interval"]),
                "last": self._lock_last,
                "next": lock_next,
                "busy": self._lock_busy,
                "current": self._public_current(self._lock_current),
                "pending": pend["lock"],
            },
            "gallery": counts,
            "update": {
                "running": self._update["running"],
                "last_run": self._update["last_run"],
                "started_at": self._update["started_at"],
                "finished_at": self._update["finished_at"],
                "ok": self._update["ok"],
                "message": self._update["message"],
                "progress": dict(self._update["progress"] or {}) or None,
                "next": up_next if up_next else None,
                "interval_enabled": bool(cfg["update"]["interval_enabled"]),
            },
            "system": core.system_info(cfg["screen"]),
        }

    def _set_status(self, msg: str):
        with self._lock:
            self._status = msg
            self._bump()

    def request_close_prompt(self) -> int:
        """请前端弹出「关闭窗口」确认框，返回本次请求的身份号。

        走既有的状态推送通道：置身份号并 +1 修订号，挂起中的长轮询立刻返回，
        前端在收到状态时弹框。身份号保持待处理直到前端带着**同一个号**确认
        （acknowledge_close_prompt），因此：
        - 页面刷新、长轮询重连或并发状态请求都不会让它无提示丢失；
        - 迟到的旧确认（用户取消后再点关闭产生的上一轮请求）不会误清掉新一轮。
        前端弹框是幂等的，重复收到只会聚焦已有弹框。
        """
        with self._lock:
            self._close_prompt_seq += 1
            self._close_prompt = self._close_prompt_seq
            self._bump()
            return self._close_prompt

    def acknowledge_close_prompt(self, prompt_id) -> bool:
        """前端确认已弹出指定身份的关闭确认框。

        只清除与当前待处理请求身份一致的确认；重复、过期或身份错误的确认
        一律无害忽略（返回 False），不会误清掉后来的新请求。
        """
        try:
            pid = int(prompt_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            if self._close_prompt == 0 or pid != self._close_prompt:
                return False
            self._close_prompt = 0
            return True

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

        并发一致性：复制、校验、合并、落盘、发布都在同一把锁内完成，两个并发的
        PATCH 不会互相覆盖（丢失更新），也不会竞争同一临时文件；先落盘成功再发布
        内存状态，落盘失败时内存保持原值并如实返回失败，不以「已保存」欺骗客户端。
        """
        errors, warnings = [], []
        if not isinstance(patch, dict):
            return False, ["请求体必须是对象"], []
        allowed_top = {"desktop", "lock", "use_pjsk", "use_custom", "cap_pjsk",
                       "screen", "update"}
        for key in patch:
            if key not in allowed_top:
                errors.append(f"{key} 不是可配置项")

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

        with self._lock:
            new = deepcopy(self._cfg)

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
                            v = _int(f"{target}.interval", sub[key],
                                     _INT_RANGES[(target, key)])
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
                      and all(isinstance(x, int) and not isinstance(x, bool) and x > 0
                              for x in v)):
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
                            v = _int(f"update.{key}", value,
                                     _INT_RANGES[("update", key)])
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

            # 先落盘，成功后才发布内存状态；失败则整体保持原状并报告失败
            try:
                core.config_save(new)
            except OSError as e:
                return False, [f"保存配置失败：{e}"], []

            # interval_enabled 由关到开：重设锚点，避免立即触发
            was_on = bool(self._cfg["update"].get("interval_enabled"))
            now_on = bool(new["update"].get("interval_enabled"))
            if now_on and not was_on:
                self._up_anchor = time.time()
            self._cfg = new
            self._bump()
        return True, [], warnings

    def config(self) -> dict:
        with self._lock:
            return deepcopy(self._cfg)

    def set_close_action(self, value):
        """保存前端窗口的关闭行为，返回 (ok, errors)。

        `value`：None（每次询问）/ "exit"（退出程序）/ "tray"（最小化到系统托盘）。
        与 apply_config 相同的「先落盘、成功后再发布」语义：落盘失败时内存保持原值。
        """
        if value is not None and value not in CLOSE_ACTIONS:
            return False, ["close_action 需要 null、exit 或 tray"]
        with self._lock:
            if self._cfg.get("close_action") == value:
                return True, []
            new = deepcopy(self._cfg)
            new["close_action"] = value
            try:
                core.config_save(new)
            except OSError as e:
                return False, [f"保存配置失败：{e}"]
            self._cfg = new
            self._bump()
        return True, []

    # ------------------------------------------------------------------ 设置导入 / 导出

    def export_config(self) -> dict:
        """导出设置快照。

        - 只包含**可在机器之间迁移**的设置；`screen`、`autostart`、`close_action`、
          `update.last_run` 属本机/运行时状态，放在 `machine`/不含在 `config` 内。
        - 不含任何图片，也不含绝对路径。
        """
        with self._lock:
            cfg = deepcopy(self._cfg)
        screen = cfg.pop("screen", None)
        cfg.pop("autostart", None)
        cfg.pop("close_action", None)
        cfg.get("update", {}).pop("last_run", None)
        return {
            "app": core.APP_NAME,
            "kind": "settings",
            "version": EXPORT_VERSION,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "machine": {"screen": screen},
            "config": cfg,
        }

    def import_config(self, payload):
        """导入设置文件。支持两种输入：

        1. 本程序导出的信封：`{"app": "pjsk-wallpaper", "config": {...}}`
        2. **原始 config.json 内容**（直接导入用户手里的配置文件）

        采用「覆盖文件中出现的项」语义（文件中没有的项保持不变）。
        严格校验：未知项、类型/范围错误整体拒绝，不做部分写入。
        返回 (ok, errors, warnings, applied)。
        """
        errors, warnings, applied = [], [], []
        if not isinstance(payload, dict):
            return False, ["文件内容必须是 JSON 对象"], [], []

        if payload.get("app") == core.APP_NAME and isinstance(payload.get("config"), dict):
            body = payload["config"]
            ver = payload.get("version")
            if isinstance(ver, int) and ver > EXPORT_VERSION:
                warnings.append(f"文件版本 {ver} 高于当前支持的 {EXPORT_VERSION}，"
                                f"仅导入可识别项")
        else:
            body = payload

        patch = {}
        for key, value in body.items():
            if key in _IGNORED_TOP:
                warnings.append(f"已忽略 {key}（{_IGNORED_TOP[key]}，不随配置迁移）")
                continue
            if key not in _IMPORTABLE_TOP:
                errors.append(f"未知配置项: {key}")
                continue

            if key in ("desktop", "lock"):
                if not isinstance(value, dict):
                    errors.append(f"{key} 需要对象")
                    continue
                sub = {}
                for k, v in value.items():
                    if k in ("enabled", "interval"):
                        sub[k] = v
                    else:
                        errors.append(f"未知配置项: {key}.{k}")
                if sub:
                    patch[key] = sub
                    applied.append(key)
            elif key == "update":
                if not isinstance(value, dict):
                    errors.append("update 需要对象")
                    continue
                sub = {}
                for k, v in value.items():
                    if k in _IGNORED_UPDATE:
                        warnings.append(f"已忽略 update.{k}（{_IGNORED_UPDATE[k]}）")
                    elif k in _IMPORTABLE_UPDATE:
                        sub[k] = v
                    else:
                        errors.append(f"未知配置项: update.{k}")
                if sub:
                    patch["update"] = sub
                    applied.append("update")
            else:
                patch[key] = value
                applied.append(key)

        if errors:
            return False, errors, warnings, []

        ok, verrors, vwarns = self.apply_config(patch)
        if not ok:
            return False, verrors, warnings, []
        self._set_status(f"已导入设置：{len(applied)} 项")
        return True, [], warnings + vwarns, applied

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
            # 先广播「正在应用」再写系统壁纸。
            # core.set_desktop 里的 SystemParametersInfo 会阻塞到 shell 开始淡入，
            # 若等它返回再通知前端，预览必然整体滞后一整段过渡——这正是
            # 「背景过渡结束后预览才切换」的根因。这里让前端与系统同时开始过渡。
            cur = core.path_to_image_id(pick)
            # 预热预览缩略图：某张图第一次上屏时后端要现算 1024px 缩略图（约 0.2s），
            # 若等前端来取才生成，预览就会晚于桌面起步。先备好再广播，
            # 两者真正同一时刻开始过渡；已缓存时这次调用近乎零成本。
            try:
                core.thumbnail(pick, PREVIEW_WIDTH)
            except Exception:
                pass
            with self._lock:
                self._pending[target] = self._public_current(cur)
                self._bump()
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
                self._pending[target] = None
                if target == "desktop":
                    self._desk_busy = False
                else:
                    self._lock_busy = False
                self._bump()

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
                                ok=None, message="更新中…", progress=None)
            self._update["last_run"] = now
            self._up_anchor = now
            # 与 apply_config 共用同一把锁完成「改内存 + 落盘」，
            # 避免并发的配置写入用旧快照回写掉这里的 last_run。
            self._cfg["update"]["last_run"] = now
            try:
                core.config_save(self._cfg)
            except OSError:
                # 落盘失败不阻断更新本身：last_run 属运行时状态，进程内仍然生效
                pass
            self._bump()
        threading.Thread(target=self._run_update, name="gallery-update",
                         daemon=True).start()
        return {"ok": True, "busy": False, "message": "已开始更新"}

    def _run_update(self):
        """执行图库更新子进程，并把它的进度**逐行**读出来实时发布。

        子进程每下完一张就打印一行进度标记；这里随读随发布并 +1 修订号，
        挂起中的长轮询立刻返回，界面不必等整个更新结束才知道进展。
        子进程的 stdout/stderr 合并到同一管道：既避免只读一条管道时另一条被写满卡死，
        也让失败时能把最后一行原因（含未捕获异常的回溯尾行）如实报出来。
        """
        msg, ok, timed_out = "更新失败", False, False
        try:
            cmd = [sys.executable, "-X", "utf8", str(core.APP_DIR / "gallery_update.py")]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", creationflags=_no_window())
            self._proc = proc

            def _kill_on_timeout():
                nonlocal timed_out
                timed_out = True
                proc.kill()

            killer = threading.Timer(UPDATE_TIMEOUT_SECONDS, _kill_on_timeout)
            killer.daemon = True
            killer.start()
            tail = []
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        tail.append(line)
                    self._note_progress(line)
                rc = proc.wait()
            finally:
                killer.cancel()

            if timed_out:
                msg = f"失败: 更新超时（超过 {UPDATE_TIMEOUT_SECONDS // 60} 分钟）"
            elif rc == 0:
                ok, msg = True, f"完成: {tail[-1] if tail else '无输出'}"
            else:
                reason = tail[-1] if tail else f"退出码 {rc}"
                for prefix in ("更新失败: ", "更新失败："):
                    if reason.startswith(prefix):
                        reason = reason[len(prefix):]
                        break
                msg = "失败: " + reason[-200:]
        except Exception as e:
            msg = f"失败: {e}"
        finally:
            self._proc = None
        with self._lock:
            self._update.update(running=False, finished_at=time.time(),
                                ok=ok, message=msg, progress=None)
            self._bump()
        self._set_status(msg)

    def _note_progress(self, line: str):
        """解析子进程的一行输出；是进度标记就立刻发布出去。"""
        m = _UPDATE_PROGRESS_RE.search(line or "")
        if not m:
            return
        prog = {"done": int(m.group(1)), "total": int(m.group(2))}
        with self._lock:
            if self._update.get("progress") == prog:
                return
            self._update["progress"] = prog
            self._bump()

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
