# pjsk-wallpaper

Project Sekai 卡面壁纸软件（桌面 + 锁屏，独立轮换，零网络常驻）

## 环境要求

- Windows 10 / 11
- Python 3.10+（安装时勾选 "Add to PATH"）

## 安装（推荐：安装助手）

1. 解压源码到任意临时位置。
2. 双击 **`安装助手.bat`**（或 `pythonw installer.pyw`）。
3. 按向导选择安装目录 → 安装 → 完成时可勾选「创建快捷方式」「开机自启」「立即启动」。

安装器会自动复制文件到目标目录、检测并安装 Pillow 依赖。
所有运行数据（图库、缓存、配置）都存放在安装目录内。

## 手动安装（备选）

```
pip install -r requirements.txt
pythonw wallpaper.pyw
```

## 目录结构

```
pjsk-wallpaper\
├── installer.pyw        安装助手（向导式：选目录 → 复制文件 → 装依赖 → 快捷方式）
├── 安装助手.bat          安装助手引导器（未装 Python 时给出提示）
├── wallpaper.pyw        GUI 主程序（tkinter，桌面/锁屏独立定时轮换，零网络）
├── gallery_update.py    图库更新脚本（独立：手动或按间隔自动触发）
├── core.py              核心逻辑（配置、壁纸设置 API、图库管理、快捷方式）
├── tray.py              系统托盘（纯 ctypes Shell_NotifyIcon，无第三方依赖）
├── set_lockscreen.ps1   锁屏壁纸设置脚本（WinRT API，无需管理员）
├── start.bat            启动器
├── requirements.txt     Python 依赖（Pillow）
├── icon.png / icon.ico  程序图标（首次运行自动生成）
├── config.json          配置（首次运行自动生成，见 .gitignore，不随仓库分发）
├── library/
│   ├── pjsk/            自动获取的 Project Sekai 卡面（FIFO，上限可配置）
│   └── custom/          手动添加的图片（不受上限约束）
├── cache/               数据库缓存（图库更新用，运行时自动生成）
└── logs/update.log      图库更新日志（运行时自动生成）
```

## 使用方式

1. 双击桌面快捷方式「pjsk 壁纸」启动 GUI（也可用开始菜单或 `start.bat`）。
2. GUI 中勾选"pjsk 图库"和/或"自加图片"作为壁纸来源。
3. 点击"立即更新"获取最新卡面；也可勾选"启用间隔更新"并设置分钟数，程序运行中会按
   该间隔自动更新。
4. 桌面和锁屏各自独立轮换，间隔可分别设置（单位：分钟）；各定时任务下方会显示
   上一次与下一次更新（切换）时间。

## 托盘与快捷方式

- **最小化到托盘**：点窗口 X 或最小化即隐藏到系统托盘（程序继续轮换）。
  托盘图标左键/双击 = 显示主窗口；右键菜单：打开主窗口 / 桌面下一张 / 锁屏下一张 / 退出。
  程序只能从托盘菜单的「退出」真正关闭。
- **快捷方式**：首次运行自动在桌面和开始菜单创建；被删后可在 GUI「其他」里点
  「创建桌面快捷方式」重建。快捷方式指向 pythonw.exe（无控制台窗口）。
- 轮换仅在程序运行时进行（托盘隐藏也算运行），建议配合「开机自启」使用。

## 架构说明

### 壁纸轮换（wallpaper.pyw）
- **桌面壁纸**：通过 `SystemParametersInfo`（`SPI_SETDESKWALLPAPER`）设置系统壁纸，切换时由 shell 播放约 1 秒的淡入过渡。
- **淡入过渡**：这是 shell 自身的行为，默认关闭，需向 `Progman` 发送 `0x052C` 开启（见 `core.enable_desktop_fade()`）。该状态保存在 explorer 内，重启电脑或 explorer 后失效，因此程序启动时以及**每次切换桌面壁纸前**都会补发一次，保证每次切换都带淡入。
- **锁屏壁纸**：调用 WinRT `LockScreen.SetImageFileAsync`（通过 PowerShell），即时切换。
- 两者完全独立，各自维护随机序列（一轮内不重复）。
- 不发起任何网络请求，只读取本地图片。

### 图库更新（gallery_update.py）
- 从 [sekai-master-db-diff](https://github.com/Sekai-World/sekai-master-db-diff) 拉取卡面数据。
- 支持按角色、组合、稀有度筛选。
- FIFO 驱逐最旧图片，保持 pjsk 目录不超过「其他」中设置的上限（默认 20 张，改动在
  下次更新时生效）。
- 触发方式：GUI 手动「立即更新」，或勾选「启用间隔更新」后由程序内计时器按间隔触发。
- 所有定时均为程序内定时：程序未运行时不会更新。

### 手动添加图片
- 在 GUI 中点击"添加图片"按钮选择本地图片。
- 图片会自动按主屏分辨率等比填充裁剪，存入 `library/custom/`。
- `custom/` 中的图片不受 FIFO 驱逐限制，永久保留。

## 技术细节

- 桌面壁纸通过 `SystemParametersInfo` 设置，无需管理员权限。
- 锁屏壁纸通过 WinRT `LockScreen.SetImageFileAsync` 设置，无需管理员权限。
- 图片统一按屏幕分辨率（首次运行自动检测，可在 `config.json` 的 `screen` 手动指定）
  等比填充裁剪，PNG 编码保存。
- 配置存储在 `config.json`，首次运行自动生成默认值。
- 图库更新使用断点续传，支持网络不稳定环境。

## 日志

- 图库更新日志：`logs/update.log`
- GUI 状态实时显示在窗口底部状态栏。

## 卸载

1. 关闭 GUI 程序。
2. 直接删除安装目录。
3. 如开启了开机自启，在启动项中删除 `pjsk-wallpaper`；
   快捷方式可在桌面和开始菜单中手动删除。
