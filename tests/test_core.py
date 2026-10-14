# -*- coding: utf-8 -*-
"""核心逻辑单测：配置合并/持久化、图库扫描、Rotator 去重、图片填充与入库。

只测试与 Tk 无关的纯逻辑；Windows OS 行为（实际换壁纸）见
docs/baseline-behavior.md 的手工验收步骤。
"""
import json
import os
import random
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import core  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            with mock.patch.object(core, "CONFIG_PATH", cfg_path), \
                    mock.patch.object(core, "detect_screen", lambda: [2560, 1440]):
                cfg = core.config_load()
            self.assertTrue(cfg["desktop"]["enabled"])
            self.assertEqual(cfg["desktop"]["interval"], 30)
            self.assertEqual(cfg["lock"]["interval"], 60)
            self.assertTrue(cfg["use_pjsk"])
            self.assertFalse(cfg["use_custom"])
            self.assertEqual(cfg["cap_pjsk"], 20)
            self.assertEqual(cfg["update"]["count"], 3)
            self.assertEqual(cfg["update"]["min_rarity"], 3)
            self.assertEqual(cfg["screen"], [2560, 1440])

    def test_partial_merge_keeps_user_and_fills_missing(self):
        """旧 config 只写了部分键：用户值保留，缺失键用默认值补齐（向后兼容）。"""
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(json.dumps({
                "desktop": {"interval": 3},
                "use_custom": True,
                "screen": [1920, 1080],
            }), "utf-8")
            with mock.patch.object(core, "CONFIG_PATH", cfg_path):
                cfg = core.config_load()
            self.assertEqual(cfg["desktop"]["interval"], 3)
            self.assertTrue(cfg["desktop"]["enabled"])  # 默认补齐
            self.assertTrue(cfg["use_custom"])
            self.assertEqual(cfg["lock"]["interval"], 60)  # 默认补齐
            self.assertEqual(cfg["screen"], [1920, 1080])  # 用户值优先

    def test_corrupt_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text("{ not json", "utf-8")
            with mock.patch.object(core, "CONFIG_PATH", cfg_path), \
                    mock.patch.object(core, "detect_screen", lambda: [1280, 720]):
                cfg = core.config_load()
            self.assertEqual(cfg["desktop"]["interval"], 30)
            self.assertEqual(cfg["screen"], [1280, 720])

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            with mock.patch.object(core, "CONFIG_PATH", cfg_path):
                cfg = core.config_load()
                cfg["desktop"]["interval"] = 7
                cfg["update"]["chara"] = "奏"
                core.config_save(cfg)
                again = core.config_load()
            self.assertEqual(again["desktop"]["interval"], 7)
            self.assertEqual(again["update"]["chara"], "奏")


class ConfigSaveConcurrencyTest(unittest.TestCase):
    """原子写盘在多写入者并发时必须成立：不得竞争同一个临时文件。"""

    def test_concurrent_writers_share_no_temp_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            errors = []

            def writer(i):
                try:
                    core.config_save({"n": i})
                except Exception as e:            # noqa: BLE001 —— 记录全部异常
                    errors.append(f"{type(e).__name__}: {e}")

            with mock.patch.object(core, "CONFIG_PATH", cfg_path):
                for _ in range(5):
                    ts = [threading.Thread(target=writer, args=(i,))
                          for i in range(16)]
                    for t in ts:
                        t.start()
                    for t in ts:
                        t.join(10)

            self.assertEqual(errors, [], f"并发写入不应报错，实际: {errors[:3]}")
            data = json.loads(cfg_path.read_text("utf-8"))   # 内容完整可解析
            self.assertIn("n", data)
            self.assertEqual([p.name for p in cfg_path.parent.iterdir()],
                             ["config.json"], "不应残留临时文件")


