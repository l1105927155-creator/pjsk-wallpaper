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

8. **「点下一张没有任何反应」的根因与修复**（用户实机反馈）：

   **根因（已用真实鼠标点击复现）**：`app/index.html` 里 **id 重复**——

   | 位置 | 元素 | 说明 |
   | --- | --- | --- |
   | 「下一次切换」显示位 | `<b id="deskNext">` | 在文档中**靠前** |
   | 「下一张」按钮 | `<button id="deskNext">` | 真正的按钮，靠后 |

   `document.getElementById("deskNext")` 只返回**第一个**（那个 `<b>`），于是
   `addEventListener("click", …)` 被绑到了 15px 高的文本元素上；
   点击真正的按钮时**没有任何事件处理器**（不发请求、无提示、无变化），
   而服务端定时切换照常工作 —— 与用户描述完全一致。`lockNext` 同病。

   **修复（最小改动）**：只把显示位改名 `deskNextTime` / `lockNextTime`，
   `renderWall` 改用新 id；按钮仍为 `deskNext` / `lockNext`。

   **测试盲区（已补）**：
   - `tests/test_web_ui.py` 新增 `test_ids_are_unique`（页面级重复 id 检查）、
     `test_click_bound_ids_are_not_display_elements`（click 不得绑在 `<b>/<span>` 等显示元素上）、
     `test_next_buttons_are_real_buttons`（按钮必须是 `<button>` 且 id 唯一）；
   - `tools/ui_check.js`：用**真实鼠标事件**（`Input.dispatchMouseEvent`）+
     `document.elementFromPoint` 命中校验 + 全页重复 id 扫描。原实现用 JS 的
     `element.click()`，**绕过命中测试**，正好点中被错误绑定的 `<b>`，因此假通过 ——
     这是本轮漏掉根因的直接原因。未修复实例上运行会明确失败，修复版通过。

   > 复盘：中途曾把症状归因为「窗口被遮挡导致页面节流/冻结」并据此做了一批改动，
   > 后经用户指正与复现确认该结论**错误**（真实按钮的点击从未发出请求，
   > 倒计时落后可由页面长时间未被操作自然解释）。相关改动已整体回退，
   > 保持最小改动。教训：验证 UI 必须用真实输入事件；HTML 结构层面的不变量
   > （id 唯一）要有断言；诊断不确定时不要顺带做无关修改。


9. **预览与桌面背景切换同步 + 相同过渡动画**（用户反馈「预览总比桌面背景晚一步，
   且预览没有过渡动画」）：

   **根因（时序）**：切换一直是由后端**先写系统壁纸、再让前端知道**：
   - 定时切换：前端只能等下一次固定周期轮询（3s）才知道当前图变了；
   - 手动「下一张」：响应要等 `SystemParametersInfoW` 返回，前端再 `loadState()`。

   而 `core.set_desktop` 会阻塞到 shell 开始淡入，于是预览必然**整体滞后一整段过渡**；
   加上预览换 `src` 是硬切（无动画），观感就是「背景过渡结束、预览才跳一下」。

   **修复（保持最小改动，只动必要处）**：
   - `runtime.py`：新增状态**修订号 + 条件变量**（`revision()` / `wait_state()`）与
     `pending`（正在应用的目标）。`switch()` 在**写系统壁纸之前**就广播 `pending`
     并唤醒等待者，因此前端与系统**同一时刻**开始过渡；
     结束后清空 `pending`、`current` 收敛为新图。配置/更新/状态变化也一并递增修订号。
   - `runtime.py`：切换前**预热**该图的 1024px 预览缩略图（首次显示某张图时后端要
     现算，实测约 0.15–0.18s；若等前端来取才生成，预览仍会晚于桌面起步）。
     已缓存时该调用近乎零成本。
   - `server.py`：`GET /api/state` 支持 `?since=<rev>&wait=<秒>` 长轮询——
     不带 `since` 立即返回（首次/动作后），带 `since` 则挂起到修订号变化或超时。
     取代前端固定 3s 轮询。
   - `app/index.html` + `styles.css` + `app.js`：预览改为**双层**（上层当前图、
     下层即将生效的新图）。新图先整幅就位，再让上层按 `--wall-fade` 淡出把它
     「揭」出来 —— 这就是一次交叉过渡；角色不互换，`<prefix>Img` 始终代表当前壁纸。
     过渡结束后撤掉下层，不留重影。
   - 过渡时长 `--wall-fade`(CSS) 与 `WALL_FADE_MS`(JS) 必须一致（有断言），
     由已有的 `prefers-reduced-motion` 统一降级。

   **验收（`tools/ui_check.js`，真实 Chrome + 真实鼠标事件）**：
   - 预览切换**有过渡动画**：密集采样上层透明度，采到中间态（本次 8/40 次）且
     `fading` 类为真 —— 硬切不会出现中间态；
   - 过渡结束后 `visibleLayers === 1`（不留重影）；
   - **同步性**：MutationObserver 记录预览换图时刻、fetch 记录响应返回时刻，
     要求 `预览时刻 < 响应时刻`（本次 6275ms < 7060ms）。因为 `set_desktop`
     会阻塞到 shell 开始淡入，这条成立即证明预览不是等写系统壁纸返回后才补的。

   **对应测试**：`tests/test_runtime.py::StatePushTest`（广播早于写壁纸、
   失败也清 `pending`、修订号推进、超时返回）、
   `tests/test_server.py`（长轮询变化即返回 / 无变化超时 / 不带 `since` 立即返回 /
   非法 `since` 回退）、`tests/test_web_ui.py`（双层结构、时长一致、
   只对目标态加 `transition`、优先用 `pending`、已改为长轮询）。

   > 说明：锁屏切换本身没有系统级过渡，其预览仍使用同一淡入——
   > 否则同一页面上一个预览渐变、另一个硬切，观感不一致。
   > 浏览器解码/绘制与系统渲染的延迟差异不在本次范围内（用户已明确）。

