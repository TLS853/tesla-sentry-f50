# 兼容性 / 可部署设备清单

> 一句话：**只要设备能在 Linux 下把 USB 口当成"设备"（Gadget）用，就能完整跑；如果只是 USB 主机口，就只能跑网页查看器这一档。**
>
> 判断方法：在候选设备上跑一遍 `sh scripts/tsf-check.sh`，30 秒出结论，比翻参数表可靠。

---

## 1. 三种部署档位

项目不是一个整体，而是三层，可以按设备能力裁剪：

| 档位 | 需要什么 | 能做什么 | 典型设备 |
|---|---|---|---|
| **A. 完整部署** | USB Device(Gadget) + exFAT + root | 虚拟 U 盘（车机以为插了 U 盘）+ 车机无感实时同步 + 网页查看/遥测/导出 | 中兴 F50、随身 WiFi、开发板 |
| **B. 半自动** | 只要 Linux + 存储 | 手动把车机 U 盘的 `TeslaCam/` 拷进归档目录，网页查看/遥测/导出照常 | 家用 NAS、软路由、老笔记本 |
| **C. 仅网页** | 只要 Python 3.7+ | 归档目录已存在（NFS/SMB 挂载也行），只当浏览前端 | 任何机器，包括 x86 NAS |

档位 B/C 不写虚拟 U 盘，只需：

```sh
sudo TSF_ARCHIVE=/mnt/nas/tesla-archive python3 sentry-viewer.py   # 端口 TSF_PORT=8080
```

U 盘管理、锁车音写入这类功能会不可用（没有镜像可挂），其余全部正常。

---

## 2. 硬门槛（缺一条就跑不了档位 A）

| # | 要求 | 为什么 | 自检命令 |
|---|---|---|---|
| 1 | **USB 控制器支持 Device/Peripheral 模式**，且板子上那个口走的是 OTG/device 通道 | 车机是 Host，它要"看到一块 U 盘"，设备就必须扮演 Device。主机口物理上做不到 | `ls /sys/class/udc/` 非空 |
| 2 | 内核 `CONFIG_CONFIGFS_FS` + `CONFIG_USB_CONFIGFS` + `CONFIG_USB_F_MASS_STORAGE` | 通过 configfs 动态挂 mass_storage 功能 | `ls /sys/kernel/config/usb_gadget` 存在 |
| 3 | 驱动 `dwc2` / `dwc3` / `musb` / `ci_udc` 之一已加载并注册 UDC | 上面那条的实际来源 | `ls /sys/class/udc/*/state` |
| 4 | 内核 exFAT（`CONFIG_EXFAT_FS`，Linux 5.7+ 主线） | 车机会把盘格式化成 exFAT，需要读写 | `grep exfat /proc/filesystems` |
| 5 | loop 设备 + `mount` + `losetup` | 镜像文件当块设备挂载（归档、U 盘管理） | `losetup -f` 不报错 |
| 6 | root 权限 | 写 configfs、挂载、改 UDC 都需要 | `id -u` = 0 |
| 7 | 能被车机 USB 口供电（5V，通常 ≤2A） | 设备插在手套箱，无独立供电 | 看功耗，见第 6 节 |

**软门槛**（缺了只是功能降级，不影响主流程）：

| 项 | 缺失影响 |
|---|---|
| Python 3.7+ | 完全跑不起来（查看器与实时同步都是 Python） |
| ffmpeg / ffprobe | 列表缩略图不显示；单路"转码导出"不可用（网页端 canvas 合成不依赖它） |
| cron / systemd timer | 归档不会自动触发，需手动跑 `tesla-sync.sh` |
| 内核 ext4 | 归档区建不了（可换 f2fs/xfs，只影响断电自愈强度） |
| 温度传感器 | 无影响（仅少了状态上报） |

---

## 3. 已验证

| 设备 | 平台 | 系统 | 内核 | 结果 |
|---|---|---|---|---|
| **中兴 F50 存储版** | 紫光展锐 T760 / aarch64 / 8 核 / 1.4G RAM / eMMC 256G | OpenWrt 25.12.5 (armsr/armv8) | 7.2.9，`25100000.dwc3` | ✅ 完整跑通：虚拟 U 盘、实时同步（车机无感）、网页、SEI 遥测、水印导出、缩略图、boombox |

