# 特斯拉哨兵云盘 · Tesla Sentry Drive

**虚拟哨兵 U 盘 · 多路同步回放 · SEI 行车遥测 · 水印导出 —— 跑在任意 USB Gadget 设备上。**

> 拿一台带 USB 口的小 Linux 设备（随身 WiFi、开发板、电视盒子、OpenWrt 路由器……）顶替原车 U 盘插进手套箱：车机照常录制，手机 / 电脑远程回看四路、六路画面，还能逐帧读出**视频码流里的 SEI 遥测**——挡位、车速、油门 / 刹车、方向盘角度、转向灯、AP 状态、坐标，导出视频时可一并烧成水印。
>
> 没有 USB Device 模式的机器（绝大多数 x86 / NAS / 软路由）也不会白跑：把 `TeslaCam/` 放到本机跑 `TSF_ARCHIVE=/path python3 sentry-viewer.py`，就是一个纯网页查看器。**A 完整部署 / B 半自动归档 / C 仅网页**，三档按设备能力裁剪，见 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)。

> 仓库名 `tesla-sentry-f50` 来自参考硬件**中兴 F50 存储版**（全功能实测机型），但项目本身不绑机型。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-aarch64%20%7C%20armv7%20%7C%20x86-blue.svg)](#硬件要求)
[![Python](https://img.shields.io/badge/python-3.7%2B-3776AB.svg)](https://www.python.org/)
[![Deps](https://img.shields.io/badge/deps-%E9%9B%B6%E7%AC%AC%E4%B8%89%E6%96%B9%E5%BA%93-brightgreen.svg)](#硬件要求)

---

## 它是什么

特斯拉的哨兵模式会把摄像头告警片段写入手套箱里的 U 盘（Model 3/Y 为 4 路：前视/后视/左右 B 柱；Model S/X 及 HW4 车型为 6 路）（`TeslaCam/SentryClips/`）。官方 App 不能远程看这些视频，必须拔盘插电脑——很麻烦。

这个项目让一台小 Linux 设备**伪装成那个 U 盘**：

```
┌──────────────┐   USB(供电+数据)   ┌────────────────────────────────┐
│   特斯拉车机  │ ◄───────────────► │  小 Linux 设备 (如树莓派/盒子)  │
└──────────────┘                   │                                │
                                   │  /opt/tesla-sentry/            │
   车机以为在写普通 U 盘             │   teslacam.img  ← 虚拟U盘镜像   │
                                   │        │  空闲时归档            │
                                   │        ▼                       │
                                   │   /archive (ext4)              │
                                   │        │                       │
                                   │        ▼                       │
                                   │   sentry-viewer.py :8080       │
                                   └────────────────────────────────┘
                                               ▲
                                    手机/电脑浏览器访问
```

**核心思路**：镜像文件（exFAT）通过 USB Gadget 的 `mass_storage` 功能导出给车机，车机以为那是真实 U 盘；Linux 侧在车闲置时短暂断开导出、挂载镜像、把新片段拷贝到独立的 ext4 归档区，再恢复导出。网页只读归档区，因此**永远不会和车机同时读写同一个 exFAT**。

---

## 特性

| 功能 | 说明 |
|---|---|
| 🗂 **虚拟 U 盘** | 容量任意（默认 64G），稀疏镜像不占实际空间，随写入增长 |
| 🔌 **车机零改造** | 插上就当普通 U 盘，车机会提示"格式化 USB 设备"，点确认即可 |
| 📦 **自动归档** | 镜像空闲 N 秒后自动把哨兵/保存/行车片段拷到 ext4 归档区，断电不丢 |
| 🔁 **归档滚动窗口** | 盒子侧 `RecentClips` 副本跟随车机的滚动缓冲，只留最近 60 分钟（可调），更旧的自动覆盖；车机 U 盘那份不受影响（同步全程只读），`SentryClips`/`SavedClips` 永不自动删。停车开哨兵时车整夜不睡，4 路约 8.4GB/小时，靠这个才不会撑爆盒子镜像 |
| ⚡ **实时查看（车机无感）** | 不挂载镜像，直接按字节解析 exFAT，车机一边写、网页十几秒内就能看到新片段；USB 全程不断开 |
| 🎬 **多路合成导出** | 勾选任意几路 → **浏览器本机实时合成一个多画面视频**（2×2/3×2 布局，webm 为主、Safari 为 mp4），设备只出文件流不参与转码；单路直接下载原文件，也可打包 zip；**下载/打包zip可选清晰度**（原画/720p/480p/360p，默认跟随播放清晰度）
| 🖼 **静态缩略图** | 服务端 ffmpeg 抽帧 + eMMC 缓存，列表秒开；事件内每分钟一组片段自动分组，默认播最新一组，可切换
| 🎬 **多路同步播放** | 按事件实际路数自适应布局（4 路 2×2、6 路 3×2），带摄像头名称，帧级同步校正、统一进度条、0.5x–4x 倍速 |
| 📊 **行车遥测（SEI 直读）** | 解析视频码流里特斯拉内嵌的 SEI 元数据（protobuf），播放页顶部面板实时显示：挡位 / 车速 / 油门% / 刹车 / 方向盘角度 / 转向灯 / AP 状态 / 坐标 / 三轴加速度，随进度逐帧跳动；不依赖网络与云端账号，数据永远跟着视频文件走
| 🗺 **事件元数据** | 读取车机 `event.json`：时间 / 城市 / 街道 / 坐标 / 触发原因 / 触发画面 / 活动 ID，列表卡片与播放器同步展示 |
| 🔍 **按触发原因筛选** | 手动保存、按喇叭、点击图标、物体感知⋯⋯分类下按原因一键筛选，带数量；分类切换自动重置 |
| 📅 **按日期筛选** | 触发原因下方再列一行日期胶囊（当天有事件才出现），可与原因筛选叠加；支持 `?date=2026-11-03` 深链 |
| 📶 **在线清晰度切换** | 播放器可选 原画 / 720p / 480p / 360p，低码率转码缓存（360p 约为原片 1/10 体积）；公网/弱网访问默认 360p，内网默认原画；首次切换需等待十几秒转码，之后秒开 |
| 💧 **导出可烧录水印** | 导出时勾选"叠加行车信息水印"，把时间/位置/车速/挡位/油门/刹车以**半透明单行横条**烧录到画面顶部（约占画面高 5%，基本不挡路况），单路/多路都支持 |
| 🎨 **网页背景** | 上传 CSS / 背景图，即刻换肤 |
| 🌗 **深色 / 浅色 / 自动** | 整套界面用 CSS 变量做成双主题，右上角一键循环切换（自动模式跟随系统），播放器子页面同步换肤；只有视频画面区域保持恒定深色 |
| 🔄 **一键在线升级（OTA）** | 设置 →「更新」页签：自动比对 GitHub 仓库里的 `version.json`，有新版本时齿轮亮红点，点一下即下载新程序、语法+启动自检、备份旧版、原子替换并自动重启；出问题可一键回滚。默认从本仓库的 `codex/modular-runtime` 分支获取程序，可用 `TSF_OTA_SOURCES` 指定局域网源；离线安装默认关闭在线检查 |
| 🔐 **全站登录保护** | 账号密码登录（cookie 记住 30 天），页面/视频流/缩略图/下载/API 全部拦截；支持 HTTP Basic 便于脚本；同 IP 连错 8 次锁 5 分钟；右上角一键退出 |
| 🔔 **飞书推送** | 哨兵触发后自动把时间 / 位置 / 触发原因 / 触发镜头 + **触发那一帧画面** 推到飞书（默认**自建应用私聊，只有本人可见**；也可用群机器人 webhook），可推**触发那一路的短视频**或封面卡片，带「查看录像」按钮 |
| 🔔 **QQ 官方机器人推送** | AppID / AppSecret 配置单聊或群聊，支持文字、封面及视频；本地媒体直接上传 QQ，无需公网服务 |
| 🔊 **锁车音自动转换** | MP3/WAV/M4A 等音频自动裁剪、转换 PCM WAV、命名为 `LockChime.wav`，同步写入 USB 根目录 |
| 🚙 **涂装管理** | 校验车型模板 PNG，自动创建 `Wraps/`、规范文件名，最多 10 个；支持预览、替换、删除及恢复导出 |
| 📂 **U 盘文件管理** | 网页直接浏览/上传/下载/删除车机 U 盘内容（临时断开、自动恢复导出） |
| 🛡 **断电自愈** | 车内 USB 硬断电场景友好：归档区 ext4 日志式、开机自检、服务自动拉起 |
| 🔁 **弹出自动重挂** | 主机(车机/电脑)对 U 盘执行"弹出"会清空 mass_storage 的 LUN，`usb-selfheal.sh` 每 2 分钟检测并自动重新导出 |
| 🚀 **开机自启** | OpenWrt `procd` 服务，USB 网卡 / 串口 / 虚拟 U 盘可共存 |

---

## 硬件要求

| 项目 | 要求 |
|---|---|
| 设备 | 支持 **USB Device (Gadget)** 模式的 Linux 设备（USB 口能当"设备"被主机识别） |
| 内核 | `CONFIG_USB_F_MASS_STORAGE=y`（configfs 方式）或 `g_mass_storage` 模块 |
| 文件系统 | 内核支持 `exfat`（`/proc/filesystems` 里有 `exfat`） |
| 存储 | ≥ 32G（建议 128G+，镜像 + 归档各占一部分） |
| 系统 | OpenWrt（procd）、systemd 系发行版，或任意带 `rc.local` 的 Linux（已在 **OpenWrt 25.12.5 / 主线内核 7.2.9** 验证） |
| Python | 3.7+，**查看器零第三方依赖**（纯标准库） |

> 本项目在**中兴 F50 存储版**（紫光展锐 T760、8 核、eMMC 256G、USB 3.0、无电池纯 USB 供电）上完整验证。

**先自检再动手**：在候选设备上跑 `sh scripts/tsf-check.sh`，它会逐项告出 UDC / configfs / exFAT / Python / init 系统是否满足，并给出结论。完整设备清单（随身 WiFi、开发板、电视盒子、x86 为何不行、各系统适配度、功能降级矩阵）见 **[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)**。

> 没有 USB Device 模式的机器（绝大多数 x86、NAS、软路由）跑不了"虚拟 U 盘"，但仍可作为**网页查看器**使用：把车机 U 盘或已归档的 `TeslaCam/` 放到本机，`TSF_ARCHIVE=/path python3 sentry-viewer.py` 即可。

---

## 快速开始

F50 访问不了 GitHub 时，使用[离线部署脚本](docs/DEPLOYMENT.md)：在 Mac 上运行 `sh tools/deploy_f50.sh root@设备IP`，即可上传并后台启动安装。设备无需 git、SFTP 或访问 GitHub。

### 1. 部署（在设备 SSH 里执行）

```sh
git clone --branch codex/modular-runtime --single-branch https://github.com/TLS853/tesla-sentry-f50.git
cd tesla-sentry-f50
sudo ./install.sh              # 默认 64G 镜像 + 归档区 /archive
```

装之前可以先看这台设备够不够格（不改动任何东西）：

```sh
sh scripts/tsf-check.sh
```

自定义参数：

```sh
sudo ./install.sh --size 200 --port 8080 --archive /archive
```

安装脚本会做这些事：

1. 检查内核 gadget / exFAT / Python 依赖，缺什么直接报错退出
2. 创建稀疏镜像 `teslacam.img`
3. 安装脚本到 `/opt/tesla-sentry/`，配置到 `/etc/tesla-sentry.conf`
4. 注册三个 OpenWrt 服务：`tesla-gadget`（虚拟U盘）、`sentry-viewer`（网页）、cron（归档）
5. 立即启动，并打印当前状态

### 2. 插到车上

把设备插进手套箱 USB 口 → 车机屏幕提示"**格式化 USB 设备**" → 点确认。

车机会自己建好 `TeslaCam/{SentryClips,SavedClips,RecentClips}` 目录，之后哨兵事件自动写入。

> 也可以在电脑上手动把这块盘格式化成 exFAT（卷标随意）。

### 3. 打开网页

浏览器访问 `http://<设备IP>:8080`：

- 手机连设备热点，或
- 设备 USB 网卡直连电脑，或
- 自行加 Tailscale / frp 等实现公网访问（**本项目不包含内网穿透**）

归档任务每 10 分钟检查一次，镜像空闲 10 分钟才动手——所以录完约 10–20 分钟后视频出现在网页上。

---

## 网页功能

### 主界面
- 三个分类：**哨兵告警 / 手动保存 / 行车记录**，带事件计数
- 每张卡片按事件实际路数显示拼图缩略，按时间线倒序
- 点开进入**多路同步播放器**：4 路自动 2×2、6 路自动 3×2，每格标注摄像头名称；帧级同步（偏差 > 0.35s 自动对齐）、统一进度条、倍速
- 支持 `?ev=SentryClips/2026-11-04_12-30-00` 深度链接直接打开某个事件

### ⚙ 个性化面板

| 标签 | 功能 |
|---|---|
| 🎨 网页背景 | 上传 `.css` / `.jpg` / `.png` / `.webp`；CSS 里用 `/skin-media/文件名` 引用图片 |
| 🔊 锁车语音 | 源音频最大 20 MiB，默认取前 5 秒，自动生成小于 1 MB 的 `LockChime.wav`，等待空闲同步写入 USB 根目录 |
| 🚙 涂装 | 上传对应车型 PNG（512–1024 正方形、≤1 MB），自动创建根目录 `Wraps/`，车机端选择；支持预览与删除 |
| 📂 文件 | **车机U盘**：浏览/上传/下载/删除 U 盘任意目录（含根目录）；**归档区**：浏览 F50 内部文件 |
| 🔔 推送 | 选择 QQ 官方机器人或飞书，推送新增事件；见 [QQ 配置及车辆文件使用说明](docs/FEATURES.md) 和 [飞书推送](#飞书推送) |
| 🌐 **多语言** | 界面支持中/英/德/法/意/西/荷/俄/日/韩，设置 → 语言 里切换，保存在本机浏览器 |
| 🔄 更新 | 查看当前/最新版本与更新说明，一键检查并升级，出问题可回滚（见 [在线升级](#在线升级ota)） |

**锁车音转换**：项目自动输出 44.1kHz / 16-bit / 单声道 PCM WAV，最长 5 秒、小于 1 MB，保存为 USB 根目录的 `LockChime.wav`。这是本项目采用的输出配置；车机端在 Toybox → Boombox → Lock Sound 选择 USB，需要车辆支持该功能。详见 [使用说明](docs/FEATURES.md)。

**U 盘文件管理模式**：点「连接车机U盘」→ 停止导出、挂载镜像 → 网页上改动直接写进车机 U 盘 → 点「恢复车机导出」或 **5 分钟无操作自动恢复**。行车/哨兵活跃时不要长时间挂着。

---

## 配置

配置文件：`/etc/tesla-sentry.conf`

```sh
TSF_DIR=/opt/tesla-sentry        # 安装目录
TSF_IMG=/opt/tesla-sentry/teslacam.img   # 虚拟U盘镜像
TSF_ARCHIVE=/archive             # 归档区 (ext4, 建议独立于镜像)
TSF_MNT=/tmp/teslamnt            # 临时挂载点
TSF_LOOP=/dev/loop3              # 挂载用 loop 设备
TSF_PORT=8080                    # 网页端口
TSF_IDLE=600                     # 镜像空闲多少秒才归档
TSF_LIVE_INTERVAL=15             # 在线只读同步轮询间隔 (秒)

# 归档滚动窗口: 盒子侧的 RecentClips 副本跟随车机的滚动缓冲 (车机自己只留最近 ~1 小时),
# 只保留最近 N 分钟的片段, 更旧的从盒子归档里删掉; 车机 U 盘那份不受影响 (同步为只读);
# 停车开哨兵时车整夜不睡, 4 路约 8.4GB/小时, 不开滚动窗口很快就撑爆盒子镜像
TSF_RECENT_ROLL_MIN=60           # 滚动窗口 (分钟), 0 = 关闭滚动、永久归档
                                 # 注意: tesla-live-sync.py 与 sentry-viewer.py 都读这个变量,
                                 # 两边必须一致 —— 同步端在"抄之前"就按窗口过滤, 查看器端负责清掉
                                 # 已在归档里的旧副本; 若只配查看器, 清掉的片段会被当成新片段重抄,
                                 # 形成 删除→重抄 的死循环, 新片段反而迟迟不落地

# 空间兜底: 万一别的数据把盘吃满, 剩余低于 FREE_MIN 时再淘汰最旧的 RecentClips,
# 清到 FREE_TARGET 为止 (哨兵/手动保存的归档由下面的定期清理管, 不参与兜底)
TSF_FREE_MIN_MB=4096             # 触发阈值 (MB)
TSF_FREE_TARGET_MB=10240         # 清理目标 (MB)
TSF_RECENT_KEEP_MIN=60           # 兜底清理时最近 N 分钟的片段无论如何保留

# 盒子侧哨兵/手动保存归档定期清理: 哨兵事件 + 手动保存的录像只增不删, 盘迟早被吃满,
# 这里只清盒子归档副本, 车机 U 盘那份不受影响。两个维度独立生效:
#   按天数 —— 只保留该分类最新事件往前 N 天内的归档 (锚点是最新事件而非当前时间,
#             停车几天没新事件不会被误清空); 按体积 —— 单个分类超过 MAX_GB 就从最旧的删
TSF_BOX_KEEP_DAYS=14             # 保留天数, 0 = 不按天数清
TSF_BOX_MAX_GB=12                # 单个分类体积上限 (GB), 0 = 不按体积清
                                 # 提示: 12G/类按 64G 镜像 + 常见存储空间设计;
                                 # 想多留就调大, 但注意剩余空间, 兜底清理不覆盖这两个分类

# 在线升级: 默认从自己的重构分支读取; 离线安装会将 ENABLED 设置为 0
TSF_OTA_REPO=TLS853/tesla-sentry-f50
TSF_OTA_REF=codex/modular-runtime
TSF_OTA_ENABLED=1
# TSF_OTA_SOURCES=http://Mac的局域网IP:8765
# TSF_USB_LOCK=/tmp/tesla-sentry-usb.lock  # 所有访问镜像的进程必须使用同一锁路径
```

### 在线升级（OTA）

网页右上角 **⚙ → 🔄 更新**：

- 打开设置即自动比对仓库里的 `version.json`；有新版本时齿轮按钮上会亮红点
- 页面显示「当前版本 / 最新版本 / 更新说明」，点 **立即升级** 完成：下载完整程序包 → 校验 SHA-256、文件清单和版本 → 两个 Python 入口自检 → 原子切换版本目录 → 重启查看器和同步服务（约 5 秒，页面自动刷新回来）
- 万一新版有问题，同一页有 **↩ 回滚到上一版本**
- 配置（`/etc/tesla-sentry.conf`）、`auth.conf`、录像归档都不在升级范围内，不会被动

发新版本的流程（维护者）：

```sh
python3 tools/make_release.py 2.6.2 "新增 XXX" "修复 YYY"   # 更新源码版本并构建脚本、整包、清单
git add -A && git commit -m "release: v2.6.2" && git push
```

> 若仓库改到别处，记得同时改 `APP_VERSION` 所在的 `OTA_REPO` 或 `TSF_OTA_SOURCES`。
> 设备没网 / GitHub 被墙时检查会失败，但**已安装的版本照常运行**，不影响看录像。

### 开发与构建

源码是可直接导入的 Python 包 `src/tesla_sentry/`，前端资源放在 `src/tesla_sentry/web/`。两个根目录 Python 文件是构建产物，设备仍只依赖 Python 3.7+ 标准库。

```sh
# 从源码启动，方便调试；只查看归档时不需要 USB 硬件
PYTHONPATH=src python3 -m tesla_sentry serve --host 127.0.0.1 --port 8080 --archive /path/to/archive
PYTHONPATH=src python3 -m tesla_sentry check
PYTHONPATH=src python3 -m tesla_sentry sync --once

# 生成并核对可独立部署的入口和完整程序包
python3 tools/build.py
python3 tools/build.py --check
python3 -m unittest discover -s tests -v
# 可选：前端模块测试，无需 npm 依赖
node --experimental-vm-modules tests/frontend.mjs
```

构建器将原始模块、HTML、CSS、JavaScript 和翻译表压缩进独立入口。运行时保持每个模块自己的命名空间和显式导入，开发与设备部署使用相同的包。浏览器使用原生 ES 模块，不需要 Node.js 构建工具。[模块与依赖说明](docs/MODULES.md) 记录了各层职责和兼容边界。

`config.py` 统一读取配置：环境变量优先于 `TSF_CONF`（默认 `/etc/tesla-sentry.conf`），再使用默认值。配置文件采用 `TSF_NAME=value`，含空格的值须加引号；Python 入口不会执行文件中的 Shell 命令。启动时校验端口、轮询间隔和并发限制，错误配置会给出具体提示。

`dist/release.json` 包含查看器、实时同步程序与五个 Shell 工具，须与 `version.json` 一起提交。安装后程序入口指向 `current/`，升级与回滚通过一次替换符号链接切换整个版本；配置、镜像和录像保存在版本目录之外。旧客户端只更新查看器时，新入口会使用内嵌配套脚本迁移完整程序包。

首次部署此修复建议重新运行 `install.sh`，以更新 OpenWrt 的配置加载方式；仅下载程序无法修改已生成的服务文件。通用 rc.local 安装从旧版升级后应重新启动设备，确保旧实时同步进程退出。测试操作临时目录和本机 HTTP 服务，不操作真实 USB 硬件。

### 多语言

设置 → 🌐 语言 里切换界面语言：中文 / English / Deutsch / Français / Italiano / Español / Nederlands / Русский / 日本語 / 한국어。选择保存在本机浏览器；首次打开会按浏览器语言自动选择。登录页会按浏览器 `Accept-Language` 自动匹配。

### QQ 官方机器人推送

网页 **⚙ → 推送 → QQ 官方机器人**，选择单聊/群聊，填写 **AppID / AppSecret / 接收 OpenID**，保存并发送测试。接收 OpenID 来自该机器人的已验证消息事件，不能填 QQ 号或群号。支持文字、封面和视频；媒体直接通过 QQ 官方接口上传，F50 不需公网域名。完整配置、OpenID 获取和平台权限说明见 [docs/FEATURES.md](docs/FEATURES.md)。

### 飞书推送

哨兵一触发，手机就能收到带触发画面的飞书消息。网页 **⚙ → 🔔 推送** 里配置，两种方式：

**方式 A（推荐）— 自建应用私聊：只有你本人能看到**

消息只进「你和机器人」的单聊，同企业其他成员看不到。

1. [飞书开放平台](https://open.feishu.cn/app) → 创建**企业自建应用**
2. 「添加应用能力」→ 勾 **机器人**
3. 「权限管理」加 `im:message`（发消息）、`im:resource`（上传图片），然后**创建版本并发布**（管理员审核通过）
4. 「凭证与基础信息」里复制 **App ID / App Secret**
5. 网页 **⚙ → 🔔 推送**：接收方式选 **私聊推送**，填 **接收人**（你的 open_id `ou_...`，或飞书登录邮箱）、App ID、App Secret → **保存设置** → **发送测试**

> 拿不到 open_id 时：先在好友里搜到这个机器人，随便发一句话，再看开放平台「事件与回调」的日志；或直接在开放平台「API 调试台」选「获取用户信息」查自己的 open_id。

**方式 B — 群机器人 webhook：群里所有人都能看到**

1. 飞书群 → 右上角设置 → **群机器人** → 添加机器人 → **自定义机器人** → 复制 webhook（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxx`）
2. 接收方式选 **群机器人**，把 webhook 粘进去，勾 **启用推送** → **保存设置** → **发送测试**

两种方式都支持带封面图（需填 App ID / App Secret 上传图片）：

配好后每条推送都会附带**真正触发哨兵的那颗摄像头、触发那一帧**的截图（取不到时回落前视第 1 秒）。

**推送内容：封面卡片 还是 短视频**

私聊模式下可二选一（群机器人只能发卡片）：

| 选项 | 说明 |
|---|---|
| 封面卡片 | 触发那一帧 + 时间/位置/原因/镜头，最快（转码 0 秒） |
| 触发摄像头视频 | 从**触发点前 5 秒**起截一段（默认 30 秒，可调 10–60），先在本机用 ffmpeg 压到 360p / 480p / 720p 再上传，附一条精简卡片方便点回网页看多路 |

实测（F50 / T760）：30 秒 480p 约 2.9MB，转码+上传约 16 秒；飞书视频消息上限 30MB，三个清晰度都远低于上限。

**推送规则**

- 只在**新增**哨兵事件时推（首次启用不补历史，避免一开就刷屏；超过 6 小时的旧事件也不补）
- 事件新旧按**目录名里的时间**判定，不按文件 mtime：车机 U 盘循环覆盖后，定时归档会把旧片段重新抄进 `/archive`，复制出来的文件 mtime 是"抄写时刻"，只看 mtime 会把几天前的录像当成新事件连推
- 目录名时间超过 24 小时的片段只记账、不推送（24 小时余量用于吸收车机与盒子的时区偏差）
- 等片段写完（默认延迟 20 秒）再推，避免封面是半截视频；可调 5–600 秒
- 支持「也推手动保存」（SavedClips）；每条失败最多重试 3 次
- 推送视频自动烧录**行车信息水印**（时间/位置/车速/挡位/油门/刹车，与网页播放器同款）；行车片段带 SEI 遥测即生效，停车哨兵片段无遥测不烧。水印渲染需要中文字体（放 `/opt/tesla-sentry/fonts/`）
- 状态与去重记录在 `$TSF_DIR/notify-state.json`，配置在 `$TSF_DIR/notify.json`

### 访问认证

网页默认带登录保护（`install.sh` 会自动生成随机密码）。凭据在独立文件 `$TSF_DIR/auth.conf`（权限 600）：

```sh
user=admin            # 用户名
pass=xxxxxxxxxx       # 明文密码；也可以改用 sha256=<hash> 免明文
ttl=2592000           # 登录状态保留秒数 (默认 30 天)
secret=               # 可选: 签名密钥, 留空则由密码派生, 改密码即让所有会话失效
```

- 除登录页外**全部路径都要认证**：页面、`/media` 视频流、`/thumb` 缩略图、`/dl` 下载、`/api/*` 接口、`/api/export` 打包
- 登录后写 HMAC 签名 cookie，所以 `<video>` 和下载这些子请求不会反复弹窗；改密码或点右上角 ⏏ 即失效
- 同时支持 HTTP Basic（`curl -u admin:密码`），方便脚本调用
- 网页 **⚙ → 🔑 账号** 里可以改密码和**退出登录**：改完本机自动用新密码重新登录，其它设备上的登录状态立即失效（`auth.conf` 里的 `secret` 会换新）
- 同一 IP 10 分钟内连错 8 次锁 5 分钟
- 删掉 `auth.conf` 并重启即关闭认证（**只有纯内网使用才建议**）
- ⚠️ 认证本身走 HTTP 明文传输，公网穿透时请务必套一层 HTTPS（或用 FRP 的 TLS/stcp），否则密码可被中间人看到


改完重启服务生效：

```sh
/etc/init.d/sentry-viewer restart
/etc/init.d/tesla-gadget restart
```

---

## 目录结构

```
/opt/tesla-sentry/
├── teslacam.img         # 虚拟U盘镜像 (exFAT, 稀疏文件)
├── tesla-gadget.sh      # 虚拟U盘开关脚本: on|off|status
├── tesla-sync.sh        # 归档同步 + 音频写入
├── tesla-img.sh         # 安全写镜像 (自动 detach LUN, 避免主机缓存错乱)
├── usb-selfheal.sh      # LUN 被电脑"弹出"后自动挂回
├── tsf-start.sh         # 通用启动入口 (非 procd/systemd 系统用)
├── cache/               # 缩略图 / 遥测缓存 (仅非 F50 设备, F50 用 /opt/mu300)
└── sentry-viewer.py     # Web 查看器 (纯标准库)

/archive/                # 归档区 (ext4)
├── SentryClips/         # 哨兵告警归档
├── SavedClips/          # 手动保存归档
├── RecentClips/         # 行车记录归档
├── .skins/              # 网页背景
├── boombox-queue/       # 待写入车机U盘的音频
├── boombox-done/        # 已写入的音频
└── files/               # 通用文件中转
```

---

## API

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/events` | GET | 归档事件列表（按分类，含 `event.json` 元数据） |
| `/login` · `/logout` | GET/POST | 登录页（`POST u/p/next`，成功后种 30 天签名 cookie）· 退出登录 |
| `/media/<cat>/<event>/<file>` | GET | 视频流（支持 Range）；追加 `?q=360p/480p/720p` 返回按需转码的低码率副本（缓存于 `TSF_CACHE/res-cache`，单任务串行转码，失败自动回退原文件） |
| `/thumb/<cat>/<event>/<file>` | GET | 缩略图（服务端抽帧 + eMMC 缓存，空文件自动重建） |
| `/api/telemetry?path=` | GET | 解析该片段内嵌 SEI 遥测，返回逐帧 `frames[]` 与最高/平均车速、刹车占比；无 SEI 返回 `{ok:false}`，结果缓存在 eMMC |
| `/api/browse?dir=` | GET | 浏览归档区目录 |
| `/api/usb/attach` | POST | 连接车机U盘（临时停止导出 + 挂载） |
| `/api/usb/detach` | POST | 恢复车机导出 |
| `/api/usb/browse?dir=` | GET | 浏览车机U盘（相对路径） |
| `/api/upload/usb` | POST | 上传到车机U盘（multipart: `file`, `path`） |
| `/api/upload/wrap` | POST | 校验 PNG、创建 Wraps 并写入（`file`, 可选 `overwrite=1`，需 USB 管理模式） |
| `/api/upload/file` | POST | 上传到归档区（multipart: `file`, `path`） |
| `/api/upload/skin` | POST | 上传皮肤 |
| `/api/upload/audio` | POST | 上传并转换锁车音（`file`, 可选 `start=0`, `duration=5`），返回文件名、时长与大小 |
| `/api/audio/setlockchime` | POST | 将历史队列音频实际转换为 LockChime.wav |
| `/api/ota/check?force=1` | GET | 比对 GitHub 仓库 `version.json`，返回 `{current, latest, notes, has_update, source}`（结果缓存 10 分钟，`force=1` 强制刷新） |
| `/api/ota/apply` | POST | 下载最新版 → 校验 → 备份旧版 → 原子替换 → 重启服务 |
| `/api/notify` | GET | 选中渠道的配置、就绪状态与最近结果；不返回应用密钥 |
| `/api/notify/save` | POST | 保存配置；QQ 使用 `provider=qq`, `qq_app_id`, `qq_app_secret`, `qq_target_type=user/group`, `qq_openid`；密钥留空保留 |
| `/api/notify/test` | POST | 向已保存渠道发送测试消息；QQ 无录像时也可测试 |
| `/api/ota/rollback` | POST | 回滚到升级前备份的版本并重启 |
| `/usbdl?path=` · `/dl?path=` | GET | 下载（U盘 / 归档区）；`/dl` 追加 `&q=360p/480p/720p` 返回转码副本 |
| `/api/usb?path=` · `/api/file?path=` | DELETE | 删除（U盘 / 归档区） |

---

## 工作原理（给想改的人）

1. **USB Gadget**：设备的 USB 控制器（UDC）通过 configfs 暴露一个复合设备 = `ncm`（USB 网卡）+ `acm`（串口）+ `mass_storage`（U 盘）。厂商固件通常已建好前两个，本项目只往现有 gadget 里**加**一个 `mass_storage` 功能并重新绑定，所以网卡/串口不会丢。
2. **镜像 vs 归档**：镜像（exFAT）只给车机用；归档区（ext4）给 Linux 用。两者物理隔离，归档区是日志式文件系统，硬断电不丢数据。
3. **归档时机**：以镜像文件的 `mtime` 判断"车是否在写"。空闲超过 `TSF_IDLE` 秒才动手，避免打断行车录制。
4. **短暂断开**：归档全过程 5–15 秒，期间 U 盘从车机视角消失。车机正在写的时候不会触发（mtime 新）。
5. **断电自愈**：开机时 `tesla-gadget` 自启重新导出镜像；`sentry-viewer` 启动时若发现残留挂载会自动卸载并恢复导出。
6. **文件管理互斥**：网页 U 盘管理模式挂载镜像期间，归档任务检测到挂载点被占用会自动跳过，避免抢 loop/UDC。
7. **在线只读同步（实时查看的关键）**：exFAT 同一时间只能被一方挂载，双挂载会互相踩坏元数据。但 gadget 导出期间，镜像文件本身仍可被只读打开 —— `tesla-live-sync.py` 于是**不挂载**，直接按 exFAT 规范解析引导扇区、FAT 表与目录项，沿簇链把车机新写的片段读出来。全程 `pread` 只读，车机完全无感知。
   - **"写完"判定**：exFAT 目录项的 `DataLength` 要文件关闭才落最终值，所以要求「长度 > 0 且连续两轮扫描签名一致」才拷贝，避免搬出半截 mp4（缺 `moov` 头放不了）。
   - **性能**：只读目录项，不扫全盘；64G 镜像每轮扫描毫秒级，新片段通常在 15–30 秒内出现在网页。
   - **兜底**：`tesla-live-sync` 不可用时，`tesla-sync.sh` 会在镜像空闲后照旧挂载归档（此时才会短暂断开 U 盘）。

更多细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)、[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

---

## 常见问题

**Q：车机不认盘 / 提示无法读取？**
先确认 `/sys/class/udc/` 有控制器、`/proc/filesystems` 有 exfat，再跑 `/opt/tesla-sentry/tesla-gadget.sh status`。新盘第一次插车一般会提示格式化，确认即可。

**Q：网页有事件但播放黑屏？**
视频是 **H.265/HEVC**。Windows 用 Edge/Chrome（需系统 HEVC 解码器），iPhone Safari 原生支持；部分安卓浏览器不支持，可自行加 ffmpeg 转码（注意内存）。

**Q：归档不触发？**
看 `/var/log/tesla-sync.log`（或安装目录下日志）。常见原因：镜像一直被写（车没休眠）、`mtime` 解析失败（设备缺 `stat`，脚本已用 `ls --full-time` 兜底）、归档任务未启用。

**Q：U 盘连接模式点了没反应？**
说明停止导出/挂载失败，多半是 loop 设备被占用或镜像不是 exFAT。跑 `losetup -a` 看占用，`umount /tmp/teslamnt` 后再试。

**Q：会不会影响车的哨兵录制？**
归档只在车闲置时进行，每次几秒；U 盘管理模式下会中断（所以有 5 分钟自动恢复）。只要能接受"偶尔几秒 U 盘不可见"，就完全不影响正常使用。

**Q：设备发热/耗电？**
5G 模组 + 视频写入负载较高。建议：锁 4G、加散热片、车内避免暴晒。

---

## 卸载

```sh
sudo ./uninstall.sh              # 停服务、删脚本（保留镜像和归档数据）
sudo ./uninstall.sh --purge      # 连镜像和归档一起删（不可恢复）
```

---

## 安全提示

- 查看器**没有身份认证**，请只在内网/可信网络暴露，公网请自行加反向代理认证（或 VPN）。
- 上传接口限制 100MB，路径被限制在归档区/镜像挂载点内，不可逃逸到系统目录。
- 本项目只操作 USB gadget、镜像和指定目录，不碰系统其他部分。

---

## 免责声明

本项目为个人爱好项目，与特斯拉公司无关。修改车机存储行为存在风险（数据丢失、录制中断等），请自行评估。使用前建议先在小容量镜像上验证。

## 参考与致谢

- [teslausb](https://github.com/marcone/teslausb) —— 树莓派伪装 U 盘归档的开山之作，本项目思路来源
- [SentryBox](https://github.com/weitanism/sentrybox) —— 车机常驻设备的产品化实践
- [tesla-sentry-viewer](https://github.com/denysvitali/tesla-sentry-viewer) —— 网页查看器形态参考

## ☕ 赞助

> 本项目完全开源免费，如果它帮到了你，欢迎请作者喝一杯咖啡~ 

<table>
  <tr>
    <td align="center"><img src="docs/sponsor/alipay.jpg" width="320" alt="支付宝收款码"><br><sub><b>支付宝</b></sub></td>
    <td align="center"><img src="docs/sponsor/wechat.jpg" width="320" alt="微信收款码"><br><sub><b>微信支付</b></sub></td>
  </tr>
</table>

## 💬 交流群

> 遇到问题、想交流玩法，欢迎扫码加入群聊~

<table>
  <tr>
    <td align="center"><img src="docs/sponsor/qq_group.jpg" width="320" alt="QQ群二维码"><br><sub><b>QQ 群：831426822</b></sub></td>
    <td align="center"><img src="docs/sponsor/wechat_group.png" width="320" alt="微信群二维码"><br><sub><b>微信群</b>（二维码 11月12日前有效，过期进群请联系作者或QQ群）</sub></td>
  </tr>
</table>

## License

[MIT](LICENSE)