10. **预览与桌面「看起来同时切换」**（用户反馈：点下一张时预览先切换、
    约半秒后壁纸才切换）：

   **先测清楚，再改。** 测量手段：`PrintWindow(Progman, PW_RENDERFULLCONTENT)`
   抓桌面窗口自身内容（不受其它窗口遮挡影响，实测可用），在切换过程中
   以约 15ms 间隔采样若干像素点，定位「桌面真正开始变」的时刻。
   8 次自动切换（图片 2.85–5.40 MB，均为真实卡面）：

   | 指标 | 最小 | 中位 | 最大 | 极差 |
   |---|---|---|---|---|
   | `set_desktop` 耗时 | 951 ms | 1041 ms | 1161 ms | 210 ms |
   | 桌面可见起点（点击后） | 375 ms | 496 ms | 605 ms | 230 ms |
   | 系统淡入时长 | 851 ms | 902 ms | 934 ms | 83 ms |

   （另测：`enable_desktop_fade()` 0.2 ms、缩略图缓存命中 0.1 ms、
   `set_desktop` 单独调用 725 ms。）

   **根因**：两件事要分开看。
   - **指令层面是同步的**：`switch()` 里广播 `pending` 与调用 `set_desktop`
     相隔 <1 ms（第 9 节的探测已验证）。
   - **落地层面做不到**：系统侧要先把 0.4–0.6 s 花在 `set_desktop` 里
     （写设置、让 explorer 解码新图），桌面才开始淡入；前端那条路只是
     「长轮询被唤醒 + 本机取已预热的缩略图 + 一次 CSS 过渡」，几毫秒即起步。
     于是预览整体领先约 0.5 s。第 9 节的改法把通知提前到写壁纸之前，
     必然导致这一侧的领先——两个方向只能选一个，不能两全。
   - **不存在可用的「系统开始变化」事件**：`SPI_GETDESKWALLPAPER` 在点击后
     **20 ms** 就翻转到新图（注册表值先写），远早于可见变化，不能当信号；
     桌面窗口的可见变化只能抓屏观测，没有事件可挂。故严格同步不可能，
     只能引入时间估计。
   - 顺带发现：系统淡入时长本身**非常稳定**（851–934 ms），而预览原用
     600 ms——这才是「相同的过渡动画」这条真正的偏差。

   **改动（两个常量 + 一处守卫，没有别的）**：
   - `runtime.py`：新增 `PREVIEW_DELAY_MS = 500`，随 `state()` 下发为
     `preview_delay_ms`。取值依据：实测起点中位 496 ms；起点/耗时比稳定在
     0.39–0.53，用固定 0.5 s 的残留误差 ≤0.13 s（原偏差 0.5–0.6 s）。
   - `app/styles.css` + `app/app.js`：`--wall-fade` / `WALL_FADE_MS`
     600 → **900**（照实测的系统淡入），两者仍由测试断言必须一致。
   - `app/app.js`：`pending` 触发的预览过渡推迟一个提前量才起步。
     **只用于桌面**——锁屏切换时看不到锁屏，没有可见的系统过渡可对齐。
     - 关键细节一：**立刻取缓冲图、到点才淡出**（按「排定时刻」等，
       而不是「加载完再等」）。否则起步会被取图耗时（约 0.17 s）往后推。
     - 关键细节二：排定过渡时**即登记** `cur.dataset.url`。切换完成后后端会
       再推一次状态（那次 `current` 已是新图、`pending` 为空），没有这个标记
       就会在延迟窗口内把过渡提前重来一遍；加载失败则撤销登记。

   **验收（`tools/ui_check.js`，18/18 通过）**：
   - 预览起步于**收到通知后 507 ms**（提前量 500 ms）。只量前端负责的那一段：
     点击到通知之间还有后端预热缩略图的耗时（新图首次上屏要现算约 0.17 s），
     那部分会把系统与预览**一起**推迟，不算错位。
   - 过渡时长实测 873–936 ms（与系统 851–934 ms 同量级）——旧的 600 ms
     会在这里被挡下。
   - 过渡只开始 **1** 次（最终状态推送没有把它重来一遍）。
   - 预览仍在「下一张」响应返回之前开始换图（同步推进，非事后补）。
   - 过渡结束后 `visibleLayers === 1`（不留重影）。

   **预期效果**：预览 0.5 → 1.4 s 淡入，桌面约 0.4–0.6 s 起、约 0.9 s 淡完，
   两者起点对齐，残留误差 ≤0.13 s。

   > 0.5 s 与 0.9 s 都是**本机实测值**。换机器、explorer 繁忙或图片更大时
   > 会漂移；但起点与 `set_desktop` 耗时是同向变化的（比值 0.39–0.53），
   > 因此漂移不会差一个量级。若日后要更准，可考虑按上一轮耗时自适应，
   > 实测精度只从 ±0.13 s 提到 ±0.08 s，不值当。
