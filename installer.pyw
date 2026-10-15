# -*- coding: utf-8 -*-
"""installer.pyw — 安装助手（向导式安装）

页面流程（对齐常见 Windows 安装器惯例，如 Inno Setup）：
欢迎 → 选择安装位置 → 正在安装（marquee 进度条 + 当前步骤文本，无日志）→ 完成
（快捷方式 / 开机自启 / 立即启动）。

由「安装助手.bat」引导运行；要求系统已安装 Python 3.10+。
所有运行产物（config/library/cache/logs）都在目标目录生成。
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import ttk

import win_startup

SRC = Path(__file__).resolve().parent
APP_NAME = "pjsk-wallpaper"
# 安装所需的最小文件集（不含 config/library/cache/logs 等运行产物）
INSTALL_FILES = [
    "launcher.pyw", "server.py", "runtime.py", "gallery_update.py",
    "core.py", "tray.py", "win_startup.py", "set_lockscreen.ps1",
    "start.bat", "requirements.txt", "icon.png", "icon.ico", "README.md",
]
# 需要整目录复制的静态资源
INSTALL_DIRS = ["app"]
# 旧版入口（Tkinter 主界面）在升级时移除，避免留下两套入口
LEGACY_FILES = ["wallpaper.pyw"]
DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA", str(SRC))) / "pjsk-wallpaper"
DIR_NAME = "pjsk-wallpaper"


def running_instance_in(dest: Path):
    """目标安装目录里是否仍有本应用的后台实例在运行；有则返回其信息，否则 None。

    判断依据只有两条——该实例自己发布的端口文件，以及 `/api/identity` 的身份应答：
    **不看前端窗口是否关闭**（关窗可能只是最小化到托盘，后台照旧在跑；只有选
    「退出程序」或托盘「退出」才结束进程），也不按进程名或 PID 猜测，避免误判、
    误杀无关进程。
    """
    try:
        info = json.loads((Path(dest) / "runtime" / "server.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("app") != APP_NAME or not info.get("port"):
        return None
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{int(info['port'])}/api/identity",
                timeout=2) as r:
            ident = json.loads(r.read().decode("utf-8"))
    except Exception:                      # noqa: BLE001 —— 连不上/不是 HTTP 即视为无实例
        return None
    if not isinstance(ident, dict) or ident.get("app") != APP_NAME:
        return None                        # 端口被别的服务占用：不当成本应用实例
    return info


def _dpi_aware():
    """Per-Monitor v2 → fallback 系统 DPI 感知，保证在高分屏下字体不模糊、不缩小。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()