class ThumbnailCacheTest(unittest.TestCase):
    """缩略图缓存：并发安全、跨来源同名不串味、内容更新失效、失败不污染缓存。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        td = Path(self._tmp.name)
        self.pjsk = td / "pjsk"
        self.custom = td / "custom"
        self.thumbs = td / "thumbs"
        self.pjsk.mkdir()
        self.custom.mkdir()
        self.addCleanup(self._tmp.cleanup)
        self._p = mock.patch.object(core, "THUMB_DIR", self.thumbs)
        self._p.start()
        self.addCleanup(self._p.stop)

    def _img(self, d: Path, name: str, size, color) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        Image.new("RGB", size, color).save(p)
        return p

    def _tmp_files(self):
        if not self.thumbs.is_dir():
            return []
        return [p.name for p in self.thumbs.iterdir() if p.suffix == ".tmp"]

    def test_concurrent_same_image_no_corruption(self):
        src = self._img(self.pjsk, "card.png", (200, 100), (10, 20, 30))
        errors, outs = [], []
        barrier = threading.Barrier(16)

        def worker():
            try:
                barrier.wait(10)
                outs.append(core.thumbnail(src, 64))
            except Exception as e:            # noqa: BLE001 —— 记录全部异常
                errors.append(f"{type(e).__name__}: {e}")

        ts = [threading.Thread(target=worker) for _ in range(16)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(20)

        self.assertEqual(errors, [], f"并发取同一缩略图不应报错: {errors[:3]}")
        self.assertEqual(len(outs), 16)
        self.assertEqual({str(p) for p in outs}, {str(outs[0])})
        with Image.open(outs[0]) as im:          # 内容完整、未被并发写坏
            self.assertEqual(im.size, (64, 32))
            self.assertEqual(im.getpixel((32, 16)), (10, 20, 30))
        self.assertEqual(self._tmp_files(), [], "不应残留临时文件")

    def test_same_name_different_source_not_shared(self):
        a = self._img(self.pjsk, "card.png", (200, 100), (200, 0, 0))
        b = self._img(self.custom, "card.png", (200, 100), (0, 0, 200))
        same = 1_700_000_000_000_000_000
        for p in (a, b):
            os.utime(p, ns=(same, same))
        self.assertEqual(a.stat().st_mtime_ns, b.stat().st_mtime_ns)

        ta, tb = core.thumbnail(a, 64), core.thumbnail(b, 64)
        self.assertNotEqual(ta, tb, "不同目录的同名图片不得共用缓存")
        with Image.open(ta) as im:
            self.assertEqual(im.getpixel((5, 5)), (200, 0, 0))
        with Image.open(tb) as im:
            self.assertEqual(im.getpixel((5, 5)), (0, 0, 200))

    def test_cache_invalidated_after_content_update(self):
        src = self._img(self.pjsk, "card.png", (200, 100), (10, 20, 30))
        first = core.thumbnail(src, 64)
        # 覆盖内容并推进修改时间
        Image.new("RGB", (200, 100), (250, 250, 250)).save(src)
        st = src.stat()
        os.utime(src, ns=(st.st_mtime_ns + 10_000_000, st.st_mtime_ns + 10_000_000))
        second = core.thumbnail(src, 64)
        self.assertNotEqual(first, second)
        with Image.open(second) as im:
            self.assertEqual(im.getpixel((10, 10)), (250, 250, 250))

    def test_cache_invalidated_when_only_size_changes(self):
        """修改时间未变但内容长度变化时同样失效（缓存键包含文件大小）。"""
        src = self._img(self.pjsk, "card.png", (200, 100), (10, 20, 30))
        ns = src.stat().st_mtime_ns
        first = core.thumbnail(src, 64)
        Image.new("RGB", (400, 200), (9, 9, 9)).save(src)   # 尺寸/字节数不同
        os.utime(src, ns=(ns, ns))                          # 修改时间保持不变
        second = core.thumbnail(src, 64)
        self.assertNotEqual(first, second)
        with Image.open(second) as im:
            self.assertEqual(im.getpixel((10, 10)), (9, 9, 9))

    def test_failed_generation_leaves_no_cache_or_temp(self):
        src = self.pjsk / "broken.png"
        src.write_bytes(b"not an image at all")
        with self.assertRaises(Exception):
            core.thumbnail(src, 64)
        self.assertEqual(list(self.thumbs.glob("*.png")), [])
        self.assertEqual(self._tmp_files(), [])
        # 修正后重试应成功，且不受上次失败影响
        Image.new("RGB", (100, 50), (1, 2, 3)).save(src)
        st = src.stat()
        os.utime(src, ns=(st.st_mtime_ns + 5, st.st_mtime_ns + 5))
        out = core.thumbnail(src, 64)
        self.assertTrue(out.is_file())
        with Image.open(out) as im:
            self.assertEqual(im.getpixel((10, 10)), (1, 2, 3))


class ScanLibraryTest(unittest.TestCase):
    def _mk(self, d: Path, names):
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"x")

    def test_respects_source_switches(self):
        with tempfile.TemporaryDirectory() as td:
            pjsk, custom = Path(td) / "pjsk", Path(td) / "custom"
            self._mk(pjsk, ["card_1.png", "card_2.png"])
            self._mk(custom, ["my.jpg"])
            with mock.patch.object(core, "PJSK_DIR", pjsk), \
                    mock.patch.object(core, "CUSTOM_DIR", custom):
                only_pjsk = core.scan_library({"use_pjsk": True, "use_custom": False})
                both = core.scan_library({"use_pjsk": True, "use_custom": True})
                none = core.scan_library({"use_pjsk": False, "use_custom": False})
            self.assertEqual([p.name for p in only_pjsk], ["card_1.png", "card_2.png"])
            self.assertEqual([p.name for p in both],
                             ["card_1.png", "card_2.png", "my.jpg"])
            self.assertEqual(none, [])

    def test_sorted_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            pjsk = Path(td) / "pjsk"
            self._mk(pjsk, ["b.png", "a.png", "card_10.png", "card_2.png"])
            with mock.patch.object(core, "PJSK_DIR", pjsk), \
                    mock.patch.object(core, "CUSTOM_DIR", Path(td) / "custom"):
                files = core.scan_library({"use_pjsk": True})
            self.assertEqual([p.name for p in files],
                             ["a.png", "b.png", "card_10.png", "card_2.png"])


class RotatorTest(unittest.TestCase):
    def _files(self, td, n):
        d = Path(td) / "lib"
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for i in range(n):
            p = d / f"card_{i:03d}.png"
            p.write_bytes(b"x")
            out.append(p)
        return out

    def test_no_repeat_within_round(self):
        """一轮内不重复：连续取完 n 张恰好覆盖全集，再去重后开始新一轮。"""
        with tempfile.TemporaryDirectory() as td:
            files = self._files(td, 5)
            rot = core.Rotator()
            rng = random.Random(42)
            picks = [rot.next(files, rng) for _ in range(5)]
            self.assertEqual(len({p.name for p in picks}), 5)
            self.assertEqual({p.name for p in picks}, {p.name for p in files})

    def test_rebuild_on_library_change(self):
        """图库内容变化时重建队列，使新图有机会进入本轮。"""
        with tempfile.TemporaryDirectory() as td:
            files = self._files(td, 4)
            rot = core.Rotator()
            rng = random.Random(1)
            first = rot.next(files, rng)
            self.assertIsNotNone(first)
            newp = Path(td) / "lib" / "card_999.png"
            newp.write_bytes(b"x")
            files2 = sorted(files + [newp], key=lambda p: p.name)
            rot.next(files2, rng)  # 触发重建
            self.assertEqual(set(rot._snapshot), {p.name for p in files2})

    def test_empty_library_returns_none(self):
        rot = core.Rotator()
        self.assertIsNone(rot.next([], random.Random(0)))


class ImageProcessingTest(unittest.TestCase):
    def test_process_fill_matches_screen_size(self):
        with tempfile.TemporaryDirectory() as td:
            src = Image.new("RGB", (100, 400), (10, 20, 30))
            dest = Path(td) / "out.png"
            core.process_fill(src, dest, [200, 200])
            with Image.open(dest) as out:
                self.assertEqual(out.size, (200, 200))
                self.assertEqual(out.format, "PNG")

    def test_process_fill_converts_rgba(self):
        with tempfile.TemporaryDirectory() as td:
            src = Image.new("RGBA", (50, 50), (1, 2, 3, 4))
            dest = Path(td) / "out.png"
            core.process_fill(src, dest, [50, 50])
            with Image.open(dest) as out:
                self.assertEqual(out.mode, "RGB")

    def test_import_image_dedup_name(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "src"
            src_dir.mkdir()
            src = src_dir / "pic.png"
            Image.new("RGB", (10, 10)).save(src)
            dest_dir = Path(td) / "custom"
            p1 = core.import_image(src, dest_dir, [10, 10])
            p2 = core.import_image(src, dest_dir, [10, 10])
            self.assertEqual(p1.name, "pic.png")
            self.assertEqual(p2.name, "pic-1.png")
            self.assertTrue(p1.exists() and p2.exists())


class GalleryQueryTest(unittest.TestCase):
    """受控 ID 映射与路径穿越防护。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        td = Path(self._tmp.name)
        self.pjsk = td / "pjsk"
        self.custom = td / "custom"
        self.pjsk.mkdir()
        self.custom.mkdir()
        (self.pjsk / "card_1.png").write_bytes(b"x")
        (self.custom / "my.jpg").write_bytes(b"x")
        (td / "secret.png").write_bytes(b"x")   # 库外文件
        self.addCleanup(self._tmp.cleanup)
        self._p1 = mock.patch.object(core, "PJSK_DIR", self.pjsk)
        self._p2 = mock.patch.object(core, "CUSTOM_DIR", self.custom)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)

    def test_counts_and_list(self):
        self.assertEqual(core.library_counts(), {"pjsk": 1, "custom": 1, "total": 2})
        ids = [it["id"] for it in core.library_list()]
        self.assertEqual(ids, ["custom/my.jpg", "pjsk/card_1.png"])

    def test_list_filter(self):
        got = [it["id"] for it in core.library_list(source="pjsk")]
        self.assertEqual(got, ["pjsk/card_1.png"])
        got = [it["id"] for it in core.library_list(keyword="my")]
        self.assertEqual(got, ["custom/my.jpg"])

    def test_resolve_valid(self):
        p = core.resolve_image_id("pjsk/card_1.png")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "card_1.png")

    def test_reject_traversal_and_unknown(self):
        for bad in ("pjsk/../secret.png", "../secret.png", "pjsk/..\\secret.png",
                    "pjsk/card_1.png/extra", "other/card_1.png",
                    "pjsk/sub/card_1.png", "pjsk/card_1.txt", "", "pjsk/",
                    "/etc/passwd", "pjsk/\x00card_1.png"):
            self.assertIsNone(core.resolve_image_id(bad), f"应拒绝: {bad!r}")


class UpdateLogTest(unittest.TestCase):
    def test_read_tail(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            (logs / "update.log").write_text("a\nb\nc\n", "utf-8")
            with mock.patch.object(core, "APP_DIR", Path(td)):
                self.assertEqual(core.read_update_log(2), ["b", "c"])

    def test_missing_log(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(core, "APP_DIR", Path(td)):
                self.assertEqual(core.read_update_log(), [])


if __name__ == "__main__":
    unittest.main()
