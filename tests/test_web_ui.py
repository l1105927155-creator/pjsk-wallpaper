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

    # ------------------------------------------------- 预览与切换同步（过渡动画）

    def test_preview_has_buffer_layer(self):
        """交叉淡入需要两层：上层当前图、下层即将生效的新图。"""
        for prefix in ("desk", "lock"):
            self.assertIn(f'id="{prefix}ImgBuf"', self.html)
            self.assertIn(f'class="wall-buf"', self.html)
            self.assertIn(f'id="{prefix}Img" class="wall-cur', self.html)
            # 下层必须在上层之前（同为绝对定位，DOM 顺序决定叠放）
            self.assertLess(self.html.index(f'id="{prefix}ImgBuf"'),
                            self.html.index(f'id="{prefix}Img"'))
            self.assertIn(f'$(prefix + "ImgBuf")', self.js)

    def test_fade_duration_matches_between_css_and_js(self):
        """过渡时长必须一致，否则两处会各自漂移。"""
        css = re.search(r"--wall-fade:\s*(\d+)ms", self.css)
        js = re.search(r"WALL_FADE_MS\s*=\s*(\d+)", self.js)
        self.assertIsNotNone(css, "styles.css 缺少 --wall-fade")
        self.assertIsNotNone(js, "app.js 缺少 WALL_FADE_MS")
        self.assertEqual(css.group(1), js.group(1))

    def test_fade_rule_only_on_target_state(self):
        """transition 只写在 .fading 上：淡出走过渡，恢复不透明立即生效。"""
        self.assertRegex(self.css, r"\.wall-cur\.fading\s*\{[^}]*opacity:\s*0")
        self.assertRegex(self.css, r"\.wall-cur\.fading\s*\{[^}]*transition:[^}]*--wall-fade")
        self.assertNotRegex(self.css, r"\.wall-cur\s*\{[^}]*transition:[^}]*--wall-fade")

    def test_render_prefers_pending_then_current(self):
        """后端在写系统壁纸之前广播 pending，前端要优先用它，
        否则预览只能等切换完成后才更新（即「晚一步」）。"""
        self.assertIn("w.pending || w.current", self.js)
        self.assertIn("function setWall(prefix, url)", self.js)

    def test_preview_starts_immediately_without_delay(self):
        """前端不得对预览引入任何人为延迟：收到 pending 就开始过渡，
        与后端发起切换是同一时刻。曾经按实测补偿过一段提前量
        （preview_delay_ms），它是经验值、在不同机器上不稳定，已移除。"""
        self.assertNotIn("preview_delay_ms", self.js)
        self.assertNotIn("previewDelay", self.js)
        self.assertNotIn("PREVIEW_DELAY_MS", self.js)
        self.assertNotIn("delayMs", self.js)
        self.assertIn("setWall(prefix, imgUrl(show.id, 1024));", self.js)

    def test_preview_does_not_restart_when_switch_finishes(self):
        """切换完成后后端会再推一次状态（current 已是新图、pending 为空）。
        目标必须提前登记，否则那次推送会把同一次过渡再触发一遍。"""
        self.assertIn("cur.dataset.url = url;", self.js)   # 排定时即登记
        self.assertIn("cur.dataset.url = prevUrl;", self.js)  # 加载失败则撤销
        self.assertLess(self.js.index("var prevUrl = cur.dataset.url;"),
                        self.js.index("// 立刻登记目标"))

    def test_state_polling_is_long_poll(self):
        """轮询改为带修订号的长轮询：变化即返回，取代固定周期。"""
        self.assertIn('api("/api/state" + q)', self.js)
        self.assertIn('"?since=" + S.rev + "&wait=" + POLL_WAIT', self.js)
        self.assertNotIn("POLL_MS", self.js, "固定周期轮询应已被长轮询取代")


if __name__ == "__main__":
    unittest.main()
