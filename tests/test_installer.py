# -*- coding: utf-8 -*-
"""安装向导自启逻辑测试。

回归背景：旧实现只在勾选时写注册表，未勾选时什么都不做，于是旧版本/上一次安装
遗留的 `pjsk-wallpaper` Run 值被运行时如实读回，界面显示「开机自启=开启」，
与安装时的选择不一致。现在安装器与运行时共用 `win_startup`，
并把「未勾选」当作「必须清除」。
"""
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    def set(self, value):
        self._value = value


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


def _serve_identity(app_name: str):
    """起一个只回应 /api/identity 的本地服务，模拟"端口上的某个程序"。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):        # 静默
            pass

        def do_GET(self):
            body = json.dumps({"app": app_name, "port": self.server.server_port,
                               "pid": 4321}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class _RecordingThread:
    """替换 threading.Thread：立即在 .start() 里同步执行目标，便于断言。"""

    def __init__(self, target=None, args=(), **kwargs):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@unittest.skipUnless(HAS_INSTALLER, "本环境无 tkinter，安装向导不可导入")
class RunningInstanceTest(unittest.TestCase):
    """升级前必须识别出运行中的本应用实例，且不得把无关服务误判成本应用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self._tmp.name)
        (self.dest / "runtime").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _serve(self, app_name):
        srv = _serve_identity(app_name)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def _port_file(self, port=None, app="pjsk-wallpaper"):
        info = {"pid": 4321}
        if app is not None:
            info["app"] = app
        if port is not None:
            info["port"] = port
        (self.dest / "runtime" / "server.json").write_text(
            json.dumps(info), "utf-8")

    def test_detects_live_instance(self):
        srv = self._serve("pjsk-wallpaper")
        self._port_file(srv.server_port)
        info = inst.running_instance_in(self.dest)
        self.assertIsNotNone(info)
        self.assertEqual(int(info["port"]), srv.server_port)

    def test_none_without_port_file(self):
        self.assertIsNone(inst.running_instance_in(self.dest))

    def test_none_when_port_file_corrupt(self):
        (self.dest / "runtime" / "server.json").write_text("{ not json", "utf-8")
        self.assertIsNone(inst.running_instance_in(self.dest))

    def test_none_when_port_not_listening(self):
        self._port_file(9)                      # 保留端口，未监听
        self.assertIsNone(inst.running_instance_in(self.dest))

    def test_none_when_file_app_name_is_foreign(self):
        srv = self._serve("pjsk-wallpaper")
        self._port_file(srv.server_port, app="some-other-app")
        self.assertIsNone(inst.running_instance_in(self.dest))

    def test_none_when_port_belongs_to_other_service(self):
        """端口被无关服务占用时必须判定为「无实例」，避免误杀无关进程。"""
        srv = self._serve("someone-else")
        self._port_file(srv.server_port)
        self.assertIsNone(inst.running_instance_in(self.dest))

    def test_detects_real_server_instance(self):
        """与真实服务对接：server.json 的文件格式与 /api/identity 应答必须能被识别。"""
        import core
        import runtime as rt_mod
        import server as sv_mod

        rt = rt_mod.Runtime(tick_seconds=9999)
        port_file = self.dest / "runtime" / "server.json"
        with mock.patch.object(core, "CONFIG_PATH", self.dest / "config.json"), \
                mock.patch.object(sv_mod, "RUNTIME_DIR", self.dest / "runtime"), \
                mock.patch.object(sv_mod, "PORT_FILE", port_file):
            srv = sv_mod.WallpaperServer(rt, port=18970 + (id(self) % 50))
            srv.bind()
            srv.write_port_file()
            self.addCleanup(srv.shutdown)
            srv.start_background()
            try:
                info = inst.running_instance_in(self.dest)
            finally:
                srv.shutdown()
        self.assertIsNotNone(info, "真实运行中的实例必须能被识别")
        self.assertEqual(int(info["port"]), srv.port)


@unittest.skipUnless(HAS_INSTALLER, "本环境无 tkinter，安装向导不可导入")
class UpgradeGateTest(unittest.TestCase):
    """实例在跑时不得开始覆盖程序文件，并要给出明确提示。"""

    def _fake_wizard(self, dest):
        self.shown, self.installed = [], []
        fake = types.SimpleNamespace(
            _cur="dir",
            var_dir=_Var(str(dest)),
            _resolve_dest=lambda raw: Path(raw),
            _show=self.shown.append,
            pb=types.SimpleNamespace(start=lambda ms: None),
            _install=self.installed.append,
            after=lambda *args: None,
        )
        # 沿用真实的「确认没有实例在跑」实现，只替换其中的检测来源
        fake._confirm_not_running = lambda d: inst.Wizard._confirm_not_running(fake, d)
        return fake

    def test_blocks_when_instance_running(self):
        from tkinter import messagebox
        fake = self._fake_wizard("D:/fake-install")
        with mock.patch.object(inst, "running_instance_in",
                               lambda d: {"app": "pjsk-wallpaper", "port": 1,
                                          "pid": 4321}), \
                mock.patch.object(messagebox, "showwarning") as warn, \
                mock.patch.object(inst.threading, "Thread", _RecordingThread):
            inst.Wizard._next(fake)
        warn.assert_called_once()
        self.assertIn("后台运行", warn.call_args[0][1])
        self.assertIn("托盘", warn.call_args[0][1])
        self.assertNotIn("installing", self.shown, "不得进入安装页")
        self.assertEqual(self.installed, [], "不得开始复制程序文件")

    def test_proceeds_when_no_instance(self):
        fake = self._fake_wizard("D:/fake-install")
        with mock.patch.object(inst, "running_instance_in", lambda d: None), \
                mock.patch.object(inst.threading, "Thread", _RecordingThread):
            inst.Wizard._next(fake)
        self.assertIn("installing", self.shown)
        self.assertEqual(self.installed, [Path("D:/fake-install")])


