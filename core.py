# -*- coding: utf-8 -*-
"""core.py — 壁纸轮换核心：配置、本地图库、桌面/锁屏壁纸设置、图片填充处理

不含任何网络逻辑。图库更新见 gallery_update.py。
"""
import ctypes
import json
import os
import random
import subprocess
import sys
import winreg
from pathlib import Path

from PIL import Image

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LIB_DIR = APP_DIR / "library"
PJSK_DIR = LIB_DIR / "pjsk"
CUSTOM_DIR = LIB_DIR / "custom"
CACHE_DIR = APP_DIR / "cache"
LOCKSCREEN_PS1 = APP_DIR / "set_lockscreen.ps1"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

def detect_screen() -> list:
    """检测主屏分辨率（先声明 DPI 感知，避免高缩放比例下取到缩放后的值）。"""
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        u = ctypes.windll.user32
        w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        if w > 0 and h > 0:
            return [w, h]
    except OSError:
        pass
    return [1920, 1080]


DEFAULT_CONFIG = {
    "screen": None,  # None = 自动检测主屏分辨率；可手动配置 [宽, 高] 覆盖
    "desktop": {"enabled": True, "interval": 30},
    "lock": {"enabled": True, "interval": 60},
    "use_pjsk": True,
    "use_custom": False,
    "cap_pjsk": 20,
    "update": {
        "count": 3,
        "region": "jp",
        "trained": True,
        "chara": "",
        "unit": "",
        "min_rarity": 3,
        "interval_enabled": False,
        "interval": 360,
        "last_run": 0.0,
    },
    "autostart": False,
}


# ---------- 配置 ----------

def config_load() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            cfg = {}
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    if merged.get("screen") is None:
        merged["screen"] = detect_screen()
    return merged


def config_save(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# ---------- 图片处理 ----------

def process_fill(img: Image.Image, dest: Path, screen):
    """等比缩放铺满 + 居中裁剪到屏幕分辨率，存为 PNG。"""
    tw, th = screen
    if img.mode != "RGB":
        img = img.convert("RGB")
    if (img.width, img.height) != (tw, th):
        scale = max(tw / img.width, th / img.height)
        w, h = round(img.width * scale), round(img.height * scale)
        img = img.resize((w, h), Image.LANCZOS)
        left, top = (w - tw) // 2, (h - th) // 2
        img = img.crop((left, top, left + tw, top + th))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG")
    return dest


def import_image(src: Path, dest_dir: Path, screen) -> Path:
    """把本地图片填充处理入库；重名自动加 -N 后缀。"""
    img = Image.open(src)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (src.stem + ".png")
    i = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem}-{i}.png"
        i += 1
    process_fill(img, dest, screen)
    return dest


# ---------- 图库扫描 ----------

def scan_library(cfg: dict):
    files = []
    for use, d in ((cfg.get("use_pjsk"), PJSK_DIR), (cfg.get("use_custom"), CUSTOM_DIR)):
        if use and d.is_dir():
            files += [p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=lambda p: p.name)


class Rotator:
    """轮换队列：一轮内不重复；图库内容变化时重建队列。"""

    def __init__(self):
        self.queue = []
        self._snapshot = None

    def next(self, files, rng: random.Random):
        if not files:
            return None
        snap = tuple(p.name for p in files)
        if snap != self._snapshot or not self.queue:
            self._snapshot = snap
            self.queue = list(files)
            rng.shuffle(self.queue)
        return self.queue.pop(0)


# ---------- 桌面壁纸 ----------

