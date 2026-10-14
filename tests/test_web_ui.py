# -*- coding: utf-8 -*-
"""Web UI 静态一致性检查：JS 引用的元素 id 必须存在、无外部 CDN 依赖、
响应式断点与令牌占位齐全、选项卡视图齐全。"""
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

    def test_stale_page_hardening(self):
        """抗「页面停滞」的加固：超时、可见性恢复同步、看门狗、预览强制刷新。"""
        self.assertIn("AbortController", self.js)              # 请求超时
        self.assertIn("visibilitychange", self.js)             # 恢复可见立即同步
        self.assertIn('addEventListener("focus"', self.js)
        self.assertIn("状态未同步", self.js)                    # 看门狗提示
        self.assertIn("painted", self.js)                      # 预览按逻辑 id 强制刷新
        self.assertNotIn("if (document.hidden) { return; }   // 页面不可见时暂停轮询",
                         self.js)
        # 轮询不应再因页面隐藏而整段跳过
        self.assertNotIn("if (document.hidden) { return; }\n      loadState();", self.js)

    def test_next_uses_state_from_same_response(self):
        """点下一张必须用同一次响应里的 state 渲染，而不是依赖后续轮询。"""
        self.assertIn("b.state", self.js)
        self.assertIn("function applyState", self.js)

    def test_no_absolute_paths_in_frontend(self):
        """前端不得拼接系统绝对路径。"""
        self.assertNotRegex(self.js, r"[A-Za-z]:\\\\")
        self.assertNotIn("file://", self.js)


if __name__ == "__main__":
    unittest.main()
