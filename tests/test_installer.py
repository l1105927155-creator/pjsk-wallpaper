# -*- coding: utf-8 -*-
"""安装向导自启逻辑测试。

回归背景：旧实现只在勾选时写注册表，未勾选时什么都不做，于是旧版本/上一次安装
遗留的 `pjsk-wallpaper` Run 值被运行时如实读回，界面显示「开机自启=开启」，
与安装时的选择不一致。现在安装器与运行时共用 `win_startup`，
并把「未勾选」当作「必须清除」。
"""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import win_startup as ws  # noqa: E402


def _stub_tkinter():
    """隔离运行时可能不带 tkinter；注入最小 stub 以便导入 installer.pyw。

    这里只为了让模块能加载、并执行与 UI 无关的流程（_finish 的自启落地），
    不会真正创建窗口。
    """
    if importlib.util.find_spec("tkinter") is not None:
        return

    class _Tk:                       # noqa: D401
        pass

    tk = types.ModuleType("tkinter")
    tk.Tk = _Tk
    for name in ("Frame", "Label", "Entry", "Button", "Checkbutton",
                 "StringVar", "BooleanVar", "IntVar"):
        setattr(tk, name, type(name, (), {}))
    ttk = types.ModuleType("tkinter.ttk")
    for name in ("Button", "Progressbar", "Frame", "Entry", "Spinbox"):
        setattr(ttk, name, type(name, (), {}))
    tk.ttk = ttk
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk


# 安装向导依赖 tkinter；缺失时用 stub 兜底
try:
    _stub_tkinter()
    _spec = importlib.util.spec_from_file_location("pjsk_installer",
                                                   ROOT / "installer.pyw")
    inst = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inst)
    HAS_INSTALLER = True
except Exception:                                   # noqa: BLE001
    inst = None
    HAS_INSTALLER = False


class _Var:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class WinStartupTest(unittest.TestCase):
    def _fake(self):
        fake = mock.MagicMock()
        key = mock.MagicMock()
        fake.OpenKey.return_value.__enter__.return_value = key
        return fake, key

    def test_write_on_sets_value(self):
        fake, _key = self._fake()
        with mock.patch.object(ws, "winreg", fake):
            ws.write(True, '"C:\\py\\pythonw.exe" "D:\\app\\launcher.pyw"')
        self.assertEqual(fake.OpenKey.call_args[0][1], ws.RUN_KEY)
        fake.DeleteValue.assert_not_called()
        args = fake.SetValueEx.call_args[0]
        self.assertEqual(args[1], ws.VALUE_NAME)
        self.assertIn("launcher.pyw", args[4])

    def test_write_off_deletes_value(self):
        """未勾选 = 主动清除，这是本次缺陷的关键修复点。"""
        fake, _key = self._fake()
        with mock.patch.object(ws, "winreg", fake):
            ws.write(False, "")
        fake.DeleteValue.assert_called_once_with(
            fake.OpenKey.return_value.__enter__.return_value, ws.VALUE_NAME)
        fake.SetValueEx.assert_not_called()

    def test_write_off_tolerates_missing_value(self):
        fake, _key = self._fake()
        fake.DeleteValue.side_effect = FileNotFoundError()
        with mock.patch.object(ws, "winreg", fake):
            ws.write(False, "")            # 不应抛异常

    def test_is_on_reads_registry_truth(self):
        fake, _key = self._fake()
        with mock.patch.object(ws, "winreg", fake):
            self.assertTrue(ws.is_on())
        fake.QueryValueEx.side_effect = FileNotFoundError()
        with mock.patch.object(ws, "winreg", fake):
            self.assertFalse(ws.is_on())

    def test_is_on_false_on_error(self):
        fake = mock.MagicMock()
        fake.OpenKey.side_effect = OSError()
        with mock.patch.object(ws, "winreg", fake):
            self.assertFalse(ws.is_on())

    def test_command_for_prefers_pythonw(self):
        with mock.patch.object(Path, "exists", lambda self: True):
            cmd = ws.command_for(r"D:\app", r"C:\py\python.exe")
        self.assertIn("pythonw.exe", cmd)
        self.assertIn("launcher.pyw", cmd)
        self.assertIn(str(Path(r"D:\app")), cmd)


class CoreDelegationTest(unittest.TestCase):
    def test_core_set_autostart_delegates(self):
        import core
        with mock.patch.object(core.win_startup, "write") as w, \
                mock.patch.object(core.win_startup, "command_for",
                                  return_value="CMD") as cf:
            core.set_autostart(False)
        cf.assert_called_once_with(core.APP_DIR)
        w.assert_called_once_with(False, "CMD")

    def test_core_autostart_on_delegates(self):
        import core
        with mock.patch.object(core.win_startup, "is_on", lambda: True):
            self.assertTrue(core.autostart_on())


class InstallerSourceInvariantTest(unittest.TestCase):
    """不依赖 tkinter 的源码级不变量：向导必须把勾选值无条件落地。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "installer.pyw").read_text("utf-8")

    def test_finish_applies_choice_unconditionally(self):
        self.assertNotIn("if self.var_autostart.get():", self.src,
                         "未勾选时必须也要走一次自启落地（清除遗留项）")
        self.assertRegex(
            self.src,
            r"self\._set_autostart\(dest,\s*self\.var_autostart\.get\(\)\)")

    def test_set_autostart_uses_shared_helper(self):
        self.assertIn("win_startup.write(", self.src)

    def test_no_local_autostart_key_definition(self):
        """自启键/值名必须来自 win_startup，避免运行时分叉。"""
        self.assertIsNone(
            re.search(r'RUN_KEY\s*=\s*r"Software\\\\Microsoft', self.src))
        self.assertIsNone(re.search(r'\.Settings\.CreateShortcut\(\'', self.src))

    def test_win_startup_ships_with_installer(self):
        block = re.search(r"INSTALL_FILES = \[(.*?)\]", self.src, re.S).group(1)
        self.assertIn("win_startup.py", block)
        self.assertTrue((ROOT / "win_startup.py").is_file())


@unittest.skipUnless(HAS_INSTALLER, "本环境无 tkinter，安装向导不可导入")
class WizardWiringTest(unittest.TestCase):
    def _fake_wizard(self, autostart):
        calls = []
        fake = types.SimpleNamespace(
            installed_dir="D:/install dir",
            var_shortcut=_Var(False),
            var_autostart=_Var(autostart),
            var_launch=_Var(False),
            destroy=lambda: None,
            _create_shortcuts=lambda d: None,
            _launch=lambda d: None,
            _set_autostart=lambda dest, on: calls.append((dest, on)),
        )
        return fake, calls

    def test_unchecked_calls_set_autostart_false(self):
        fake, calls = self._fake_wizard(False)
        inst.Wizard._finish(fake)
        self.assertEqual(calls, [(Path("D:/install dir"), False)])

    def test_checked_calls_set_autostart_true(self):
        fake, calls = self._fake_wizard(True)
        inst.Wizard._finish(fake)
        self.assertEqual(calls, [(Path("D:/install dir"), True)])

    def test_wizard_method_uses_shared_helper(self):
        with mock.patch.object(inst.win_startup, "write") as w:
            wiz = types.SimpleNamespace(_pythonw=lambda: "PYW")
            inst.Wizard._set_autostart(wiz, Path("D:/d"), False)
        w.assert_called_once()
        self.assertFalse(w.call_args[0][0])
        self.assertIn("launcher.pyw", w.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