def set_desktop(path: Path):
    """SystemParametersInfo 设置桌面壁纸，无需管理员。壁纸方式固定为「填充」。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "WallpaperStyle", 0, winreg.REG_SZ, "10")  # 10 = 填充
            winreg.SetValueEx(k, "TileWallpaper", 0, winreg.REG_SZ, "0")
    except OSError:
        pass
    user32 = ctypes.windll.user32
    user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                             ctypes.c_wchar_p, ctypes.c_uint]
    ok = user32.SystemParametersInfoW(20, 0, str(path), 0x01 | 0x02)
    if not ok:
        raise OSError(f"SystemParametersInfo 失败: {path}")


# ---------- 桌面壁纸淡入（shell 行为，需主动开启） ----------

def enable_desktop_fade() -> bool:
    """开启 shell 换壁纸时的淡入过渡：向 Progman 发送 0x052C。

    该状态保存在 explorer 内，重启电脑或 explorer 后失效；发送后立即生效，
    因此每次切换壁纸前调用一次即可保证每次都带淡入。返回是否发送成功。
    """
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    hwnd = user32.FindWindowW("Progman", None)
    if not hwnd:
        return False
    user32.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_size_t, ctypes.c_size_t,
                                           ctypes.c_uint, ctypes.c_uint,
                                           ctypes.POINTER(ctypes.c_size_t)]
    res = ctypes.c_size_t()
    user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), 0x052C, 0, 0, 0x0002, 1000,
                               ctypes.byref(res))
    return True


# ---------- 锁屏壁纸（WinRT，经 Windows PowerShell 5.1 调用） ----------

def _powershell_exe() -> str:
    p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return p if os.path.exists(p) else "powershell.exe"


def set_lockscreen(path: Path, timeout: int = 30):
    """LockScreen.SetImageFileAsync 设置锁屏壁纸，无需管理员。"""
    cmd = [_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(LOCKSCREEN_PS1), "-Path", str(path)]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                       creationflags=0x08000000)  # CREATE_NO_WINDOW
    # 按系统 ANSI 代码页解码（PowerShell 5.1 输出 GBK），避免解码异常导致误判
    dec = lambda b: (b or b"").decode("mbcs", errors="replace").strip()
    r.stdout, r.stderr = dec(r.stdout), dec(r.stderr)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip() or "锁屏设置失败")


# ---------- 开机自启（HKCU Run） ----------

def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            val = f'"{sys.executable}" "{APP_DIR / "wallpaper.pyw"}"'
            winreg.SetValueEx(k, "pjsk-wallpaper", 0, winreg.REG_SZ, val)
        else:
            try:
                winreg.DeleteValue(k, "pjsk-wallpaper")
            except FileNotFoundError:
                pass


def autostart_on() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "pjsk-wallpaper")
            return True
    except OSError:
        return False


# ---------- 托盘图标资源 ----------

def tray_icon_path() -> Path:
    """生成图标文件（托盘用 PNG，快捷方式用 ICO）：蓝底白色音符。"""
    png, ico = APP_DIR / "icon.png", APP_DIR / "icon.ico"
    if not png.exists() or not ico.exists():
        from PIL import Image, ImageDraw
        # 按 256px 绘制再缩小，保证 ICO 各尺寸清晰
        s = 256
        img = Image.new("RGBA", (s, s), (45, 78, 255, 255))
        d = ImageDraw.Draw(img)
        u = s / 32  # 与 32px 网格对齐的单位
        d.rectangle((19 * u, 6 * u, 22 * u, 20 * u), fill=(255, 255, 255, 255))          # 竖杆
        d.polygon([(19 * u, 6 * u), (19 * u, 14 * u), (27 * u, 11 * u), (27 * u, 8 * u)],
                  fill=(255, 255, 255, 255))                                               # 旗
        d.ellipse((9 * u, 17 * u, 21 * u, 26 * u), fill=(255, 255, 255, 255))             # 符头
        d.rectangle((19 * u, 20 * u, 22 * u, 23 * u), fill=(255, 255, 255, 255))
        img.resize((32, 32), Image.LANCZOS).save(png, format="PNG")
        img.save(ico, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)])
    return png


# ---------- 开始菜单/桌面快捷方式 ----------

def _wscript_create_shortcut(lnk: Path, target: str, args: str, icon: Path):
    """通过 WScript.Shell COM 创建 .lnk，无需管理员。"""
    ps = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '\"{args}\"'; "
        f"$s.WorkingDirectory = '{APP_DIR}'; "
        f"$s.IconLocation = '{icon},0'; "
        f"$s.Save()"
    )
    r = subprocess.run([_powershell_exe(), "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30, creationflags=0x08000000)
    return r.returncode == 0 and lnk.exists(), r.stderr.decode("mbcs", "replace").strip()


def create_shortcuts() -> list:
    """在桌面与开始菜单创建快捷方式，返回 [(名称, (ok, msg)), ...]。"""
    tray_icon_path()  # 确保 icon.png / icon.ico 已生成
    icon = APP_DIR / "icon.ico"
    # .pyw 关联解释器：用 pythonw（无控制台）。优先当前解释器同目录的 pythonw
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    target = str(pythonw if pythonw.exists() else exe)
    script = APP_DIR / "wallpaper.pyw"
    results = []
    for lnk in (Path(os.environ.get("USERPROFILE", ".")) / "Desktop" / "pjsk 壁纸.lnk",
                Path(os.environ.get("APPDATA", ".")) / r"Microsoft\Windows\Start Menu\Programs"
                / "pjsk 壁纸.lnk"):
        try:
            results.append((str(lnk), _wscript_create_shortcut(lnk, target, script, icon)))
        except OSError as e:
            results.append((str(lnk), (False, str(e))))
    return results


def shortcuts_exist() -> bool:
    return (Path(os.environ.get("APPDATA", ".")) / r"Microsoft\Windows\Start Menu\Programs"
            / "pjsk 壁纸.lnk").exists()
