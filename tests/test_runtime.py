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


class ConfigConsistencyTest(RuntimeBase):
    """配置的读改写与持久化必须一致：不丢更新、不报假成功、不写坏磁盘。"""

    _PATCHES = [
        {"desktop": {"interval": 11}},
        {"lock": {"interval": 77}},
        {"cap_pjsk": 123},
        {"update": {"count": 7}},
        {"update": {"min_rarity": 2}},
        {"update": {"chara": "奏"}},
        {"update": {"region": "cn"}},
        {"use_custom": True},
    ]

    def _assert_patches_applied(self, cfg, label):
        self.assertEqual(cfg["desktop"]["interval"], 11, label)
        self.assertEqual(cfg["lock"]["interval"], 77, label)
        self.assertEqual(cfg["cap_pjsk"], 123, label)
        self.assertEqual(cfg["update"]["count"], 7, label)
        self.assertEqual(cfg["update"]["min_rarity"], 2, label)
        self.assertEqual(cfg["update"]["chara"], "奏", label)
        self.assertEqual(cfg["update"]["region"], "cn", label)
        self.assertTrue(cfg["use_custom"], label)

    def test_concurrent_distinct_field_patches_do_not_lose_updates(self):
        r = rt.Runtime(tick_seconds=9999)
        results = []
        barrier = threading.Barrier(len(self._PATCHES))

        def worker(patch):
            barrier.wait(10)
            results.append(r.apply_config(patch))

        ts = [threading.Thread(target=worker, args=(p,)) for p in self._PATCHES]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)

        self.assertTrue(all(ok for ok, _e, _w in results), results)
        self._assert_patches_applied(r.config(), "内存")
        self._assert_patches_applied(
            json.loads(self.cfg_path.read_text("utf-8")), "磁盘")

    def test_concurrent_config_change_and_update_keep_last_run(self):
        """图库更新写的 last_run 不得被并发的旧配置快照回写。"""
        r = rt.Runtime(tick_seconds=9999)
        barrier = threading.Barrier(2)

        def patcher():
            barrier.wait(10)
            r.apply_config({"desktop": {"interval": 45}})

        def updater():
            barrier.wait(10)
            with mock.patch.object(r, "_run_update", lambda: None):
                r.start_update(manual=True)

        ts = [threading.Thread(target=patcher), threading.Thread(target=updater)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)

        for label, cfg in (("内存", r.config()),
                           ("磁盘", json.loads(self.cfg_path.read_text("utf-8")))):
            self.assertEqual(cfg["desktop"]["interval"], 45, label)
            self.assertGreater(cfg["update"]["last_run"], 0, label)

    def test_save_failure_is_reported_and_memory_stays_consistent(self):
        r = rt.Runtime(tick_seconds=9999)
        r.apply_config({"desktop": {"interval": 5}})
        with mock.patch.object(core, "config_save", side_effect=OSError("磁盘已满")):
            ok, errors, _w = r.apply_config({"desktop": {"interval": 45}})
        self.assertFalse(ok, "落盘失败不得报告成功")
        self.assertTrue(errors)
        # 内存与磁盘都保持旧值，不出现「报成功却状态不一致」
        self.assertEqual(r.config()["desktop"]["interval"], 5)
        self.assertEqual(json.loads(self.cfg_path.read_text("utf-8"))
                         ["desktop"]["interval"], 5)


