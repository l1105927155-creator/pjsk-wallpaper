# -*- coding: utf-8 -*-
"""wallpaper.pyw — 壁纸轮换 GUI 主程序（常驻，零网络）

pythonw 运行无控制台窗口。功能：
- 桌面/锁屏壁纸独立定时随机轮换（一轮内不重复）
- 手动下一张（桌面 / 锁屏）
- 图库目录勾选（pjsk / custom 混用）
- 自加图片入库（文件对话框）
- 图库立即更新 / 按间隔更新（调用 gallery_update.py 子进程）
- 开机自启

所有定时均为程序内定时：程序未运行时不会触发。
"""
import ctypes
import random
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import core
import tray


def _dpi_aware():
    """声明 Per-Monitor v2 DPI 感知：高分屏缩放下字体/控件不会被系统缩小。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


_dpi_aware()

rng = random.Random()
desk_rot = core.Rotator()
lock_rot = core.Rotator()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("pjsk 壁纸")
        self.minsize(420, 480)
        # 统一字体：跟随系统 UI 字体和 DPI 缩放
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.cfg = core.config_load()

        self.desk_timer = None
        self.lock_timer = None
        self.up_anchor = time.time()  # 间隔更新倒计时锚点（进程内，不从上次运行补算）
        self.up_last = float(self.cfg.get("update", {}).get("last_run", 0.0))
        self._updating = False
        self._desk_thread = None
        self._lock_thread = None
        self._switch_lock = threading.Lock()
        self._running = True

        self._build_ui()
        self._apply_config_to_ui()
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        # 开启 shell 换壁纸时的淡入过渡（状态在 explorer 内，切换前会再补发）
        core.enable_desktop_fade()

        self.after(500, self._tick)

        self.tray = tray.Tray(core.tray_icon_path(), "pjsk 壁纸", self._on_tray_action)
        self.tray.start()

    # ---------- UI ----------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, **pad)

        # 桌面壁纸
        d = ttk.LabelFrame(frm, text="桌面壁纸")
        d.pack(fill="x", **pad)
        self.var_desk_on = tk.BooleanVar()
        ttk.Checkbutton(d, text="启用定时轮换", variable=self.var_desk_on).grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Label(d, text="间隔(分)").grid(row=0, column=1, sticky="e")
        self.var_desk_iv = tk.IntVar(value=30)
        ttk.Spinbox(d, from_=1, to=1440, width=6, textvariable=self.var_desk_iv).grid(
            row=0, column=2, padx=6)
        ttk.Button(d, text="下一张", command=lambda: self._next_desk(manual=True)).grid(
            row=0, column=3, padx=6)
        self.lbl_desk_times = ttk.Label(d, text="")
        self.lbl_desk_times.grid(row=1, column=0, columnspan=5, sticky="w",
                                 padx=6, pady=(0, 4))

        # 锁屏壁纸
        l = ttk.LabelFrame(frm, text="锁屏壁纸")
        l.pack(fill="x", **pad)
        self.var_lock_on = tk.BooleanVar()
        ttk.Checkbutton(l, text="启用定时轮换", variable=self.var_lock_on).grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Label(l, text="间隔(分)").grid(row=0, column=1, sticky="e")
        self.var_lock_iv = tk.IntVar(value=60)
        ttk.Spinbox(l, from_=1, to=1440, width=6, textvariable=self.var_lock_iv).grid(
            row=0, column=2, padx=6)
        ttk.Button(l, text="下一张", command=lambda: self._next_lock(manual=True)).grid(
            row=0, column=3, padx=6)
        self.lbl_lock_times = ttk.Label(l, text="")
        self.lbl_lock_times.grid(row=1, column=0, columnspan=5, sticky="w",
                                 padx=6, pady=(0, 4))

        # 图库
        g = ttk.LabelFrame(frm, text="图库（切换只读本地图，无网络）")
        g.pack(fill="x", **pad)
        self.var_use_pjsk = tk.BooleanVar(value=True)
        self.var_use_custom = tk.BooleanVar(value=False)
        ttk.Checkbutton(g, text="pjsk 图库", variable=self.var_use_pjsk).grid(
            row=0, column=0, sticky="w", padx=6, pady=2)
        ttk.Checkbutton(g, text="自加图片 (custom)", variable=self.var_use_custom).grid(
            row=0, column=1, sticky="w")
        self.lbl_lib = ttk.Label(g, text="")
        self.lbl_lib.grid(row=1, column=0, columnspan=3, sticky="w", padx=6)

        bg = ttk.Frame(g)
        bg.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=4)
        ttk.Button(bg, text="添加图片...", command=self._add_images).pack(side="left", padx=3)
        ttk.Button(bg, text="打开 pjsk 目录", command=lambda: self._open_dir(core.PJSK_DIR)).pack(
            side="left", padx=3)
        ttk.Button(bg, text="打开 custom 目录", command=lambda: self._open_dir(core.CUSTOM_DIR)).pack(
            side="left", padx=3)

        # 图库更新
        u = ttk.LabelFrame(frm, text="图库更新（独立于壁纸轮换）")
        u.pack(fill="x", **pad)
        self.var_up_iv_on = tk.BooleanVar()
        ttk.Checkbutton(u, text="启用间隔更新", variable=self.var_up_iv_on).grid(
            row=0, column=0, sticky="w", padx=6, pady=2)
        ttk.Label(u, text="间隔(分)").grid(row=0, column=2, sticky="w")
        self.var_up_iv = tk.IntVar(value=360)
        ttk.Spinbox(u, from_=1, to=10080, width=7, textvariable=self.var_up_iv).grid(
            row=0, column=3, sticky="w")
        row = 1
        ttk.Label(u, text="每次张数").grid(row=row, column=0, sticky="w", padx=6)
        self.var_cnt = tk.IntVar(value=3)
        ttk.Spinbox(u, from_=1, to=10, width=5, textvariable=self.var_cnt).grid(
            row=row, column=1, sticky="w")
        ttk.Label(u, text="稀有度≥").grid(row=row, column=2, sticky="w")
        self.var_rar = tk.IntVar(value=3)
        ttk.Spinbox(u, from_=1, to=4, width=5, textvariable=self.var_rar).grid(
            row=row, column=3, sticky="w")
        ttk.Label(u, text="角色").grid(row=row + 1, column=0, sticky="w", padx=6, pady=2)
        self.var_chara = tk.StringVar()
        ttk.Entry(u, width=10, textvariable=self.var_chara).grid(row=row + 1, column=1, sticky="w")
        ttk.Label(u, text="组合").grid(row=row + 1, column=2, sticky="w")
        self.var_unit = tk.StringVar()
        ttk.Entry(u, width=10, textvariable=self.var_unit).grid(row=row + 1, column=3, sticky="w")
        ttk.Label(u, text="（角色/组合留空 = 全部；如 奏 / mmj / 25）").grid(
            row=row + 2, column=0, columnspan=4, sticky="w", padx=6)

        bu = ttk.Frame(u)
        bu.grid(row=row + 3, column=0, columnspan=4, sticky="w", padx=6, pady=4)
        self.btn_update = ttk.Button(bu, text="立即更新", command=self._update_now)
        self.btn_update.pack(side="left", padx=3)
        self.lbl_update = ttk.Label(bu, text="")
        self.lbl_update.pack(side="left", padx=6)
        self.lbl_update_times = ttk.Label(u, text="")
        self.lbl_update_times.grid(row=row + 4, column=0, columnspan=5, sticky="w",
                                   padx=6, pady=(0, 4))

        # 其他
        o = ttk.LabelFrame(frm, text="其他")
        o.pack(fill="x", **pad)
        self.var_autostart = tk.BooleanVar()
        ttk.Checkbutton(o, text="开机自启", variable=self.var_autostart,
                        command=self._toggle_autostart).grid(row=0, column=0, sticky="w",
                                                             padx=6, pady=4)
        ttk.Button(o, text="创建桌面快捷方式", command=self._create_shortcuts).grid(
            row=0, column=1, sticky="w", padx=6)
        ttk.Label(o, text="pjsk 上限(张)").grid(row=0, column=2, sticky="e")
        self.var_cap = tk.IntVar(value=20)
        ttk.Spinbox(o, from_=1, to=9999, width=6, textvariable=self.var_cap).grid(
            row=0, column=3, sticky="w", padx=6)
        ttk.Label(o, text="(FIFO 驱逐，下次更新生效)").grid(row=0, column=4, sticky="w")

        # 状态
        self.lbl_status = ttk.Label(frm, text="", anchor="w")
        self.lbl_status.pack(fill="x", **pad)

        for w in (d, l, g, u, o):
            w.columnconfigure(4, weight=1)

    def _apply_config_to_ui(self):
        d, l = self.cfg.get("desktop", {}), self.cfg.get("lock", {})
        self.var_desk_on.set(bool(d.get("enabled", True)))
        self.var_desk_iv.set(int(d.get("interval", 30)))
        self.var_lock_on.set(bool(l.get("enabled", True)))
        self.var_lock_iv.set(int(l.get("interval", 60)))
        self.var_use_pjsk.set(bool(self.cfg.get("use_pjsk", True)))
        self.var_use_custom.set(bool(self.cfg.get("use_custom", False)))
        up = self.cfg.get("update", {})
        self.var_cnt.set(int(up.get("count", 3)))
        self.var_rar.set(int(up.get("min_rarity", 3)))
        self.var_chara.set(str(up.get("chara", "")))
        self.var_unit.set(str(up.get("unit", "")))
        self.var_autostart.set(core.autostart_on())
        self.var_cap.set(int(self.cfg.get("cap_pjsk", 20)))
        self.var_up_iv_on.set(bool(up.get("interval_enabled", False)))
        self.var_up_iv.set(int(up.get("interval", 360)))
        self._refresh_lib_label()
        self._refresh_time_labels()

    # ---------- 状态采集 ----------

    def _collect_config(self) -> dict:
        self.cfg["desktop"] = {"enabled": self.var_desk_on.get(),
                               "interval": self.var_desk_iv.get()}
        self.cfg["lock"] = {"enabled": self.var_lock_on.get(),
                            "interval": self.var_lock_iv.get()}
        self.cfg["use_pjsk"] = self.var_use_pjsk.get()
        self.cfg["use_custom"] = self.var_use_custom.get()
        self.cfg["cap_pjsk"] = self.var_cap.get()
        self.cfg["update"] = {"count": self.var_cnt.get(), "min_rarity": self.var_rar.get(),
                              "chara": self.var_chara.get().strip(),
                              "unit": self.var_unit.get().strip(),
                              "region": self.cfg.get("update", {}).get("region", "jp"),
                              "trained": self.cfg.get("update", {}).get("trained", True),
                              "interval_enabled": self.var_up_iv_on.get(),
                              "interval": self.var_up_iv.get(),
                              "last_run": self.up_last}
        return self.cfg

    def _save(self):
        core.config_save(self._collect_config())

    def _refresh_lib_label(self):
        p = len(list(core.PJSK_DIR.glob("*.png"))) if core.PJSK_DIR.is_dir() else 0
        c = len(list(core.CUSTOM_DIR.glob("*.png"))) if core.CUSTOM_DIR.is_dir() else 0
        self.lbl_lib.config(text=f"当前可用: pjsk {p} 张 / custom {c} 张")

    def _status(self, msg: str):
        self.lbl_status.config(text=msg)

    # ---------- 轮换调度 ----------

    def _tick(self):
        """每 20 秒：保存配置 + 检查定时切换 + 刷新时间标签。"""
        if not self._running:
            return
        try:
            self._save()
            self._refresh_lib_label()
            self._check_desk()
            self._check_lock()
            self._check_update()
            self._refresh_time_labels()
        except Exception:
            self._status("错误: " + traceback.format_exc(limit=1))
        self.after(20000, self._tick)

    def _check_desk(self):
        if self.desk_timer is None or self._elapsed(self.desk_timer) >= self.var_desk_iv.get() * 60:
            if self.var_desk_on.get():
                self._next_desk(manual=False)

    def _check_lock(self):
        if self.lock_timer is None or self._elapsed(self.lock_timer) >= self.var_lock_iv.get() * 60:
            if self.var_lock_on.get():
                self._next_lock(manual=False)

    def _check_update(self):
        """间隔更新：纯程序内计时，程序未运行时不更新。"""
        if not self.var_up_iv_on.get():
            self.up_anchor = time.time()  # 关闭期间不累计，重新启用后从头计时
            return
        if self._elapsed(self.up_anchor) >= self.var_up_iv.get() * 60:
            self.up_anchor = time.time()
            self._start_update()

    @staticmethod
    def _elapsed(since: float) -> float:
        return time.time() - since

    # ---------- 时间标签 ----------

    @staticmethod
    def _fmt_ts(ts) -> str:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(ts)) if ts else "—"

    def _time_line(self, last, next_ts, enabled: bool, word: str) -> str:
        if not enabled:
            nxt = "—（未启用）"
        elif next_ts is None:
            nxt = "即将执行"
        else:
            nxt = self._fmt_ts(next_ts)
        return f"上一次{word}: {self._fmt_ts(last)}    下一次{word}: {nxt}"

    def _refresh_time_labels(self):
        self.lbl_desk_times.config(text=self._time_line(
            self.desk_timer,
            self.desk_timer + self.var_desk_iv.get() * 60 if self.desk_timer else None,
            self.var_desk_on.get(), "切换"))
        self.lbl_lock_times.config(text=self._time_line(
            self.lock_timer,
            self.lock_timer + self.var_lock_iv.get() * 60 if self.lock_timer else None,
            self.var_lock_on.get(), "切换"))
        self.lbl_update_times.config(text=self._time_line(
            self.up_last,
            self.up_anchor + self.var_up_iv.get() * 60,
            self.var_up_iv_on.get(), "更新"))

    # ---------- 切换 ----------

    def _next_desk(self, manual: bool):
        files = core.scan_library(self._collect_config())
        pick = desk_rot.next(files, rng)
        if pick is None:
            if manual:
                messagebox.showinfo("提示", "图库为空：请先添加图片或更新 pjsk 图库")
            return
        if self._desk_thread and self._desk_thread.is_alive():
            return
        t = threading.Thread(target=self._set_desk, args=(pick,), daemon=True)
        self._desk_thread = t
        self.desk_timer = time.time()
        t.start()
        self._status(f"桌面 → {pick.name}")

    def _next_lock(self, manual: bool):
        files = core.scan_library(self._collect_config())
        pick = lock_rot.next(files, rng)
        if pick is None:
            if manual:
                messagebox.showinfo("提示", "图库为空：请先添加图片或更新 pjsk 图库")
            return
        if self._lock_thread and self._lock_thread.is_alive():
            return
        t = threading.Thread(target=self._set_lock, args=(pick,), daemon=True)
        self._lock_thread = t
        self.lock_timer = time.time()
        t.start()
        self._status(f"锁屏 → {pick.name}")

    def _set_desk(self, path: Path):
        try:
            core.enable_desktop_fade()  # 每次切换前补发，保证这次切换带淡入
            core.set_desktop(path)
            self.after(0, lambda: self._status(f"桌面 ✓ {path.name}"))
        except Exception as e:
            msg = f"桌面失败: {e}"  # 先取值：except 块结束后 e 会被解绑
            self.after(0, lambda: self._status(msg))

    def _set_lock(self, path: Path):
        try:
            core.set_lockscreen(path)
            self.after(0, lambda: self._status(f"锁屏 ✓ {path.name}"))
        except Exception as e:
            msg = f"锁屏失败: {e}"
            self.after(0, lambda: self._status(msg))

    # ---------- 图库操作 ----------

    def _add_images(self):
        paths = filedialog.askopenfilenames(
            title="选择图片", filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not paths:
            return
        screen = self.cfg.get("screen") or core.detect_screen()
        ok, fail = 0, 0
        for p in paths:
            try:
                core.import_image(Path(p), core.CUSTOM_DIR, screen)
                ok += 1
            except Exception as e:
                fail += 1
                self._status(f"导入失败 {Path(p).name}: {e}")
        self._refresh_lib_label()
        self._status(f"已导入 {ok} 张" + (f"，失败 {fail} 张" if fail else ""))

    def _open_dir(self, d: Path):
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["explorer", str(d)])

    def _update_now(self):
        self._start_update(manual=True)

    def _start_update(self, manual: bool = False):
        """启动图库更新子进程；已在更新时直接返回。"""
        if self._updating:
            if manual:
                self._status("更新已在进行中")
            return
        self._updating = True
        self.up_last = time.time()
        self.up_anchor = self.up_last
        self._save()
        self.btn_update.config(state="disabled")
        self.lbl_update.config(text="更新中…")
        threading.Thread(target=self._run_update, daemon=True).start()

    def _run_update(self):
        try:
            r = subprocess.run(
                [sys.executable, "-X", "utf8",
                 str(core.APP_DIR / "gallery_update.py")],
                capture_output=True, text=True, timeout=1200)
            out = (r.stdout or "").strip().splitlines()
            tail = out[-1] if out else ""
            msg = f"完成: {tail}" if r.returncode == 0 else f"失败: {(r.stderr or '').strip()[-200:]}"
        except Exception as e:
            msg = f"失败: {e}"
        self.after(0, lambda: self._update_done(msg))

    def _update_done(self, msg: str):
        self._updating = False
        self.btn_update.config(state="normal")
        self.lbl_update.config(text=msg)
        self._refresh_lib_label()
        self._refresh_time_labels()

    # ---------- 系统开关 ----------

    def _toggle_autostart(self):
        try:
            core.set_autostart(self.var_autostart.get())
        except OSError as e:
            messagebox.showerror("错误", str(e))

    def _create_shortcuts(self):
        try:
            results = core.create_shortcuts()
        except OSError as e:
            messagebox.showerror("错误", str(e))
            return
        fails = [name for name, (ok, _m) in results if not ok]
        if fails:
            messagebox.showerror("快捷方式", "创建失败：\n" + "\n".join(fails))
        else:
            messagebox.showinfo("快捷方式", "已创建/更新：\n桌面 + 开始菜单")

    def _hide_to_tray(self):
        """点 X 或最小化 → 隐藏到托盘（程序常驻）。"""
        self.withdraw()
        self._save()

    def _on_tray_action(self, action: str):
        """托盘回调（独立线程）：把动作转回 Tk 主线程执行"""
        def run():
            if action == "show":
                self.deiconify()
            elif action == "next_desk":
                self._next_desk(manual=True)
            elif action == "next_lock":
                self._next_lock(manual=True)
            elif action == "exit":
                self._quit()
        try:
            self.after(0, run)
        except tk.TclError:
            pass

    def _quit(self):
        """真正退出：停轮换、收托盘图标、存配置、关窗口。"""
        self._running = False
        self._save()
        try:
            self.tray.stop()
            self.tray._thread.join(timeout=3)  # 等 Shell_NotifyIcon(NIM_DELETE) 完成，避免幽灵图标
        except Exception:
            pass
        self.destroy()


def _single_instance_or_show() -> bool:
    """单实例守卫：已有实例在跑时唤起它的主窗口并返回 False（本进程退出）。
    用命名互斥体判定 + 向已有实例的托盘窗口投递"打开主窗口"命令。"""
    import ctypes
    k32 = ctypes.windll.kernel32
    ERROR_ALREADY_EXISTS = 183
    k32.CreateMutexW.restype = ctypes.c_void_p
    mutex = k32.CreateMutexW(None, False, "pjsk-wallpaper-mutex")
    globals()["_mutex_hold"] = mutex  # 持有句柄防止 GC 释放互斥体
    if k32.GetLastError() == ERROR_ALREADY_EXISTS:
        u32 = ctypes.windll.user32
        tray = u32.FindWindowW("pjsk_tray_wnd", "pjsk tray")
        if tray:
            u32.PostMessageW(tray, 0x0111, 1, 0)  # WM_COMMAND → "show"
        return False
    return True


if __name__ == "__main__":
    if _single_instance_or_show():
        app = App()
        app.mainloop()
