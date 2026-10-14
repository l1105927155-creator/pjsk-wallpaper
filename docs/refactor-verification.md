# 重构验收对照表

对照 `docs/architecture-refactor.md` 与 `docs/ui-refactor.md` 的验收标准，逐项给出证据与状态。

**自动化证据**：`python -m unittest discover -s tests`（68 项，全绿）。

| 标记 | 含义 |
| --- | --- |
| ✅ | 已通过自动化测试或实机截图核对 |
| ⏳ | 需在 Windows 实机由人工确认（GUI/OS 级效果，本机无桌面会话无法代替） |

## 架构验收

| 验收项 | 证据 | 状态 |
| --- | --- | --- |
| 不启动 Tkinter 窗口即可查看/修改全部旧版配置并触发全部旧版操作 | 旧 `wallpaper.pyw` 已移除；`app/` 覆盖旧 GUI 全部字段与操作；`tests/test_web_ui.py` 校验控件与接口齐全 | ✅ |
| 页面关闭/刷新/崩溃后轮换与更新调度继续，重开显示真实状态 | 调度在 `runtime.py` 独立线程，与 HTTP 无关；`tests/test_runtime.py::SchedulerTest`；`state()` 为唯一状态来源 | ✅ |
| 桌面/锁屏独立性、轮次去重、淡入、图片处理、更新频率、FIFO 语义一致 | `tests/test_core.py`（Rotator 去重/重建、process_fill、import_image）、`tests/test_runtime.py::SwitchTest`、`tests/test_gallery_update.py::enforce_cap` | ✅ |
| 人工确认锁屏修改生效 | 需实机 Win+L 核对 | ⏳ |
| 多次启动只有一个后台调度实例；托盘退出后无残留服务/更新子进程 | 命名互斥体 + `/api/identity` 身份校验（`tests/test_launcher.py`）；`Runtime.stop()` 终止更新子进程 | ✅（实机重启场景 ⏳） |
| 断网时本地切换照常；更新失败有反馈且不破坏现有图库 | 切换只读本地图库（无网络调用）；`Runtime._run_update` 捕获非零退出并记录消息 | ✅（实机断网 ⏳） |
| 原 `config.json`、`library/` 迁移后可用，保留用户自定义图片/配置/缓存/日志 | `core.config_load` 缺键补齐（`test_partial_merge_keeps_user_and_fills_missing`）；安装器只新增/覆盖程序文件，不动数据目录 | ✅（升级安装 ⏳） |
| API 校验、跨站访问阻断、路径遍历拒绝、恶意/超限上传拒绝 | `tests/test_server.py`（403 令牌/Host/Origin、`resolve_image_id` 穿越拒绝、格式/体积校验） | ✅ |
| README 与安装流程与实际启动/退出行为一致 | 本次已重写 `README.md` 并同步 `installer.pyw`、`start.bat`、快捷方式/自启目标 | ✅ |

## UI 验收

| 验收项 | 证据 | 状态 |
| --- | --- | --- |
| 桌面/锁屏的预览、启停、间隔、下一张分别正确，不串用状态 | 两套独立卡片 + `state.desktop/lock`；`tests/test_runtime.py::test_desktop_and_lock_are_independent` | ✅ |
| pjsk/custom 来源选择、添加图片、图库数量、打开目录可用 | 图库页 + `POST /api/gallery/custom`、`POST /api/system/open-folder` | ✅（真实文件对话框上传 ⏳） |
| 更新筛选参数、数量、更新开关与间隔、立即更新及状态展示齐全 | 设置页「图库更新」；`state.update` 提供 running/last/next/message | ✅ |
| 开机自启、快捷方式、pjsk 上限与旧版状态/错误展示得到保留 | 设置页「系统」「图库来源」；`system_info` 暴露 autostart/shortcuts | ✅ |
| 安装时「开机自启」的选择与安装后界面显示一致 | 安装器与运行时共用 `win_startup`；未勾选时主动清除 Run 项；`tests/test_installer.py`（源码不变量 + `_finish` 流程 + 真实注册表临时值名读写） | ✅ |
| 页面刷新与重开不丢配置、不停止计时，状态从后端恢复 | 状态全部来自 `GET /api/state`；配置在服务端持久化 | ✅ |
| 1280/800/420 及 Windows 缩放环境无横向溢出或重叠，主要交互可操作 | 无头 Chrome 实机截图核对 1280×900、800、420（见 `docs/screens/`）；`styles.css` 三档断点 | ✅ |
| 无图库、更新失败、网络断开、任务繁忙、上传无效文件等异常有准确反馈 | 前端空态/错误 banner、busy 按钮、toast；后端 409/400/413 语义 | ✅ |
| 页面 JS/CSS/图片离线可加载，无外部 CDN 依赖 | `tests/test_web_ui.py::test_no_external_resources`、`test_local_assets_referenced_exist` | ✅ |
| 不改变原壁纸服务的随机/轮换/下载/持久化规则 | 复用 `core.Rotator`、`core.process_fill`、`gallery_update`；仅修复中文角色筛选的 int/str 比较缺陷（见下） | ✅ |

## 已知偏差与说明

1. **中文角色筛选缺陷修复**：`gallery_update.find_chara` 原在按 i18n 名字匹配时返回字符串 id，
   与 `card["characterId"]`（int）比较恒为假，导致输入「奏」等中文名筛选恒为空。
   本轮统一返回 `int`，属最小必要的缺陷修复，罗马音别名与数字 id 行为不变。已记入
   `docs/baseline-behavior.md`「已知问题」。