class StartupWallpaperTest(RuntimeBase):
    """启动时当前系统壁纸读不到，不得让运行时构造失败。

    回归背景：`core.get_desktop_wallpaper()` 在原壁纸文件已被删除（或系统为纯色
    壁纸）时如实返回 None，而 `Runtime.__init__` 直接把 None 交给
    `core.path_to_image_id()`，`Path(None)` 抛 TypeError。启动器用 pythonw 运行，
    异常无控制台输出，表现为「双击快捷方式没有任何反应」。
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(core, "APP_DIR", self.td).start()
        mock.patch.object(core, "get_desktop_wallpaper", lambda: None).start()

    def test_none_wallpaper_does_not_break_startup(self):
        r = rt.Runtime(tick_seconds=9999)
        self.assertIsNone(r._desk_current)

    def test_existing_wallpaper_still_maps_to_id(self):
        f = self.pjsk / "card_000.png"
        f.write_bytes(b"x")
        with mock.patch.object(core, "get_desktop_wallpaper", lambda: f):
            r = rt.Runtime(tick_seconds=9999)
        self.assertEqual(r._desk_current, "pjsk/card_000.png")


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


class StatePushTest(RuntimeBase):
    """预览与系统切换保持同步的机制：状态修订号、长轮询、切换前广播 pending。"""

    def _runtime(self):
        r = rt.Runtime(tick_seconds=9999)
        r._save_last_state = lambda: None      # 不写真实的 runtime/state.json
        return r

    def test_state_exposes_rev_and_pending(self):
        r = self._runtime()
        s = r.state()
        self.assertIn("rev", s)
        self.assertIsNone(s["desktop"]["pending"])
        self.assertIsNone(s["lock"]["pending"])

    def test_state_has_no_preview_delay(self):
        """前端不做人为延迟：收到 pending 即开始过渡，与后端发起切换同时刻。

        曾有过的 preview_delay_ms（按实测补偿系统换图的起步延迟）已移除——
        它是经验值，在不同机器/图片上不稳定，反而不如程序层面同步。
        """
        s = self._runtime().state()
        self.assertNotIn("preview_delay_ms", s)

    def test_pending_is_broadcast_before_wallpaper_is_written(self):
        """核心时序：写系统壁纸之前就要把新图广播出去。

        core.set_desktop 会阻塞到 shell 开始淡入；若等它返回再通知前端，
        预览必然滞后一整段过渡——这正是「背景过渡结束后预览才切换」的根因。
        """
        self._files(3)
        r = self._runtime()
        marks = []

        def fake_set_desktop(_path):
            marks.append(time.time())
            time.sleep(0.3)      # 真实 SPI_SETDESKWALLPAPER 会阻塞到 shell 开始淡入

        got = {}

        def watcher(rev):
            st = r.wait_state(rev, 5)
            got["pending"] = st["desktop"]["pending"]
            got["at"] = time.time()

        with mock.patch.object(core, "set_desktop", fake_set_desktop), \
                mock.patch.object(core, "enable_desktop_fade", lambda: True):
            t = threading.Thread(target=watcher, args=(r.revision(),))
            t.start()
            time.sleep(0.1)
            res = r.switch("desktop", manual=True)
            t.join(5)

        self.assertTrue(res["ok"], res)
        self.assertEqual(len(marks), 1, "应只调用一次写壁纸")
        self.assertIsNotNone(got.get("pending"), "切换前应先广播 pending")
        self.assertEqual(got["pending"]["id"], "pjsk/" + res["name"])
        self.assertLess(got["at"], marks[0] + 0.15,
                        "广播应发生在写系统壁纸开始前后，而不是等它返回（阻塞约 0.3s）")
        # 结束后 pending 清空，current 收敛为新图
        st = r.state()
        self.assertIsNone(st["desktop"]["pending"])
        self.assertEqual(st["desktop"]["current"]["id"], "pjsk/" + res["name"])

    def test_rev_advances_on_config_and_switch(self):
        self._files(2)
        r = self._runtime()
        rev0 = r.revision()
        r.apply_config({"desktop": {"interval": 45}})
        rev1 = r.revision()
        self.assertGreater(rev1, rev0)
        with mock.patch.object(core, "set_desktop", lambda p: None), \
                mock.patch.object(core, "enable_desktop_fade", lambda: True):
            r.switch("desktop", manual=True)
        self.assertGreater(r.revision(), rev1)

    def test_wait_state_times_out_when_nothing_changes(self):
        r = self._runtime()
        t0 = time.time()
        r.wait_state(r.revision(), 0.4)
        self.assertGreaterEqual(time.time() - t0, 0.3)

    def test_failed_switch_clears_pending(self):
        """切换失败也不能把界面卡在「正在应用」上。"""
        self._files(2)
        r = self._runtime()
        with mock.patch.object(core, "enable_desktop_fade", lambda: True), \
                mock.patch.object(core, "set_desktop",
                                  side_effect=OSError("boom")):
            res = r.switch("desktop", manual=True)
        self.assertFalse(res["ok"])
        self.assertIsNone(r.state()["desktop"]["pending"])


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


class UpdateProgressTest(RuntimeBase):
    """图库更新的下载进度：子进程逐行输出 → 运行时逐行发布 → 状态立即可见。"""

    def test_note_progress_publishes_and_bumps(self):
        r = rt.Runtime(tick_seconds=9999)
        rev = r.state()["rev"]
        r._note_progress("[进度] 2/3")
        st = r.state()
        self.assertEqual(st["update"]["progress"], {"done": 2, "total": 3})
        self.assertGreater(st["rev"], rev)          # 修订号 +1 → 挂起长轮询立刻返回
        # 非进度行（含时间戳的普通日志）忽略，且不重复发布
        r._note_progress("2026-10-14 20:00:00  + card_1.png  奏 «33»")
        self.assertEqual(r.state()["update"]["progress"], {"done": 2, "total": 3})
        self.assertEqual(r.state()["rev"], st["rev"])

    def test_run_update_streams_progress_lines(self):
        r = rt.Runtime(tick_seconds=9999)
        app = Path(tempfile.mkdtemp(prefix="pjsk-stub-"))
        (app / "gallery_update.py").write_text(
            "print('[进度] 0/2', flush=True)\n"
            "print('[进度] 1/2', flush=True)\n"
            "print('[进度] 2/2', flush=True)\n", "utf-8")
        seen = []
        with mock.patch.object(core, "APP_DIR", app), \
                mock.patch.object(r, "_note_progress",
                                  lambda line: seen.append(line)):
            r._run_update()
        self.assertEqual([l for l in seen if "[进度]" in l],
                         ["[进度] 0/2", "[进度] 1/2", "[进度] 2/2"])
        st = r.update_status()
        self.assertTrue(st["ok"])
        self.assertTrue(st["message"].startswith("完成:"))
        self.assertIsNone(st["progress"])           # 结束后进度清空（running 也为 False）

    def test_run_update_reports_reason_on_failure(self):
        r = rt.Runtime(tick_seconds=9999)
        app = Path(tempfile.mkdtemp(prefix="pjsk-stub-"))
        (app / "gallery_update.py").write_text(
            "print('[进度] 0/1', flush=True)\n"
            "print('更新失败: RuntimeError: 筛选条件下没有卡片')\n"
            "raise SystemExit(1)\n", "utf-8")
        with mock.patch.object(core, "APP_DIR", app):
            r._run_update()
        st = r.update_status()
        self.assertFalse(st["ok"])
        self.assertIn("筛选条件下没有卡片", st["message"])
        self.assertIsNone(st["progress"])

    def test_run_update_timeout_kills_child(self):
        r = rt.Runtime(tick_seconds=9999)
        app = Path(tempfile.mkdtemp(prefix="pjsk-stub-"))
        (app / "gallery_update.py").write_text("import time; time.sleep(30)\n", "utf-8")
        with mock.patch.object(core, "APP_DIR", app), \
                mock.patch.object(rt, "UPDATE_TIMEOUT_SECONDS", 0.5):
            r._run_update()
        st = r.update_status()
        self.assertFalse(st["ok"])
        self.assertIn("超时", st["message"])


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
        self.assertNotIn("close_action", cfg)
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


class CloseActionTest(RuntimeBase):
    """前端窗口关闭行为（config.close_action）：本机偏好，不随设置文件迁移。"""

    def test_default_is_ask_every_time(self):
        self.assertIsNone(rt.Runtime(tick_seconds=9999).config().get("close_action"))

    def test_set_persists_and_reloads(self):
        r = rt.Runtime(tick_seconds=9999)
        ok, errors = r.set_close_action("tray")
        self.assertTrue(ok, errors)
        self.assertEqual(r.config()["close_action"], "tray")
        saved = json.loads(self.cfg_path.read_text("utf-8"))
        self.assertEqual(saved["close_action"], "tray")
        # 重新加载仍生效（跨进程重启保留）
        self.assertEqual(rt.Runtime(tick_seconds=9999).config()["close_action"], "tray")

    def test_set_accepts_exit_and_none(self):
        r = rt.Runtime(tick_seconds=9999)
        self.assertTrue(r.set_close_action("exit")[0])
        self.assertEqual(r.config()["close_action"], "exit")
        self.assertTrue(r.set_close_action(None)[0])
        self.assertIsNone(r.config()["close_action"])

    def test_set_rejects_unknown_value(self):
        r = rt.Runtime(tick_seconds=9999)
        ok, errors = r.set_close_action("whatever")
        self.assertFalse(ok)
        self.assertTrue(errors)
        self.assertIsNone(r.config().get("close_action"))

    def test_save_failure_keeps_memory_unchanged(self):
        r = rt.Runtime(tick_seconds=9999)
        with mock.patch.object(core, "config_save", side_effect=OSError("boom")):
            ok, errors = r.set_close_action("exit")
        self.assertFalse(ok)
        self.assertTrue(errors)
        self.assertIsNone(r.config().get("close_action"))

    def test_not_exported_and_ignored_on_import(self):
        r = rt.Runtime(tick_seconds=9999)
        r.set_close_action("tray")
        self.assertNotIn("close_action", r.export_config()["config"])
        ok, errors, warnings, _applied = r.import_config({"close_action": "exit"})
        self.assertTrue(ok, errors)
        self.assertTrue(any("close_action" in w for w in warnings))
        self.assertEqual(r.config()["close_action"], "tray")   # 本机项不被导入覆盖


class ClosePromptTest(RuntimeBase):
    """关闭确认请求经状态推送投递：宿主不需要向页面注入脚本。"""

    def test_prompt_is_sticky_until_acknowledged(self):
        """整改：状态读取者不止内嵌页面一个，事件不能读一次就清零。

        曾是「读到即消费」：并发状态请求（前端保存配置/更新时自己也会再发一次
        /api/state）会抢先消费掉，真正要弹框的页面收不到。
        """
        r = rt.Runtime(tick_seconds=9999)
        self.assertFalse(r.state()["close_prompt"])     # 默认无请求
        r.request_close_prompt()
        self.assertTrue(r.state()["close_prompt"])      # 第一个读者
        self.assertTrue(r.state()["close_prompt"])      # 第二个读者仍能看到
        self.assertTrue(r.wait_state(0, 0)["close_prompt"])  # 长轮询响应同样带标志
        r.acknowledge_close_prompt()                    # 前端确认已弹框
        self.assertFalse(r.state()["close_prompt"])

    def test_prompt_can_be_requested_again_after_ack(self):
        r = rt.Runtime(tick_seconds=9999)
        r.request_close_prompt()
        r.acknowledge_close_prompt()
        r.request_close_prompt()                        # 用户再次点关闭
        self.assertTrue(r.state()["close_prompt"])
        r.acknowledge_close_prompt()
        self.assertFalse(r.state()["close_prompt"])

    def test_prompt_wakes_pending_long_poll(self):
        """请求弹框要让挂起中的长轮询立刻返回，否则窗口关闭后会等满超时。"""
        r = rt.Runtime(tick_seconds=9999)
        rev = r.state()["rev"]
        out = {}
        t = threading.Thread(target=lambda: out.update(r.wait_state(rev, 25)))
        t.start()
        time.sleep(0.3)
        r.request_close_prompt()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "长轮询没有被唤醒")
        self.assertTrue(out.get("close_prompt"))


if __name__ == "__main__":
    unittest.main()