> 这台是项目的参考硬件：无电池、纯 USB 供电、待机 1W 级，插上就可以不管。

图例：✅ 实测 ｜ 🟡 同架构/同内核推断可行（需自检确认）｜ 🔵 社区有同类案例 ｜ ❌ 明确不可行

---

## 4. 设备清单（按类别）

### 4.1 随身 WiFi / 4G-5G CPE（最贴合，体积小、免额外供电）

这类设备天生就有 USB 复合设备能力（要给电脑当 RNDIS 网卡 + 串口），gadget 基础设施齐全。

| 设备类型 | 代表型号 | 判断 |
|---|---|---|
| 展锐 T760/T310 平台随身 WiFi | 中兴 F50 系列、U50、部分"5G 随身 WiFi" | ✅ / 🟡 大概率开箱可用（F50 已实测；同平台改款通常一致） |
| 高通 MDM/ SDX 平台随身 WiFi | 各类 DIY 随身 WiFi、MiFi | 🟡 常见 dwc3，需确认 flash 有余量装 python3 |
| 华为 E8372 / E5573 等老款 | HiSilicon | 🟡 gadget 能力有，但系统封闭、难装 Python 3.7+，不推荐 |
| OpenWrt 4G/5G CPE（带 USB 口） | 各类 MT7621/MT7981 CPE | 🟡 需确认那个 USB 口是 OTG 还是纯 Host；MT7621 常见只有 Host |
| GL.iNet 等便携路由 | MT3000 / AXT1800 等 | 🟡 多为纯 Host USB 口，需自检；不支持则退到档位 B |

**注意**：随身 WiFi 的 USB 口通常同时是"供电 + 数据"，插到车机上正好；但部分型号插上后 USB 会切到"充电模式"而不再枚举 gadget，需要在系统里固定为 peripheral/复合模式。

### 4.2 开发板（可玩性最高，体积/功耗差异大）

| 板子 | USB Device 能力 | 判断 |
|---|---|---|
| Raspberry Pi Zero / Zero 2 W | micro-USB 口原生 USB OTG（dwc2） | 🟡 经典 gadget 板，但**供电与数据同口**，插车机可取电；WiFi 版可同时开热点 |
| Raspberry Pi 3A+ / 3B+ | micro-USB 电源口支持 OTG（需 `dtoverlay=dwc2`） | 🟡 可行，功耗偏高 |
| Raspberry Pi 4B | USB-C 口支持 peripheral（需改 `otg_mode`/DTS） | 🟡 可行，5V3A 需求较高，车机口可能带不动 |
| Raspberry Pi 5 / CM4 / CM5 | 视载板与 DTS 而定 | 🔵 需实测；CM 系列在定制载板上最容易做对 |
| Orange Pi Zero 2/3、Radxa Zero 等 | 全志/瑞芯微 SoC，多为 OTG 口 | 🟡 需确认 DTS 里该口是 `dr_mode = "peripheral"` 或 `"otg"` |
| NanoPi NEO/R2S、Rock Pi 等 | 视型号 | 🟡 R2S 类双网口板常无 OTG，务必自检 |
| 树莓派 CM + 定制底板 | 可自行引出 device 口 | ✅ 工程上最稳，但要画板 |

电源提示：树莓派 4/5 峰值 3–5W，车机 USB 口（尤其哨兵期间）供电能力有限，建议选 Zero 级或带 UPS 的载板。

### 4.3 电视盒子 / 矿渣 / 老手机

| 设备 | 判断 |
|---|---|
| 斐讯 N1、玩客云、各类 Amlogic 机顶盒 | ❌/🟡 绝大多数 USB 口是 Host；少数板子有 OTG 口但要改 DTS 并重新编译内核，性价比低 |
| 旧 Android 手机（已 root） | 🔵 可行：configfs gadget 齐全，插上电脑同时充电+当 U 盘；稳定性/散热/自动开机是主要问题（需去掉电池保护、设成上电自启） |
| 旧 iPhone / iPad | ❌ 系统封闭，无法做 mass_storage gadget |

### 4.4 x86 设备（明确的"不行"与"可以"）