2. **配置保存时机**：旧版 `_tick` 每 20 秒整表回写 `config.json`；新版改为「变更即校验 + 原子写盘」，
   并按 `architecture-refactor.md` 要求取消周期性回写。字段与合法范围保持不变。

3. **切换请求语义**：旧版在已有切换线程进行中时仍会消耗一次轮换队列；新版在目标繁忙时直接返回
   `busy` 且不消耗队列（更符合「防重复点击」与 API 语义，对正常使用无可见差异）。

4. **无头环境限制**：本机无桌面会话，故「真实换壁纸 / 锁屏生效 / 托盘交互 / 开机自启 / 升级安装」
   依赖 Windows 实机人工核对；对应步骤见 `docs/baseline-behavior.md`「手工验收步骤」。
   其余逻辑均有自动化覆盖。

5. **安装向导自启项一致性修复**：旧实现在「开机自启动」未勾选时**不做任何事**，
   于是旧版本/上一次安装遗留的 `HKCU\...\Run\pjsk-wallpaper` 会被运行时如实读回，
   界面显示「已开启」，与安装时的选择不一致。
   现在自启键名/值名/写入逻辑统一收敛到 `win_startup.py`（安装器与 `core.py` 共用），
   并把「未勾选」定义为**主动清除**。相关回归用例见 `tests/test_installer.py`。

6. **同类留意项（未改动）**：安装向导「创建桌面和开始菜单快捷方式」未勾选时同样不动作，
   因此升级时若旧快捷方式指向已移除的 `wallpaper.pyw` 会失效。因涉及删除用户桌面上的
   快捷方式，本轮不擅自处理；如需「未勾选即清理旧快捷方式」的行为可再提需求。

7. **超出原文档范围、按用户要求新增的功能**（原 `docs/` 未要求，经用户确认后实施）：

   - **设置导入 / 导出**：`GET /api/config/export` 导出可迁移设置快照；
     `POST /api/config/import` 接受导出的信封**或原始 `config.json`**，复用 `apply_config`
     的同一套字段与范围校验，非法内容整体拒绝、不做部分写入。
     `screen`（本机分辨率）、`autostart`（本机自启）、`update.last_run`（运行时状态）
     被明确识别并忽略（导出时移入 `machine`，导入时给出说明），避免换机器后引入错误配置。
     界面支持拖入 / 选择文件导入，并回显「已导入 N 项」或逐条错误。
   - **图库目录未做可配置化**：按用户确认，保持程序目录下的相对路径
     `library/pjsk`、`library/custom` 不变，因此受控 ID 映射与路径穿越防护的边界不变。

   对应测试：`tests/test_runtime.py::ConfigImportExportTest`、`tests/test_server.py`、
   `tests/test_web_ui.py::test_config_transfer_controls`。

8. **「点下一张没有任何反应」的真正根因与修复**（用户实机反馈，两次迭代）：

   **根因（已确认并复现）**：`app/index.html` 里 **id 重复**——

   | 位置 | 元素 | 说明 |
   | --- | --- | --- |
   | 「下一次切换」显示位 | `<b id="deskNext">` | 在文档中**靠前** |
   | 「下一张」按钮 | `<button id="deskNext">` | 真正的按钮，靠后 |

   `document.getElementById("deskNext")` 只返回**第一个**（那个 `<b>`），于是
   `addEventListener("click", …)` 被绑到了 15px 高的文本元素上；
   用户点击真正的按钮时**没有任何事件处理器**，所以毫无反应（无请求、无提示），
   而服务端定时切换照常工作 —— 与用户描述完全一致。`lockNext` 同病。

   **修复**：显示位改名 `deskNextTime` / `lockNextTime`，`renderWall` 改用新 id；
   按钮仍为 `deskNext` / `lockNext`。

   **测试盲区（已补）**：旧用例只断言「JS 引用的 id 存在」，未校验唯一性；
   而当时的浏览器自检用 `element.click()`，**绕过了命中测试**，正好点中被错误绑定的
   `<b>`，于是“测试通过”。现在：
   - `tests/test_web_ui.py` 新增 `test_ids_are_unique`（页面级重复 id 检查）、
     `test_click_bound_ids_are_not_display_elements`（click 不得绑在 `<b>/<span>` 等显示元素上）、
     `test_next_buttons_are_real_buttons`（按钮必须是 `<button>` 且 id 唯一）；
   - `tools/ui_check.js` 改为**真实鼠标事件**（`Input.dispatchMouseEvent`）+
     `document.elementFromPoint` 命中校验 + 全页重复 id 扫描。
     在未修复的实例上运行会明确失败（可复现原 bug），在修复版上 14/14 通过。

   **同时保留的加固**（属另一类风险，非本次根因）：`next` 响应携带 `state` 直接渲染、
   可见性恢复立即同步、请求超时、预览 `&v=` 强制刷新、看门狗提示「状态未同步」。

   **顺带修复**：`core.thumbnail` 缓存键加入来源目录，避免 `pjsk/x.png` 与 `custom/x.png`
   同名互相覆盖。

   > 复盘：上一轮把症状归因为「窗口被遮挡导致页面节流/冻结」是**错误结论** ——
   > 依据是页面倒计时落后于真实时间，但那可以由「页面长时间未被操作」自然解释，
   > 且真实按钮的点击从未发出请求。教训：验证 UI 必须用真实输入事件，
   > 且 HTML 结构层面的不变量（id 唯一）要有断言。
