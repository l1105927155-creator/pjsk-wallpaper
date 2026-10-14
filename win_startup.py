# -*- coding: utf-8 -*-
"""win_startup.py — 开机自启项（HKCU Run）的唯一读写入口

安装器与应用共用同一份实现，避免「安装时写入的值名/路径」与「运行时读取的键」
出现分叉——那正是本次「未勾选自启却显示已开启」问题的成因。

自启项：
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
    name  = pjsk-wallpaper
    value = "<pythonw>" "<安装目录>\\launcher.pyw"

本模块不依赖 PIL / tkinter，可被安装向导在最早期安全导入。
"""
import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "pjsk-wallpaper"
LAUNCHER = "launcher.pyw"


def command_for(app_dir, python_exe=None) -> str:
    """生成自启命令行：优先使用无控制台的 pythonw.exe。"""
    exe = Path(python_exe or sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else exe
    return f'"{target}" "{Path(app_dir) / LAUNCHER}"'


def write(on: bool, command: str = "") -> None:
    """写入或清除自启项。

    `on=False` 时**主动删除**已有值：这样安装向导的勾选就是权威状态，
    不会残留旧版本/上一次安装留下的自启项。
    """
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(k, VALUE_NAME)
            except FileNotFoundError:
                pass


def is_on() -> bool:
    """当前是否已启用自启（如实读注册表）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE_NAME)
        return True
    except OSError:
        return False


def read_command() -> str:
    """返回已写入的命令行；不存在时返回空串。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return str(winreg.QueryValueEx(k, VALUE_NAME)[0])
    except OSError:
        return ""
