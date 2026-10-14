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


class ConfigImportExportTest(RuntimeBase):
    def _r(self):
        r = rt.Runtime(tick_seconds=9999)
        r.apply_config({"desktop": {"enabled": True, "interval": 15},
                        "lock": {"enabled": False, "interval": 90},
                        "use_pjsk": True, "use_custom": False, "cap_pjsk": 50,
                        "update": {"count": 5, "min_rarity": 4, "interval": 240,
                                   "interval_enabled": True, "chara": "奏",
                                   "unit": "mmj", "region": "cn", "trained": False}})
        return r

    # ---------------- 导出 ----------------

    def test_export_shape_and_excludes_machine_fields(self):
        r = self._r()
        out = r.export_config()
        self.assertEqual(out["app"], "pjsk-wallpaper")
        self.assertEqual(out["kind"], "settings")
        self.assertEqual(out["version"], rt.EXPORT_VERSION)
        self.assertIn("exported_at", out)
        cfg = out["config"]
        self.assertNotIn("screen", cfg)
        self.assertNotIn("autostart", cfg)
        self.assertNotIn("last_run", cfg["update"])
        self.assertIn("screen", out["machine"])            # 本机项另存
        self.assertEqual(cfg["desktop"]["interval"], 15)
        self.assertEqual(cfg["update"]["region"], "cn")

    def test_export_does_not_leak_absolute_paths(self):
        import json as _json
        text = _json.dumps(self._r().export_config(), ensure_ascii=False)
        self.assertNotIn(str(self.td), text)
        self.assertNotIn("library", text)

    # ---------------- 导入：原始 config.json ----------------

    def test_import_raw_config_json(self):
        r = rt.Runtime(tick_seconds=9999)
        raw = {
            "screen": [2560, 1440],
            "desktop": {"enabled": False, "interval": 7},
            "lock": {"enabled": True, "interval": 33},
            "use_pjsk": False, "use_custom": True, "cap_pjsk": 12,
            "update": {"count": 2, "region": "jp", "trained": True, "chara": "",
                       "unit": "", "min_rarity": 3, "interval_enabled": True,
                       "interval": 480, "last_run": 123.0},
            "autostart": True,
        }
        ok, errors, warnings, applied = r.import_config(raw)
        self.assertTrue(ok, errors)
        self.assertEqual(errors, [])
        self.assertIn("desktop", applied)
        self.assertIn("update", applied)
        cfg = r.config()
        self.assertFalse(cfg["desktop"]["enabled"])
        self.assertEqual(cfg["desktop"]["interval"], 7)
        self.assertEqual(cfg["lock"]["interval"], 33)
        self.assertEqual(cfg["cap_pjsk"], 12)
        self.assertEqual(cfg["update"]["interval"], 480)
        self.assertTrue(cfg["update"]["region"] == "jp")
        # 本机项被忽略并给出说明
        joined = " ".join(warnings)
        self.assertIn("screen", joined)
        self.assertIn("autostart", joined)
        self.assertIn("last_run", joined)
        # 持久化
        saved = json.loads(self.cfg_path.read_text("utf-8"))
        self.assertEqual(saved["desktop"]["interval"], 7)

    # ---------------- 导入：导出信封往返 ----------------

    def test_export_import_roundtrip(self):
        src = self._r()
        payload = src.export_config()
        dst = rt.Runtime(tick_seconds=9999)
        ok, errors, _w, _a = dst.import_config(payload)
        self.assertTrue(ok, errors)
        a, b = src.config(), dst.config()
        for key in ("desktop", "lock", "use_pjsk", "use_custom", "cap_pjsk"):
            self.assertEqual(a[key], b[key])
        for key in ("count", "min_rarity", "interval", "interval_enabled",
                    "chara", "unit", "region", "trained"):
            self.assertEqual(a["update"][key], b["update"][key], key)

    # ---------------- 导入：校验失败 ----------------

    def test_import_rejects_non_object(self):
        r = rt.Runtime(tick_seconds=9999)
        for bad in ([1, 2], "x", 5, None):
            ok, errors, _w, _a = r.import_config(bad)
            self.assertFalse(ok)
            self.assertTrue(errors)

    def test_import_rejects_unknown_key(self):
        r = rt.Runtime(tick_seconds=9999)
        ok, errors, _w, _a = r.import_config({"nope": 1})
        self.assertFalse(ok)
        self.assertTrue(any("nope" in e for e in errors))
        ok, errors, _w, _a = r.import_config({"desktop": {"hack": 1}})
        self.assertFalse(ok)
        ok, errors, _w, _a = r.import_config({"update": {"weird": 1}})
        self.assertFalse(ok)

    def test_import_rejects_bad_types_and_ranges(self):
        r = rt.Runtime(tick_seconds=9999)
        cases = [
            {"desktop": {"interval": "abc"}},
            {"desktop": {"interval": 0}},
            {"lock": {"interval": 99999}},
            {"cap_pjsk": 0},
            {"desktop": {"enabled": "yes"}},
            {"update": {"min_rarity": 9}},
            {"update": {"region": "xx"}},
            {"update": {"chara": "x" * 100}},
        ]
        for bad in cases:
            ok, errors, _w, _a = r.import_config(bad)
            self.assertFalse(ok, f"应拒绝: {bad}")
            self.assertTrue(errors)

    def test_import_failure_writes_nothing(self):
        r = self._r()
        r.apply_config({"desktop": {"interval": 11}})
        before = self.cfg_path.read_text("utf-8")
        ok, _e, _w, _a = r.import_config({"desktop": {"interval": 30},
                                          "unknown": 1})
        self.assertFalse(ok)
        self.assertEqual(self.cfg_path.read_text("utf-8"), before)
        self.assertEqual(r.config()["desktop"]["interval"], 11)

    def test_import_partial_merges_without_wiping(self):
        r = self._r()
        ok, errors, _w, _a = r.import_config({"desktop": {"interval": 9}})
        self.assertTrue(ok, errors)
        cfg = r.config()
        self.assertEqual(cfg["desktop"]["interval"], 9)
        self.assertTrue(cfg["lock"]["enabled"] is False)   # 其余保持不变
        self.assertEqual(cfg["cap_pjsk"], 50)

    def test_import_newer_version_warns_but_applies(self):
        r = rt.Runtime(tick_seconds=9999)
        ok, errors, warnings, _a = r.import_config({
            "app": "pjsk-wallpaper", "version": 999,
            "config": {"desktop": {"interval": 21}}})
        self.assertTrue(ok, errors)
        self.assertTrue(any("版本" in w for w in warnings))
        self.assertEqual(r.config()["desktop"]["interval"], 21)

    def test_import_ignores_non_settings_envelope_keys(self):
        """导出的信封里 machine/exported_at 等不应被当成未知项拒绝。"""
        r = rt.Runtime(tick_seconds=9999)
        payload = r.export_config()
        payload["machine"] = {"screen": [800, 600]}
        payload["extra"] = "ignored"
        ok, errors, _w, _a = r.import_config(payload)
        self.assertTrue(ok, errors)

    def test_import_sets_status(self):
        r = rt.Runtime(tick_seconds=9999)
        r.import_config({"use_custom": True})
        self.assertIn("导入", r.state()["status"])


if __name__ == "__main__":
    unittest.main()
