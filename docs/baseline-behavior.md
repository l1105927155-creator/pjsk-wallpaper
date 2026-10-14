# 旧版行为基线（重构前，供迁移对照）

本文记录重构开始前 `wallpaper.pyw`(Tkinter) 版本的**实际**行为，作为回归对照。
来源：源码逐项核对 + `tests/` 自动化单测（22 项，全绿）。

## 数据与目录

| 项 | 位置 | 说明 |
| --- | --- | --- |
| 配置 | `config.json`（`APP_DIR`） | 首次运行/缺失/损坏时用 `DEFAULT_CONFIG` 合并补齐；`screen` 为 `null` 时自动探测主屏 |
| pjsk 图库 | `library/pjsk/*.png` | 更新脚本写入，FIFO 上限由 `cap_pjsk` 控制 |
| custom 图库 | `library/custom/*.png` | 手动添加，**不受** FIFO 上限约束 |
| 缓存 | `cache/`（图库更新 HTTP 缓存，TTL 24h） | 仅更新脚本使用 |
| 日志 | `logs/update.log` | 仅图库更新写入 |

`.gitignore` 忽略 `config.json / library/ / cache/ / logs/ / runtime/`——升级不得清空或移动这些目录。

## 配置字段与合法范围（旧版）

- `desktop.enabled` / `desktop.interval`（分钟，Spinbox 1–1440，默认 30）
- `lock.enabled` / `lock.interval`（分钟，1–1440，默认 60）
- `use_pjsk`（默认 true）/ `use_custom`（默认 false）
- `cap_pjsk`（1–9999，默认 20，FIFO 下次更新生效）
- `update`: `count`(1–10, 默认3)、`region`("jp")、`trained`(true)、`chara`、`unit`、`min_rarity`(1–4, 默认3)、`interval_enabled`(false)、`interval`(1–10080, 默认360)、`last_run`
- `autostart`、`screen`

## 轮换与调度（关键语义）

- **每 20 秒一个 tick**：先保存配置，再检查三类定时，再刷新时间标签。
- **桌面 / 锁屏完全独立**：各自的 `Rotator`（各自随机队列）、各自计时器 `desk_timer`/`lock_timer`、各自间隔；互不重置。
- **一轮内不重复**：`Rotator.next()` 按文件名快照重建队列，`rng.shuffle` 后逐个弹出；图库内容变化时重建队列。
- 定时判定：`timer is None 或 已过 interval*60 秒` 且 `enabled` 时触发一次切换。手动「下一张」会把对应计时器重置为当前时间（影响下次定时触发）。
- **间隔更新**：`up_anchor` 为进程内锚点，进程未运行时不补算；关闭开关期间每次都把锚点重置为 now（重新启用后从头计时）。
- **所有定时都是进程内定时**：程序不运行时不会触发。
- 切换在线程中执行，同一目标有 in-flight 线程时跳过（避免重入）。切换完成/失败通过 `self.after(0,...)` 回主线程更新状态栏。

## 壁纸设置

- 桌面：`SystemParametersInfoW(SPI_SETDESKWALLPAPER, ...)`；先写注册表 `WallpaperStyle=10`(填充)/`TileWallpaper=0`；无需管理员。
- 淡入：`enable_desktop_fade()` 在启动时及**每次切换前**向 `Progman` 发 `0x052C`（状态在 explorer 内，重启失效）。
- 锁屏：`core.set_lockscreen()` 经 `set_lockscreen.ps1` 调 WinRT `LockScreen.SetImageFileAsync`，`CREATE_NO_WINDOW`，`mbcs` 解码输出，30s 超时。
- 图片统一按 `screen` 等比填充 + 居中裁剪存 PNG（`process_fill`）。

## 图库更新