@unittest.skipUnless(HAS_INSTALLER, "本环境无 tkinter，安装向导不可导入")
class InstallPreservesUserDataTest(unittest.TestCase):
    """覆盖程序文件时不得触碰用户数据。"""

    def test_upgrade_keeps_config_library_cache_logs(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src, dest = td / "src", td / "dest"
            (src / "app").mkdir(parents=True)
            (src / "app" / "index.html").write_text("new-index", "utf-8")
            for name in ("launcher.pyw", "core.py"):
                (src / name).write_text("new-" + name, "utf-8")
            dest.mkdir()
            (dest / "config.json").write_text('{"user": 1}', "utf-8")
            (dest / "library" / "custom").mkdir(parents=True)
            (dest / "library" / "custom" / "mine.png").write_bytes(b"img")
            (dest / "cache" / "thumbs").mkdir(parents=True)
            (dest / "cache" / "thumbs" / "x.png").write_bytes(b"thumb")
            (dest / "logs").mkdir()
            (dest / "logs" / "update.log").write_text("log", "utf-8")

            fake = types.SimpleNamespace(_step=lambda msg: None,
                                         after=lambda *args: None,
                                         _install_done=lambda: None,
                                         _fatal=lambda msg=None: None,
                                         _instance_reappeared=lambda: None,
                                         _backup_program_files=lambda d: inst.Wizard._backup_program_files(fake, d),
                                         _restore_program_files=lambda b, d: inst.Wizard._restore_program_files(fake, b, d))
            with mock.patch.object(inst, "SRC", src), \
                    mock.patch.object(inst, "INSTALL_FILES",
                                      ["launcher.pyw", "core.py"]), \
                    mock.patch.object(inst, "INSTALL_DIRS", ["app"]):
                inst.Wizard._install(fake, dest)

            self.assertEqual((dest / "config.json").read_text("utf-8"),
                             '{"user": 1}')
            self.assertTrue((dest / "library" / "custom" / "mine.png").is_file())
            self.assertEqual((dest / "cache" / "thumbs" / "x.png").read_bytes(),
                             b"thumb")
            self.assertEqual((dest / "logs" / "update.log").read_text("utf-8"),
                             "log")
            # 程序文件确实被新版本覆盖
            self.assertEqual((dest / "launcher.pyw").read_text("utf-8"),
                             "new-launcher.pyw")
            self.assertEqual((dest / "app" / "index.html").read_text("utf-8"),
                             "new-index")


class InstallerUpgradeInvariantTest(unittest.TestCase):
    """源码级不变量：升级前查实例，且不自行查杀进程。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "installer.pyw").read_text("utf-8")

    def test_consults_running_instance_before_next(self):
        block = re.search(r"def _next\(self\):(.*?)def ", self.src, re.S).group(1)
        self.assertIn("_confirm_not_running(", block,
                      "开始安装前必须确认没有运行中的实例")
        self.assertLess(block.index("_confirm_not_running("),
                        block.index('_show("installing")'))

    def test_never_kills_processes(self):
        for banned in ("taskkill", "TerminateProcess", "os.kill", "psutil",
                       "Popen(['task"):
            self.assertNotIn(banned, self.src)

    def test_judges_by_port_file_not_browser_window(self):
        self.assertIn("/api/identity", self.src)
        for banned in ("msedge", "chrome.exe"):
            self.assertNotIn(banned, self.src)


class InstallSafetyTest(unittest.TestCase):
    """整改 P2：向导检查与复制之间有时间窗；复制失败要能恢复原程序文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _make(self):
        """源目录（新版本）与目标目录（旧版本 + 用户数据）。"""
        src, dest = self.td / "src", self.td / "dest"
        (src / "app").mkdir(parents=True)
        (src / "app" / "index.html").write_text("new-index", "utf-8")
        (src / "launcher.pyw").write_text("new-launcher", "utf-8")
        (src / "core.py").write_text("new-core", "utf-8")
        dest.mkdir()
        (dest / "launcher.pyw").write_text("old-launcher", "utf-8")
        (dest / "app").mkdir()
        (dest / "app" / "index.html").write_text("old-index", "utf-8")
        (dest / "config.json").write_text('{"user": 1}', "utf-8")
        return src, dest

    def _fake(self):
        steps, scheduled, destroyed = [], [], []
        fake = types.SimpleNamespace(
            _step=steps.append,
            after=lambda delay, fn=None, *a, **k:
                scheduled.append(fn) if callable(fn) else None,
            _install_done=lambda: steps.append("_install_done"),
            pb=types.SimpleNamespace(stop=lambda: None),
            var_dir=types.SimpleNamespace(get=lambda: str(self.td / "dest")),
            _show=lambda view: steps.append("_show:" + view),
            destroy=lambda: destroyed.append(1),
        )
        # 绑定真实的收尾方法（弹窗已由 _drain 打桩），只替换 destroy
        fake._fatal = lambda msg=None: inst.Wizard._fatal(fake, msg)
        fake._instance_reappeared = lambda: inst.Wizard._instance_reappeared(fake)
        fake._backup_program_files = lambda d: inst.Wizard._backup_program_files(fake, d)
        fake._restore_program_files = lambda b, d: inst.Wizard._restore_program_files(fake, b, d)
        return fake, steps, scheduled, destroyed

    def _drain(self, scheduled):
        """按真实顺序执行 after() 排队的回调，收集弹窗正文（a[1] 是消息文本）。"""
        shown = []
        from tkinter import messagebox
        with mock.patch.object(messagebox, "showwarning",
                               lambda *a, **k: shown.append(a[1])), \
                mock.patch.object(messagebox, "showerror",
                                  lambda *a, **k: shown.append(a[1])):
            for fn in list(scheduled):
                fn()
        return shown

    def test_rechecks_instance_before_copying(self):
        """向导检查通过后、复制前实例又启动：必须中止且不覆盖任何文件。"""
        src, dest = self._make()
        fake, steps, scheduled, destroyed = self._fake()
        with mock.patch.object(inst, "SRC", src), \
                mock.patch.object(inst, "INSTALL_FILES", ["launcher.pyw"]), \
                mock.patch.object(inst, "INSTALL_DIRS", ["app"]), \
                mock.patch.object(inst, "running_instance_in", lambda d: {"port": 1}):
            inst.Wizard._install(fake, dest)
        self.assertEqual((dest / "launcher.pyw").read_text("utf-8"), "old-launcher")
        self.assertEqual((dest / "app" / "index.html").read_text("utf-8"), "old-index")
        self.assertFalse(fake._result_ok)
        shown = self._drain(scheduled)      # 弹窗与 destroy 都发生在 after 回调里
        self.assertTrue(destroyed, "应中止安装流程")
        self.assertTrue(any("重新启动" in s for s in shown), shown)

    def test_restores_original_files_when_copy_fails(self):
        """复制中途 I/O 失败：把备份的原程序文件放回去，不留混合版本。"""
        src, dest = self._make()
        fake, steps, scheduled, destroyed = self._fake()
        real_copy2 = shutil.copy2
        calls = {"n": 0}

        def flaky_copy2(s_, d_, *a, **k):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk full")
            return real_copy2(s_, d_, *a, **k)

        with mock.patch.object(inst, "SRC", src), \
                mock.patch.object(inst, "INSTALL_FILES",
                                  ["launcher.pyw", "core.py"]), \
                mock.patch.object(inst, "INSTALL_DIRS", ["app"]), \
                mock.patch.object(inst, "running_instance_in", lambda d: None), \
                mock.patch.object(inst.shutil, "copy2", flaky_copy2):
            inst.Wizard._install(fake, dest)
        self.assertEqual((dest / "launcher.pyw").read_text("utf-8"), "old-launcher")
        self.assertEqual((dest / "app" / "index.html").read_text("utf-8"), "old-index")
        self.assertFalse((dest / "core.py").exists(), "失败前未写入的文件不应出现")
        self.assertEqual((dest / "config.json").read_text("utf-8"), '{"user": 1}')
        shown = self._drain(scheduled)
        self.assertTrue(any("安装失败" in s and "disk full" in s for s in shown), shown)

    def test_reports_mixed_state_when_restore_also_fails(self):
        """复制失败且恢复也不完整：必须明确提示，不能让用户以为升级成功。"""
        src, dest = self._make()
        fake, steps, scheduled, destroyed = self._fake()
        real_copy2 = shutil.copy2

        def flaky_copy2(s_, d_, *a, **k):
            if str(d_).startswith(str(dest)):
                raise OSError("disk full")
            return real_copy2(s_, d_, *a, **k)

        with mock.patch.object(inst, "SRC", src), \
                mock.patch.object(inst, "INSTALL_FILES",
                                  ["launcher.pyw", "core.py"]), \
                mock.patch.object(inst, "INSTALL_DIRS", ["app"]), \
                mock.patch.object(inst, "running_instance_in", lambda d: None), \
                mock.patch.object(inst.shutil, "copy2", flaky_copy2), \
                mock.patch.object(inst.Wizard, "_restore_program_files",
                                  lambda self_, b, d: ["launcher.pyw（boom）"]):
            inst.Wizard._install(fake, dest)
        self.assertFalse(fake._result_ok)
        shown = self._drain(scheduled)
        self.assertTrue(any("新旧混合" in s and "disk full" in s for s in shown), shown)


if __name__ == "__main__":
    unittest.main()
