# -*- coding: utf-8 -*-
"""Web UI 静态一致性检查：JS 引用的元素 id 必须存在、无外部 CDN 依赖、
响应式断点与令牌占位齐全、选项卡视图齐全。"""
import collections
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"


class WebUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP / "index.html").read_text("utf-8")
        cls.js = (APP / "app.js").read_text("utf-8")
        cls.css = (APP / "styles.css").read_text("utf-8")
        cls.ids = set(re.findall(r'\bid="([^"]+)"', cls.html))
        # id -> 标签名（用于检查事件是否绑到了显示元素上）
        cls.id_tags = {m.group(2): m.group(1)
                       for m in re.finditer(r'<(\w+)[^>]*?\bid="([^"]+)"', cls.html, re.S)}

    def test_ids_are_unique(self):
        """id 重复是致命问题：getElementById 只返回第一个，
        会把事件绑到错误的元素上（曾导致「下一张」按钮点了没反应）。"""
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        dup = [k for k, v in collections.Counter(ids).items() if v > 1]
        self.assertEqual(dup, [], f"index.html 存在重复 id: {dup}")

    def test_click_bound_ids_are_not_display_elements(self):
        """绑定了 click 的 id 不能落在纯显示元素上（<b>/<span>/<p>…），
        否则显示位会抢走 id，真正的按钮永远收不到点击。"""
        display_tags = {"b", "i", "em", "strong", "span", "small", "code",
                        "p", "dt", "dd", "h1", "h2", "h3", "bdi", "label"}
        bound = set(re.findall(
            r'\$\("([A-Za-z0-9_\-]+)"\)\.addEventListener\("click"', self.js))
        self.assertTrue(bound, "未解析到任何 click 绑定")
        for bid in sorted(bound):
            self.assertIn(bid, self.id_tags, f"{bid} 在 HTML 中不存在")
            self.assertNotIn(self.id_tags[bid], display_tags,
                             f"{bid} 绑到了 <{self.id_tags[bid]}>（显示元素）上")

    def test_next_buttons_are_real_buttons(self):
        """回归：deskNext/lockNext 必须是 <button>，且与显示位 deskNextTime/lockNextTime 不同名。"""
        for bid in ("deskNext", "lockNext"):
            self.assertEqual(self.id_tags.get(bid), "button", f"{bid} 不是 <button>")
            self.assertEqual(re.findall(rf'\bid="{bid}"', self.html).__len__(), 1)
            self.assertIn(f'$("{bid}").addEventListener("click"', self.js)

    def test_meta_display_ids_do_not_clash_with_buttons(self):
        """「下一次切换」显示位不得与「下一张」按钮重名。"""
        for prefix in ("desk", "lock"):
            self.assertIn(f'id="{prefix}NextTime"', self.html)
            self.assertIn(f'$(prefix + "NextTime")', self.js)

    def test_all_js_element_ids_exist(self):
        used = set(re.findall(r'\$\("([A-Za-z0-9_\-]+)"\)', self.js))
        missing = sorted(used - self.ids)
        self.assertEqual(missing, [], f"app.js 引用了 HTML 中不存在的 id: {missing}")

    def test_tab_views_exist(self):
        views = re.findall(r'data-view="([^"]+)"', self.html)
        self.assertEqual(sorted(views), ["gallery", "overview", "settings"])
        for v in views:
            self.assertIn(f'id="view-{v}"', self.html)

    def test_token_placeholder_present_once(self):
        self.assertIn("__PJSK_TOKEN__", self.html)
        self.assertEqual(self.html.count("__PJSK_TOKEN__"), 1)

    def test_no_external_resources(self):
        """离线可用：不得引用外部 CDN/字体/图片。"""
        for name, text in (("index.html", self.html), ("app.js", self.js),
                           ("styles.css", self.css)):
            for m in re.findall(r'https?://[^\s"\'\)]+', text):
                self.fail(f"{name} 含外部资源引用: {m}")
            self.assertNotIn("//cdn", text)
            self.assertNotIn("@import", text)

    def test_local_assets_referenced_exist(self):
        for m in re.findall(r'(?:href|src)="(assets/[^"]+)"', self.html):
            self.assertTrue((APP / m).is_file(), f"缺少静态资源 {m}")
        for m in re.findall(r'url\((["\']?)(assets/[^)"\']+)', self.css):
            pass  # 本样式表未使用 assets 背景图
        self.assertTrue((APP / "styles.css").is_file())
        self.assertTrue((APP / "app.js").is_file())

    def test_responsive_breakpoints(self):
        for w in ("900px", "620px", "420px"):
            self.assertIn(f"@media (max-width: {w})", self.css)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_key_controls_present(self):
        for need in ("deskNext", "lockNext", "btnUpdate", "btnUpdate2",
                     "fileInput", "dropzone", "galleryGrid", "pagePrev", "pageNext",
                     "setAutostart", "btnShortcuts", "setCap", "setUpChara",
                     "setUpUnit", "setUpRarity", "setUpCount", "lightbox", "logModal",
                     "btnExportConfig", "btnImportConfig", "configFileInput",
                     "configDropzone", "configImportStatus"):
            self.assertIn(f'id="{need}"', self.html, f"缺少控件 {need}")

    def test_config_transfer_controls(self):
        """导入：拖放 + 文件选择；导出：按钮；且只接受 .json。"""
        self.assertRegex(self.html, r'id="configFileInput"[^>]*accept="[^"]*\.json')
        self.assertIn('id="configDropzone"', self.html)
        self.assertIn("importConfigFile", self.js)
        self.assertIn("exportConfig", self.js)
        self.assertIn("FileReader", self.js)

    def test_all_new_endpoints_used(self):
        for ep in ("/api/state", "/api/config", "/api/wallpaper/desktop/next",
                   "/api/wallpaper/lock/next", "/api/gallery", "/api/images/",
                   "/api/gallery/custom", "/api/gallery/update",
                   "/api/gallery/update/log", "/api/system/autostart",
                   "/api/system/open-folder", "/api/system/create-shortcuts",
                   "/api/config/export", "/api/config/import"):
            self.assertIn(ep, self.js, f"前端未使用接口 {ep}")

    def test_no_absolute_paths_in_frontend(self):
        """前端不得拼接系统绝对路径。"""
        self.assertNotRegex(self.js, r"[A-Za-z]:\\\\")
        self.assertNotIn("file://", self.js)


if __name__ == "__main__":
    unittest.main()