_dpi_aware()


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("pjsk 壁纸 安装向导")
        self.resizable(False, False)

        self.var_dir = tk.StringVar(value=str(DEFAULT_DIR))
        self.var_shortcut = tk.BooleanVar(value=True)
        self.var_autostart = tk.BooleanVar(value=False)
        self.var_launch = tk.BooleanVar(value=True)
        self.installed_dir = None
        self._result_ok = True

        # ----- 主内容区（Inno Setup 风格：左侧白色页面区 + 底部灰色按钮条）-----
        self.page = tk.Frame(self, bg="white", padx=16, pady=16)
        self.page.pack(fill="both", expand=True)

        self.btn_back = ttk.Button(self, text="< 上一步", command=self._back, width=12)
        self.btn_next = ttk.Button(self, text="下一步 >", command=self._next, width=12)
        self.btn_cancel = ttk.Button(self, text="取消", command=self.destroy, width=12)
        btns = tk.Frame(self, bg="SystemButtonFace")
        btns.pack(fill="x", padx=12, pady=(6, 10))
        self.btn_back.pack(side="left")
        tk.Frame(btns, bg="SystemButtonFace", width=8).pack(side="left")
        self.btn_next.pack(side="right")
        self.btn_cancel.pack(side="right", padx=(8, 0))

        self.pages = {}
        for name, builder in (("welcome", self._build_welcome),
                              ("dir", self._build_dir),
                              ("installing", self._build_installing),
                              ("done", self._build_done)):
            f = tk.Frame(self.page, bg="white")
            builder(f)
            self.pages[name] = f
        self._cur = None
        self._show("welcome")

    # ---------- 小组件（白底统一风格） ----------

    def _title(self, f, text):
        tk.Label(f, text=text, bg="white", fg="black",
                 font=("Microsoft YaHei UI", 13, "bold"), anchor="w").pack(
            fill="x", pady=(0, 12))

    def _label(self, f, text):
        tk.Label(f, text=text, bg="white", fg="black",
                 font=("Microsoft YaHei UI", 9), justify="left", anchor="w",
                 wraplength=570).pack(fill="x", pady=2)

    # ---------- 页面 ----------

    def _build_welcome(self, f):
        # 欢迎页：标准做法 = 左侧竖色带 + 大标题 + 一句引导，其余留白
        band = tk.Frame(self.page, bg="#2d4eff", width=8)
        band.pack(side="left", fill="y", padx=(0, 20))
        body = tk.Frame(f, bg="white")
        body.pack(side="left", fill="both", expand=True)
        tk.Label(body, text="欢迎使用 pjsk 壁纸 安装向导", bg="white", fg="black",
                 font=("Microsoft YaHei UI", 16, "bold"), anchor="w").pack(
            fill="x", anchor="n", pady=(8, 16))
        self._label(body, "本向导将把 pjsk 壁纸 安装到您的电脑。")
        tk.Frame(body, bg="white", height=8).pack()
        self._label(body, "建议关闭其他应用程序后再继续。")
        tk.Frame(body, bg="white").pack(fill="both", expand=True)
        self._label(body, "单击「下一步」继续。")

    def _build_dir(self, f):
        self._title(f, "选择安装位置")
        tk.Frame(f, bg="white", height=4).pack()
        self._label(f, "安装位置：")
        row = tk.Frame(f, bg="white")
        row.pack(fill="x", pady=(4, 0))
        self.ent_dir = tk.Entry(row, textvariable=self.var_dir,
                                font=("Microsoft YaHei UI", 9))
        self.ent_dir.pack(side="left", fill="x", expand=True, ipady=3)
        tk.Button(row, text="浏览...", command=self._browse, width=8).pack(
            side="left", padx=(8, 0))

    def _build_installing(self, f):
        self._title(f, "正在安装")
        tk.Frame(f, bg="white", height=8).pack()
        self.lbl_step = tk.Label(f, text="正在复制文件...", bg="white", fg="black",
                                 font=("Microsoft YaHei UI", 9), anchor="w")
        self.lbl_step.pack(fill="x")
        tk.Frame(f, bg="white", height=8).pack()
        self.pb = ttk.Progressbar(f, mode="indeterminate", length=560)
        self.pb.pack(fill="x")

    def _build_done(self, f):
        self._title(f, "正在完成 pjsk 壁纸 安装向导")
        for text, var in (("创建桌面和开始菜单快捷方式", self.var_shortcut),
                          ("开机自动启动", self.var_autostart),
                          ("立即运行 pjsk 壁纸", self.var_launch)):
            tk.Checkbutton(f, text=text, variable=var, bg="white", fg="black",
                           activebackground="white",
                           font=("Microsoft YaHei UI", 9), anchor="w").pack(
                fill="x", pady=3, ipady=2)

    # ---------- 导航 ----------

    def _show(self, name):
        if self._cur:
            self.pages[self._cur].pack_forget()
        self._cur = name
        self.pages[name].pack(fill="both", expand=True)
        self.btn_back.config(state="normal" if name == "dir" else "disabled")
        if name == "welcome":
            self.btn_next.config(text="下一步 >", state="normal")
        elif name == "dir":
            self.btn_next.config(text="下一步 >", state="normal")
        elif name == "installing":
            self.btn_next.config(text="正在安装...", state="disabled")
            self.btn_back.config(state="disabled")
            self.btn_cancel.config(state="disabled")
        elif name == "done":
            self.btn_next.config(text="完成", state="normal")

        # 固定窗口大小：横向加长，路径列更宽
        self.update_idletasks()
        self.geometry("640x300")
        self.minsize(640, 300)

    def _back(self):
        if self._cur == "dir":
            self._show("welcome")

    def _next(self):
        if self._cur == "welcome":
            self._show("dir")
        elif self._cur == "dir":
            dest = self._resolve_dest(self.var_dir.get().strip())
            if dest is None:
                return
            if not self._confirm_not_running(dest):
                return
            self.var_dir.set(str(dest))
            self._show("installing")
            self.pb.start(12)
            threading.Thread(target=self._install, args=(dest,), daemon=True).start()
        elif self._cur == "done":
            self._finish()

    def _confirm_not_running(self, dest: Path) -> bool:
        """覆盖程序文件前确认目标目录里没有正在运行的本应用实例。

        关掉前端窗口不代表后台进程已退出（关窗可能只是最小化到托盘）；带病覆盖会让
        运行中的实例继续用旧模块而磁盘已换成新代码。检测到实例时明确提示并要求用户
        正常退出后重试，不做任何进程查杀。
        """
        info = running_instance_in(dest)
        if info is None:
            return True
        from tkinter import messagebox
        messagebox.showwarning(
            "安装向导",
            "检测到 pjsk 壁纸 正在后台运行"
            + (f"（进程 {info.get('pid')}）" if info.get("pid") else "") + "。\n\n"
            "请先结束它，再重新点击「下一步」：\n"
            "· 右键系统托盘图标，选择「退出」；或\n"
            "· 关闭前端窗口时选择「退出程序」。\n"
            "仅最小化或关闭窗口（若已记住「最小化到系统托盘」）不会结束后台进程。",
            parent=self)
        return False

    def _resolve_dest(self, raw: str):
        """目标目录 = 输入框内容，原样使用（浏览时的补全只是便利，用户修改后以用户为准）。"""
        if not raw:
            from tkinter import messagebox
            messagebox.showerror("安装向导", "请选择安装位置。", parent=self)
            return None
        p = Path(raw)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            from tkinter import messagebox
            messagebox.showerror("安装向导", f"无法创建目录：\n{p}\n\n{e}", parent=self)
            return None
        return p

    def _browse(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or str(DEFAULT_DIR),
                                    title="选择安装位置")
        if d:
            p = Path(d)
            if p.name.lower() != DIR_NAME:
                p = p / DIR_NAME
            self.var_dir.set(str(p))

    # ---------- 安装执行 ----------

    def _step(self, msg):
        self.after(0, lambda: self.lbl_step.config(text=msg))

    def _install(self, dest: Path):
        try:
            # 向导上一步的检查与真正复制之间有时间窗，覆盖前再确认一次实例
            # 已退出；检测到就中止，一个字节都不覆盖。
            if running_instance_in(dest) is not None:
                self._result_ok = False
                self.after(0, self._instance_reappeared)
                return

            self._step("正在复制文件...")
            backup = self._backup_program_files(dest)
            try:
                for name in INSTALL_FILES:
                    src = SRC / name
                    if src.exists():
                        shutil.copy2(src, dest / name)
                for name in INSTALL_DIRS:
                    src = SRC / name
                    if src.is_dir():
                        shutil.copytree(src, dest / name, dirs_exist_ok=True)
            except Exception as copy_err:
                # 复制中途失败：把备份的原程序文件放回去，尽量不留混合版本
                self._step("正在恢复原程序文件...")
                failed = self._restore_program_files(backup, dest)
                if failed:
                    raise RuntimeError(
                        f"复制失败（{copy_err}），且恢复原程序文件未完成："
                        f"{'；'.join(failed)}。程序文件可能处于新旧混合状态，"
                        "请重新运行安装向导修复。") from copy_err
                self._step("已恢复原程序文件。")
                raise
            finally:
                shutil.rmtree(backup, ignore_errors=True)

            # 升级：移除旧版 Tkinter 入口，避免残留两套启动方式
            for name in LEGACY_FILES:
                old = dest / name
                if old.exists():
                    self._step(f"正在清理旧版本文件 {name}...")
                    try:
                        old.unlink()
                    except OSError:
                        pass

            self._step("正在检查运行环境...")
            r = subprocess.run([sys.executable, "-c", "import PIL, webview"],
                               capture_output=True)
            if r.returncode != 0:
                self._step("正在安装依赖（首次需要联网，约十几秒）...")
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                                    str(dest / "requirements.txt")],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    self._result_ok = False
                    self.after(0, self._pip_failed)
        except Exception as e:
            self._result_ok = False
            msg = str(e)
            self.after(0, lambda: self._fatal(msg))
        self.after(0, self._install_done)

    def _backup_program_files(self, dest: Path) -> Path:
        """覆盖前把将被替换/删除的程序文件备份到临时目录，供失败时还原。

        只备份程序文件（INSTALL_FILES / INSTALL_DIRS / LEGACY_FILES），
        config.json、library/、cache/、logs/ 等用户数据不碰也不备份。
        """
        backup = Path(tempfile.mkdtemp(prefix="pjsk-install-backup-"))
        for name in INSTALL_FILES + LEGACY_FILES:
            src = dest / name
            if src.is_file():
                shutil.copy2(src, backup / name)
        for name in INSTALL_DIRS:
            src = dest / name
            if src.is_dir():
                shutil.copytree(src, backup / name, dirs_exist_ok=True)
        return backup

    def _restore_program_files(self, backup: Path, dest: Path) -> list:
        """把备份的程序文件放回目标目录；返回未能恢复的条目（不抛异常）。"""
        failed = []
        dest.mkdir(parents=True, exist_ok=True)
        for name in INSTALL_FILES + LEGACY_FILES:
            src = backup / name
            if not src.is_file():
                continue
            try:
                shutil.copy2(src, dest / name)
            except OSError as e:
                failed.append(f"{name}（{e}）")
        for name in INSTALL_DIRS:
            src = backup / name
            if not src.is_dir():
                continue
            try:
                shutil.copytree(src, dest / name, dirs_exist_ok=True)
            except OSError as e:
                failed.append(f"{name}/（{e}）")
        return failed

    def _instance_reappeared(self):
        """覆盖前复查发现实例又在运行：中止安装（未覆盖任何文件）。"""
        from tkinter import messagebox
        messagebox.showwarning(
            "安装向导",
            "安装过程中检测到 pjsk 壁纸 又重新启动了。\n\n"
            "已中止安装，没有覆盖任何程序文件。请结束程序后重新运行安装向导。",
            parent=self)
        self.destroy()


    def _pip_failed(self):
        from tkinter import messagebox
        messagebox.showwarning("安装向导",
                               "依赖（Pillow / pywebview）自动安装失败。\n"
                               "请稍后在命令行手动执行：\n"
                               "pip install -r requirements.txt", parent=self)

    def _fatal(self, msg):
        from tkinter import messagebox
        messagebox.showerror(
            "安装向导",
            f"安装失败：{msg}\n\n"
            "程序文件可能处于新旧混合的不完整状态，请重新运行安装向导完成修复。\n"
            "设置（config.json）、图库（library）、缓存（cache）与日志均不会被改动。",
            parent=self)
        self.destroy()

    def _install_done(self):
        self.pb.stop()
        self.installed_dir = self.var_dir.get()
        self._show("done")

    def _finish(self):
        dest = Path(self.installed_dir)
        errors = []
        if self.var_shortcut.get():
            try:
                self._create_shortcuts(dest)
            except Exception as e:
                errors.append(f"创建快捷方式失败：{e}")
        # 开机自启：勾选与否都要落地（未勾选=清除遗留项），保证与安装选择一致
        try:
            self._set_autostart(dest, self.var_autostart.get())
        except Exception as e:
            errors.append(f"设置开机自启失败：{e}")
        if self.var_launch.get():
            try:
                self._launch(dest)
            except Exception as e:
                errors.append(f"启动程序失败：{e}")
        if errors:
            from tkinter import messagebox
            messagebox.showwarning("安装向导", "\n".join(errors), parent=self)
        self.destroy()

    # ---------- 完成动作 ----------

    def _pythonw(self) -> str:
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        return str(pythonw if pythonw.exists() else exe)

    def _create_shortcuts(self, dest: Path):
        icon = dest / "icon.ico"
        script = ""
        for lnk in (Path(os.environ.get("USERPROFILE", ".")) / "Desktop" / "pjsk 壁纸.lnk",
                    Path(os.environ.get("APPDATA", ".")) /
                    r"Microsoft\Windows\Start Menu\Programs\pjsk 壁纸.lnk"):
            script += (
                f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); "
                f"$s.TargetPath = '{self._pythonw()}'; "
                f"$s.Arguments = '\"{dest / 'launcher.pyw'}\"'; "
                f"$s.WorkingDirectory = '{dest}'; "
                f"$s.IconLocation = '{icon},0'; "
                f"$s.Save(); "
            )
        ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-Command", script], capture_output=True, timeout=60,
                           creationflags=0x08000000)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or b"").decode("mbcs", "replace").strip())

    def _set_autostart(self, dest: Path, on: bool):
        """按向导勾选写入/清除自启项（与运行时共用 win_startup 实现）。"""
        win_startup.write(bool(on), f'"{self._pythonw()}" "{dest / "launcher.pyw"}"')

    def _launch(self, dest: Path):
        subprocess.Popen([self._pythonw(), str(dest / "launcher.pyw")],
                         cwd=str(dest), creationflags=0x08000000)


if __name__ == "__main__":
    Wizard().mainloop()
