# -*- coding: utf-8 -*-
"""核心逻辑单测：配置合并/持久化、图库扫描、Rotator 去重、图片填充与入库。

只测试与 Tk 无关的纯逻辑；Windows OS 行为（实际换壁纸）见
docs/baseline-behavior.md 的手工验收步骤。
"""
import json
import random
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