- ❌ **普通 x86 主机 / N100 软路由 / NAS / 笔记本**：USB 口只有 xHCI Host 控制器，**没有 UDC**，任何软件都无法让它变成 U 盘。这是硬件限制，不是驱动问题。
- 少数 Intel 平台有 xDCI / USB Debug Capability，但只能做调试通道，**不能**导出 mass_storage，别在这上面花时间。
- ✅ 但这些设备非常适合 **档位 B/C**：把 `TeslaCam/` 定期拷进 NAS（`rsync`/脚本/把车机 U 盘直接插 NAS），然后跑网页查看器。

### 4.5 其他操作系统

| 系统 | 判断 |
|---|---|
| OpenWrt（21.02+，含 23/24/25） | ✅ 开箱支持（procd 服务由 install.sh 生成） |
| Debian / Ubuntu / Armbian / Raspbian | ✅ 开箱（systemd 单元 + `/etc/cron.d`） |
| Fedora / CentOS Stream / openSUSE | ✅ systemd 分支，注意 SELinux 需放行（`setenforce 0` 或加策略） |
| Arch Linux ARM | ✅ systemd 分支；`python` 默认是 3.x，注意 `ffmpeg` 包名 |
| Alpine / postmarketOS（OpenRC） | 🟡 走 install.sh 的通用 `rc.local` 分支；需 `apk add python3 ffmpeg exfatprogs util-linux` |
| Buildroot / Yocto / 极简 BusyBox 系统 | 🟡 手工部署：python3 + exfat + ffmpeg 自己带，服务用自写 init 脚本调 `tsf-start.sh` |
| FreeBSD / OpenBSD | ❌ 无 Linux configfs/USB gadget 机制（BSD 有自己的 USB templates，但不兼容本项目脚本） |
| Windows / macOS | ❌ 无法做 mass_storage gadget；只能当客户端浏览器访问设备 |
| WSL2 / Docker（在 x86 上） | ❌ 容器内没有 UDC，最多跑档位 C |

---

## 5. 功能降级矩阵

| 设备能力 | 虚拟 U 盘 | 实时同步 | 缩略图 | SEI 遥测 | 水印导出 | 多路合成 |
|---|---|---|---|---|---|---|
| 完整（F50 级） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅（浏览器端） |
| 无 ffmpeg | ✅ | ✅ | ❌ 占位符 | ✅ | ✅ | ✅ |
| 单核 512M | ✅ | ✅ 稍慢 | ✅ 慢（首次 ~1s/张） | ✅ 0.5–1s/段 | ✅ | ✅ 靠浏览器 |
| 无 cron | ✅ | ✅ | ✅ | ✅ | ✅ | ✅（归档需手动） |
| 无内核 exFAT（用 FUSE exfat） | 🟡 车机端正常，本机挂载慢 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 仅档位 C | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |

CPU 实测参考（F50，8 核 A76/A55 混合）：SEI 遥测 40 秒片段首次 0.4s、缓存命中 <10ms；缩略图 0.2–0.4s/张；实时同步一轮约 1–3s（扫描镜像目录 + 增量拷贝）。

---

## 6. 供电、散热与寿命（决定能不能长期插在车上）

| 项 | 建议 |
|---|---|
| 供电 | 车机手套箱 USB 口通常 5V/2.1A，但**哨兵期间整夜供电**；设备待机功耗越低越好（F50 约 1W，树莓派 4 约 3–5W 偏高） |
| 无电池优先 | 有电池的设备在车内高温下有鼓包风险，且夏天车内可达 60℃+ |
| 温度 | 优先金属外壳、无风扇或大散热片；持续写入 eMMC 会发热 |
| 写入量 | 哨兵持续录制 20–40MB/s，一天 50–100GB 级别；eMMC/TF 卡注意寿命，长期建议工业级或外接 SSD |
| 断电 | 车机会在哨兵关闭/低压保护时直接断电，所以归档区要日志式文件系统（ext4），本项目已按此设计 |

---

## 7. 常见"以为可以，其实不行"