- `subprocess.run([sys.executable, "-X","utf8", gallery_update.py])`，超时 1200s；`_updating` 互斥，重复触发提示「更新已在进行中」。
- 更新开始即写 `last_run` 并把 `up_anchor` 归位；按钮禁用、显示「更新中…」；结束后按 returncode 显示「完成: <末行>」或「失败: <stderr 末200字>」。
- FIFO 驱逐按 **mtime 升序**删最旧，保持 `library/pjsk` 数量 ≤ `cap_pjsk`。
- 触发：手动「立即更新」或（开关开启时）间隔到期。

## 托盘 / 生命周期

- 关窗或最小化 → `withdraw()` 隐藏到托盘（保存配置），进程继续轮换。
- 托盘菜单：打开主窗口 / 桌面下一张 / 锁屏下一张 / 退出；左键/双击=显示。
- 只有托盘「退出」才真正结束；退出时停轮换、`NIM_DELETE` 托盘图标、存配置、destroy。
- 单实例：命名互斥体 `pjsk-wallpaper-mutex`；已存在则向托盘窗口发送 `WM_COMMAND(1)` 唤起主窗口后本进程退出。

## 自启与快捷方式

- 自启：HKCU `...\CurrentVersion\Run` 值 `pjsk-wallpaper` = `"<pythonw>" "<APP_DIR>/wallpaper.pyw"`。
- 快捷方式：桌面 + 开始菜单 `pjsk 壁纸.lnk`，目标 `pythonw.exe`，参数 `wallpaper.pyw`，图标 `icon.ico`。

## 已知问题（重构中处理，需评审）

1. **中文角色筛选静默失效（已修复）**：`gallery_update.find_chara()` 从 i18n `character_name.json`（键为字符串）匹配时返回 **字符串** id，而 `pick_candidates` 用 `card["characterId"]`(int) `== cid`(str) 比较恒为假 → 中文名（如「奏」）筛选得到空池，报「筛选条件下没有卡片」。罗马音别名（如 `kanade`）与纯数字 id 走另一分支返回 int，正常。
   - 影响：`ui-refactor.md` 要求保留「角色/组合筛选」，旧版该输入实际不可用。
   - 处理：阶段2 以最小改动修复（统一返回 `int(cid)`），保留罗马音别名与数字 id 行为；单测改为断言正确行为。

2. **配置每 20 秒整表回写**：`_tick` 每次都 `config_save`，且 `_collect_config` 用 UI 值整体覆盖。Web 化后改为「变更即校验保存」，不再周期性回写（`architecture-refactor.md` 明确要求）。

## 手工验收步骤（Windows 实机，重构前后各跑一次）

> 说明：本环境无 Tkinter/GUI，无可视化桌面，故 OS 级行为在真机人工核对；自动化只覆盖纯逻辑。

- [ ] A. 桌面切换：点「下一张」→ 壁纸立即更换且带约 1s 淡入；状态栏显示文件名。
- [ ] B. 锁屏切换：点锁屏「下一张」→ 锁屏图更换（Win+L 核对）。
- [ ] C. 独立性：桌面间隔设 1 分钟、锁屏设 5 分钟，观察桌面按其自身节奏切换，不被锁屏重置。
- [ ] D. 一轮去重：图库 5 张时连续切 5 次，5 张各不相同。
- [ ] E. 关窗=托盘：点 X → 窗口隐藏，托盘图标在，壁纸仍按间隔切换；托盘「退出」后停止。
- [ ] F. 单实例：重复双击快捷方式，只唤起已开窗口，任务管理器无第二个 pythonw 常驻。
- [ ] G. 立即更新：点「立即更新」→ 按钮禁用、显示「更新中…」，完成后显示新增尾行，pjsk 计数增加且 ≤ 上限。
- [ ] H. 无网络更新：断网后「立即更新」→ 显示失败信息，**现有图库不受损**；本地切换照常。
- [ ] I. 旧数据兼容：用旧 `config.json` + 现有 `library/` 启动，配置值与图片保留。
- [ ] J. 自启与快捷方式：勾选自启后重启系统生效；删除快捷方式后可重建。
