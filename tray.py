# -*- coding: utf-8 -*-
"""tray.py — 纯 ctypes 系统托盘（Shell_NotifyIcon），无第三方依赖

在独立线程里跑一个隐藏的 Win32 消息窗口接收托盘消息，再通过回调把动作
（show / next_desk / next_lock / exit）派发给调用方。回调发生在托盘线程，
调用方需自行保证线程安全（launcher.pyw 直接调用运行时，无需 UI 线程中转）。
"""
import ctypes
import threading
from pathlib import Path
from ctypes import wintypes as wt

WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1          # 自定义：托盘消息
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2

NIN_BALLOONSHOW = 0x0402


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_char * 16),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


def _load_hicon(path: Path):
    """从 PNG 生成 HICON：用 ctypes 的 LoadImage 只支持 ico/bmp，
    所以借助 GDI+（系统自带）解码 PNG。"""
    gdid = ctypes.c_ulonglong(0)
    gdiplus = ctypes.WinDLL("gdiplus")
    class GdiplusStartupInput(ctypes.Structure):
        _fields_ = [("GdiplusVersion", wt.UINT), ("DebugEventCallback", ctypes.c_void_p),
                    ("SuppressBackgroundThread", wt.BOOL), ("SuppressExternalCodecs", wt.BOOL)]
    si = GdiplusStartupInput()
    si.GdiplusVersion = 1
    if gdiplus.GdiplusStartup(ctypes.byref(gdid), ctypes.byref(si), None) != 0:
        raise OSError("GdiplusStartup 失败")
    # GdipCreateBitmapFromFile
    gdiplus.GdipCreateBitmapFromFile.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    bmp = ctypes.c_void_p()
    if gdiplus.GdipCreateBitmapFromFile(str(path), ctypes.byref(bmp)) != 0:
        raise OSError(f"无法读取图标 {path}")
    gdiplus.GdipCreateHICONFromBitmap.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.HICON)]
    hicon = wt.HICON()
    if gdiplus.GdipCreateHICONFromBitmap(bmp, ctypes.byref(hicon)) != 0:
        raise OSError("GdipCreateHICONFromBitmap 失败")
    gdiplus.GdipDisposeImage(bmp)
    return hicon


class Tray:
    """在独立线程里跑隐藏消息窗口 + 托盘图标。动作通过回调（需线程安全）派发。

    callback(action) 收到: "show"（左键/双击）、"exit"（菜单退出）、"next_desk"、"next_lock"
    """

    def __init__(self, icon_png: Path, tooltip: str, callback):
        self._icon_png = icon_png
        self._tooltip = tooltip
        self._callback = callback
        self._thread = None
        self._started = threading.Event()
        self._stopped = False
        self._wnd = None
        self._nid = None

    # ---------- 外部 API（主线程调用） ----------

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started.wait(5)

    def stop(self):
        """删除托盘图标并结束消息循环。可重复调用。"""
        if self._stopped:
            return
        self._stopped = True
        if self._wnd:
            user32 = ctypes.windll.user32
            user32.PostMessageW(self._wnd, WM_CLOSE, 0, 0)

    # ---------- 线程内部 ----------

    def _run(self):
        user32 = ctypes.windll.user32
        # LPARAM 可能超过有符号范围，按无符号传递
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_size_t]
        user32.DefWindowProcW.restype = ctypes.c_longlong
        kernel32 = ctypes.windll.kernel32
        shell32 = ctypes.windll.shell32

        hinst = kernel32.GetModuleHandleW(None)
        cls_name = "pjsk_tray_wnd"
        wndproc = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)(self._wndproc)

        wc = WNDCLASSW()
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            return

        self._wnd = user32.CreateWindowExW(0, cls_name, "pjsk tray", 0,
                                           0, 0, 0, 0, None, None, hinst, None)
        if not self._wnd:
            return

        hicon = _load_hicon(self._icon_png)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._wnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = hicon
        nid.szTip = self._tooltip[:127]
        self._nid = nid
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            return
        self._started.set()

        msg = wt.MSG()
        while not self._stopped and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        user32.DestroyWindow(self._wnd)
        user32.UnregisterClassW(cls_name, hinst)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        user32 = ctypes.windll.user32
        if msg == WM_TRAYICON and wparam == 1:
            lo = lparam & 0xFFFF
            if lo in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._emit("show")
            elif lo == WM_RBUTTONUP:
                self._popup_menu(hwnd)
        elif msg == WM_COMMAND and wparam in (1, 2, 3, 4):  # 菜单命令
            self._emit(["show", "next_desk", "next_lock", "exit"][wparam - 1])
        elif msg == WM_CLOSE:
            # 让 GetMessageW 返回 0 退出循环，不实际销毁（在 _run 尾部统一处理）
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self, hwnd):
        user32 = ctypes.windll.user32
        SetForegroundWindow = user32.SetForegroundWindow
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, 1, "打开主窗口")
        user32.AppendMenuW(menu, 0, 2, "桌面下一张")
        user32.AppendMenuW(menu, 0, 3, "锁屏下一张")
        user32.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
        user32.AppendMenuW(menu, 0, 4, "退出")
        pos = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pos))
        SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(menu, 0x0182, pos.x, pos.y, 0, hwnd, None)  # TPM_RETURNCMD|TPM_RIGHTBUTTON
        if cmd:
            user32.PostMessageW(hwnd, WM_COMMAND, cmd, 0)
        user32.DestroyMenu(menu)

    def _emit(self, action: str):
        # 回调发生在托盘线程；tkinter 调用必须回到主线程
        try:
            self._callback(action)
        except Exception:
            pass


def _main_smoke():
    """自测：显示托盘 5 秒后退出。"""
    import time
    import core
    shown = []
    t = Tray(core.tray_icon_path(), "pjsk 壁纸（自测）", lambda a: shown.append(a))
    t.start()
    time.sleep(5)
    t.stop()
    print("tray shown 5s, events:", shown)


if __name__ == "__main__":
    _main_smoke()