1. **USB 双公头线把两个主机口连起来** —— 两边都是 Host，永远不会枚举，还可能烧口。
2. **加 USB Hub 转 OTG** —— Hub 不会把 Host 变成 Device。
3. **在 x86 上用 `g_mass_storage` 模块** —— 模块存在但找不到 UDC，`modprobe` 成功、`echo udc > UDC` 必然失败。
4. **Android 手机不 root 就想当 U 盘** —— 系统不给 configfs 写权限。
5. **用 exFAT 之外的文件系统做镜像** —— 车机只认 exFAT/FAT32，且大于 32G 必须 exFAT。
6. **镜像容量选太大** —— 稀疏文件平时只按实际写入占空间，但**车机首次使用会格式化整个虚拟 U 盘**，之后镜像就按标称容量实占。170G 镜像放在 200G 出头的存储上，再加归档区，磁盘写满后车机会报"U 盘读不出/需要格式化"。默认 64G（可滚 7 小时以上）适合绝大多数设备；空间紧张可以用 `--size 32`，存储充裕想留长回看可手动加大（如 `--size 128`），但请保证 镜像容量 + 20G < 空闲空间。

---

## 8. 可配置项（换设备时最常改的）

全部通过环境变量注入，`/etc/tesla-sentry.conf` 由 `install.sh` 生成，服务单元以 `EnvironmentFile` 传入；也可直接 `TSF_XXX=... python3 sentry-viewer.py` 临时指定。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TSF_ARCHIVE` | `/archive` | 归档区路径（ext4 目录）。档位 B/C 部署时指到已有目录即可 |
| `TSF_PORT` | `8080` | 网页端口 |
| `TSF_IMG` | `/root/teslacam.img` 或 `$TSF_DIR/teslacam.img` | 虚拟 U 盘镜像 |
| `TSF_MNT` | `/tmp/teslamnt` | 镜像临时挂载点 |
| `TSF_LOOP` | `/dev/loop3` | 归档用的 loop 设备 |
| `TSF_LOSETUP` | 有 `/opt/mu300/busybox-bin/losetup` 就用它，否则走 PATH | 精简系统可指定 busybox 版 |
| `TSF_GADGET` | `/sys/kernel/config/usb_gadget/linux` | 现有 gadget 目录名各设备不同，自检脚本不会改它，装错时手工指定 |
| `TSF_CACHE` | 有 `/opt/mu300` 就用它，否则 `$TSF_DIR/cache` | 缩略图/遥测/合成临时缓存 |
| `TSF_FFMPEG` / `TSF_FFPROBE` | 同上策略，否则走 PATH | 缩略图与转码 |
| `TSF_MEDIA_CONN` | `8` | 视频流并发上限（弱设备调小） |
| `TSF_TEL_CONN` | `1` | 遥测解码并发（单核设备保持 1） |
| `TSF_IDLE` | `600` | 镜像空闲多少秒后才归档 |

> `/archive/...` 这种虚拟前缀在内部会自动映射到 `TSF_ARCHIVE`，所以网页深链、导出、缩略图在自定义归档区下依然可用。

## 9. 快速自检

```sh
# 拷到候选设备上执行（建议 root）
sh scripts/tsf-check.sh          # 人类可读，带结论
sh scripts/tsf-check.sh --json   # 机器可读，便于批量筛设备
```

输出示例（F50 实测）：

```
1. 平台
  ✓ 操作系统           Linux 7.2.9 aarch64
  ✓ 权限               root
2. USB Device / Gadget (硬门槛)
  ✓ configfs gadget    已挂载
  ✓ UDC 控制器         25100000.dwc3 (state=configured)
3. 文件系统与存储
  ✓ exFAT (内核)       支持
4. 运行时依赖
  ✓ python3            3.13
  ✓ init 系统          OpenWrt procd (install.sh 直接支持)

结论: 完全满足, 直接 sudo ./install.sh
```

如果第 2 节出现 ✗（没有 UDC），别急着放弃：

1. 先把数据线插到电脑上再跑一次——部分设备只有插线后才注册 UDC
2. 查 DTS：`grep -r "dr_mode" /sys/firmware/devicetree/base/` ，`otg`/`peripheral` 才有戏，`host` 就是纯主机口
3. 确认内核模块：`ls /lib/modules/$(uname -r)/ | grep -E "dwc2|dwc3|musb"`，有模块但没加载就 `modprobe`
4. 都不行 → 这台只能跑档位 B/C
