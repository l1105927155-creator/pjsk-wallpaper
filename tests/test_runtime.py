# -*- coding: utf-8 -*-
"""运行时（runtime.py）单测：调度独立性、切换与图库为空、更新互斥、配置校验与落盘。

不触发真实换壁纸/联网：系统调用与更新子进程均被替换。
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402
import runtime as rt  # noqa: E402


class RuntimeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.td = Path(self._tmp.name)
        self.cfg_path = self.td / "config.json"
        self.pjsk = self.td / "library" / "pjsk"
        self.custom = self.td / "library" / "custom"
        self.pjsk.mkdir(parents=True)
        self.custom.mkdir(parents=True)
        self._patchers = [
            mock.patch.object(core, "CONFIG_PATH", self.cfg_path),
            mock.patch.object(core, "PJSK_DIR", self.pjsk),
            mock.patch.object(core, "CUSTOM_DIR", self.custom),
            mock.patch.object(core, "detect_screen", lambda: [2560, 1440]),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patchers:
            self.addCleanup(p.stop)

    def _files(self, n, source="pjsk"):
        d = self.pjsk if source == "pjsk" else self.custom
        out = []
        for i in range(n):
            p = d / f"card_{i:03d}.png"
            p.write_bytes(b"x")
            out.append(p)
        return out


class ConfigValidationTest(RuntimeBase):
    def test_reject_bad_type_and_range(self):
        r = rt.Runtime(tick_seconds=9999)
        ok, errors, _ = r.apply_config({"desktop": {"interval": "abc"}})
        self.assertFalse(ok)
        self.assertTrue(any("interval" in e for e in errors))
        ok, errors, _ = r.apply_config({"desktop": {"interval": 99999}})
        self.assertFalse(ok)
        ok, errors, _ = r.apply_config({"cap_pjsk": 0})
        self.assertFalse(ok)
        ok, errors, _ = r.apply_config({"update": {"min_rarity": 9}})
        self.assertFalse(ok)

    def test_reject_unknown_key(self):
        r = rt.Runtime(tick_seconds=9999)
        ok, errors, _ = r.apply_config({"desktop": {"hack": 1}})
        self.assertFalse(ok)
        ok, errors, _ = r.apply_config({"nope": 1})
        self.assertFalse(ok)

    def test_accept_valid_and_persist(self):
        r = rt.Runtime(tick_seconds=9999)
        ok, errors, _ = r.apply_config({
            "desktop": {"enabled": False, "interval": 5},
            "update": {"chara": "奏", "min_rarity": 4, "interval_enabled": True},
        })
        self.assertTrue(ok, errors)
        cfg = json.loads(self.cfg_path.read_text("utf-8"))
        self.assertEqual(cfg["desktop"]["interval"], 5)
        self.assertFalse(cfg["desktop"]["enabled"])
        self.assertEqual(cfg["update"]["chara"], "奏")

    def test_both_sources_off_warns_but_saves(self):
        r = rt.Runtime(tick_seconds=9999)
        ok, errors, warns = r.apply_config({"use_pjsk": False, "use_custom": False})
        self.assertTrue(ok)
        self.assertTrue(warns)

    def test_invalid_patch_does_not_write(self):
        r = rt.Runtime(tick_seconds=9999)
        r.apply_config({"desktop": {"interval": 5}})
        before = self.cfg_path.read_text("utf-8")
        r.apply_config({"desktop": {"interval": -1}})
        self.assertEqual(self.cfg_path.read_text("utf-8"), before)


class SwitchTest(RuntimeBase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(core, "enable_desktop_fade", lambda: True).start()
        mock.patch.object(core, "set_desktop",
                          lambda p: self.calls.append(("desktop", p.name))).start()
        mock.patch.object(core, "set_lockscreen",
                          lambda p: self.calls.append(("lock", p.name))).start()

    def test_desktop_and_lock_are_independent(self):
        self._files(3)
        r = rt.Runtime(tick_seconds=9999)
        r.switch("desktop", manual=True)
        time.sleep(0.02)
        r.switch("lock", manual=True)
        st = r.state()
        self.assertIsNotNone(st["desktop"]["last"])
        self.assertIsNotNone(st["lock"]["last"])
        self.assertNotEqual(st["desktop"]["last"], st["lock"]["last"])
        # 触发锁屏不改变已记录的桌面计时
        self.assertLessEqual(st["desktop"]["last"], st["lock"]["last"])

    def test_no_repeat_within_round(self):
        self._files(4)
        r = rt.Runtime(tick_seconds=9999)
        names = []
        for _ in range(4):
            res = r.switch("desktop", manual=True)
            names.append(res["name"])
        self.assertEqual(len(set(names)), 4)

    def test_empty_library(self):
        r = rt.Runtime(tick_seconds=9999)
        res = r.switch("desktop", manual=True)
        self.assertTrue(res["empty"])
        self.assertFalse(res["ok"])

    def test_set_desktop_failure_reported(self):
        self._files(1)
        r = rt.Runtime(tick_seconds=9999)
        with mock.patch.object(core, "set_desktop",
                               side_effect=OSError("boom")):
            res = r.switch("desktop", manual=True)
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["error"])


class SchedulerTest(RuntimeBase):
    def test_due_switches_desktop_and_lock_separately(self):
        self._files(2)
        calls = []
        with mock.patch.object(core, "enable_desktop_fade", lambda: True), \
                mock.patch.object(core, "set_desktop",
                                  lambda p: calls.append("desktop")), \
                mock.patch.object(core, "set_lockscreen",
                                  lambda p: calls.append("lock")):
            r = rt.Runtime(tick_seconds=9999)
            r.apply_config({"desktop": {"enabled": True, "interval": 1},
                            "lock": {"enabled": False, "interval": 60}})
            r._tick_once()   # 首次：desk_last 为 None → 触发一次桌面
            self.assertEqual(calls, ["desktop"])
            calls.clear()
            r._tick_once()   # 未到间隔：不触发
            self.assertEqual(calls, [])


class UpdateTest(RuntimeBase):
    def test_start_update_mutex(self):
        r = rt.Runtime(tick_seconds=9999)
        started = threading.Event()

        def fake_run():
            started.set()
            time.sleep(0.3)
            with r._lock:
                r._update.update(running=False, finished_at=time.time(),
                                 ok=True, message="完成: ok")

        with mock.patch.object(r, "_run_update", side_effect=fake_run):
            first = r.start_update(manual=True)
            self.assertTrue(first["ok"])
            started.wait(1)
            second = r.start_update(manual=True)
            self.assertTrue(second["busy"])
            self.assertFalse(second["ok"])
            time.sleep(0.4)
        self.assertFalse(r.update_status()["running"])

    def test_start_update_records_last_run(self):
        r = rt.Runtime(tick_seconds=9999)
        with mock.patch.object(r, "_run_update", lambda: None):
            r.start_update(manual=True)
        self.assertGreater(r.update_status()["last_run"], 0)
        self.assertGreater(json.loads(self.cfg_path.read_text("utf-8"))
                           ["update"]["last_run"], 0)


if __name__ == "__main__":
    unittest.main()
