# -*- coding: utf-8 -*-
"""图库更新纯逻辑单测：筛选解析、候选池、已有 id、FIFO 驱逐。

不触网：只测不发起请求的函数。
"""
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402
import gallery_update as gu  # noqa: E402
import runtime as rt  # noqa: E402


class SourceBaseTest(unittest.TestCase):
    """界面提示用的卡面图源地址：按地区给出，未知地区按 jp 兜底。"""

    def test_source_base_by_region(self):
        for region in ("jp", "cn", "en", "kr", "tw"):
            base = gu.source_base(region)
            self.assertTrue(base.startswith("https://"), base)
            self.assertEqual(base, gu.REGIONS[region]["assets"])
        self.assertEqual(gu.source_base("不存在"), gu.REGIONS["jp"]["assets"])


class ProgressMarkerTest(unittest.TestCase):
    """进度标记是 gallery_update → runtime 的跨进程契约，两处必须一致。"""

    def test_progress_line_matches_runtime_parser(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gu.progress(2, 3)
        line = buf.getvalue().strip()
        # 必须是干净的「[进度] 已下载/总计」：不带 update.log 的时间戳前缀，
        # 否则 runtime 的解析器和界面的「已下载/总计」都会带上日志时间。
        self.assertEqual(line, "[进度] 2/3")
        m = rt._UPDATE_PROGRESS_RE.search(line)
        self.assertIsNotNone(m, f"runtime 解析不到进度行: {line}")
        self.assertEqual((int(m.group(1)), int(m.group(2))), (2, 3))


class ParseTest(unittest.TestCase):
    def test_find_unit_aliases(self):
        self.assertEqual(gu.find_unit("mmj"), "idol")
        self.assertEqual(gu.find_unit("25"), "school_refusal")
        self.assertEqual(gu.find_unit("25时"), "school_refusal")
        self.assertEqual(gu.find_unit("wxs"), "theme_park")
        self.assertEqual(gu.find_unit("vocaloid"), "piapro")
        self.assertIsNone(gu.find_unit("不存在"))

    def test_find_chara_alias_and_id(self):
        self.assertEqual(gu.find_chara("kanade", {}), 17)
        self.assertEqual(gu.find_chara("21", {}), 21)
        self.assertIsNone(gu.find_chara("nobody", {}))

    def test_find_chara_chinese_name(self):
        """i18n 名字匹配应返回 int（旧版返回字符串导致中文名筛选静默失效，已修复）。"""
        names = {"17": {"firstName": "宵崎", "givenName": "奏"}}
        self.assertEqual(gu.find_chara("奏", names), 17)
        pool = gu.pick_candidates(
            [{"id": 1, "characterId": 17, "cardRarityType": "rarity_4"},
             {"id": 2, "characterId": 5, "cardRarityType": "rarity_4"}],
            {"chara": "奏"}, names, {17: "school_refusal", 5: "idol"})
        self.assertEqual([c["id"] for c in pool], [1])


class PickCandidateTest(unittest.TestCase):
    def setUp(self):
        self.cards = [
            {"id": 1, "characterId": 17, "cardRarityType": "rarity_4"},
            {"id": 2, "characterId": 17, "cardRarityType": "rarity_3"},
            {"id": 3, "characterId": 5, "cardRarityType": "rarity_4"},
            {"id": 4, "characterId": 21, "cardRarityType": "rarity_birthday"},
        ]
        self.chara_unit = {17: "school_refusal", 5: "idol", 21: "piapro"}

    def test_filter_by_min_rarity(self):
        pool = gu.pick_candidates(self.cards, {"min_rarity": 4}, {}, self.chara_unit)
        self.assertEqual({c["id"] for c in pool}, {1, 3})

    def test_filter_by_chara_alias(self):
        pool = gu.pick_candidates(self.cards, {"chara": "kanade"}, {}, self.chara_unit)
        self.assertEqual({c["id"] for c in pool}, {1, 2})

    def test_filter_by_unit_includes_piapro(self):
        pool = gu.pick_candidates(self.cards, {"unit": "piapro"}, {}, self.chara_unit)
        self.assertEqual({c["id"] for c in pool}, {4})

    def test_unknown_chara_raises(self):
        with self.assertRaises(RuntimeError):
            gu.pick_candidates(self.cards, {"chara": "无名"}, {}, self.chara_unit)


class ExistingAndCapTest(unittest.TestCase):
    def test_existing_ids_parses_names(self):
        with tempfile.TemporaryDirectory() as td:
            pjsk = Path(td) / "pjsk"
            pjsk.mkdir()
            for n in ("card_101.png", "card_102_trained.png", "note.txt"):
                (pjsk / n).write_bytes(b"x")
            with mock.patch.object(core, "PJSK_DIR", pjsk):
                ids = gu.existing_ids()
            self.assertEqual(ids, {101, 102})

    def test_enforce_cap_fifo_by_mtime(self):
        """FIFO：按修改时间驱逐最旧，保持数量 <= cap。"""
        with tempfile.TemporaryDirectory() as td:
            pjsk = Path(td) / "pjsk"
            pjsk.mkdir()
            for i in range(5):
                p = pjsk / f"card_{i}.png"
                p.write_bytes(b"x")
                os.utime(p, (1000 + i, 1000 + i))  # 递增 mtime
            with mock.patch.object(core, "PJSK_DIR", pjsk):
                gu.enforce_cap(3)
            left = sorted(p.name for p in pjsk.glob("*.png"))
            self.assertEqual(left, ["card_2.png", "card_3.png", "card_4.png"])

    def test_enforce_cap_noop_when_under(self):
        with tempfile.TemporaryDirectory() as td:
            pjsk = Path(td) / "pjsk"
            pjsk.mkdir()
            (pjsk / "card_0.png").write_bytes(b"x")
            with mock.patch.object(core, "PJSK_DIR", pjsk):
                gu.enforce_cap(10)
            self.assertEqual(len(list(pjsk.glob("*.png"))), 1)


if __name__ == "__main__":
    unittest.main()
