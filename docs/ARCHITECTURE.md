# 架构与实现原理

## 1. 整体数据流

```
                  ┌─────────────────────────────────────────────┐
  特斯拉车机        │            小 Linux 设备                     │
┌──────────┐      │                                             │
│ Sentry   │ USB  │  ┌──────────────────────────────────────┐   │
│ Clips    │◄────►│  │ USB Gadget (configfs)                │   │
│ 写入      │      │  │  ├─ ncm.usb0   ← USB 虚拟网卡         │   │
└──────────┘      │  │  ├─ acm.GS0    ← 串口控制台           │   │
                  │  │  └─ mass_storage.0 ← 虚拟U盘          │   │
                  │  └───────────────┬──────────────────────┘   │
                  │                  │ lun.0/file = $TSF_IMG     │
                  │                  ▼                          │
                  │      teslacam.img (exFAT, 稀疏, 默认170G)    │
                  │                  │                          │
                  │                  │ 车闲置(镜像 mtime > IDLE) │
                  │                  ▼                          │
                  │      tesla-sync.sh:                       │
                  │        unbind UDC → losetup → mount →     │
                  │        拷贝新事件 → 写 Boombox/ → umount → │
                  │        detach → rebind                    │
                  │                  │                          │
                  │                  ▼                          │
                  │      /archive (ext4, 日志式) ──► sentry-viewer.py :8080
                  └─────────────────────────────────────────────┘
```

**关键设计**：镜像（exFAT）与归档区（ext4）是两块**物理隔离**的存储。车机永远只碰镜像，网页永远只碰归档区，两者不会同时读写同一个 exFAT 文件系统。

## 2. 为什么能"伪装 U 盘"

Linux 内核的 USB Gadget 框架允许设备扮演 USB 外设。通过 configfs（`/sys/kernel/config/usb_gadget/`）可以动态拼装一个复合设备：

| 功能 | configfs 路径 | 作用 |
|---|---|---|
| NCM | `functions/ncm.usb0` | USB 虚拟网卡（SSH/网页访问） |
| ACM | `functions/acm.GS0` | 串口控制台（救援用） |
| Mass Storage | `functions/mass_storage.0` | 虚拟 U 盘（本项目追加） |

`mass_storage` 的 `lun.0/file` 指向一个**普通文件**，主机就会把它当作块设备。所以"170G U 盘"其实是设备上的一个稀疏文件。

> 注意：修改 gadget 必须先 `echo "" > UDC` 解绑，**解绑期间所有 USB 功能（网卡、串口、U 盘）会同时消失**。因此所有涉及解绑的操作必须脱离 SSH 会话执行（`setsid`），否则命令会被自己掐断。

## 3. 归档算法

```
每分钟 cron 检查（实际每 10 分钟）:
  1. 若 $TSF_MNT 已被挂载（网页 U 盘管理模式）→ 直接退出，不抢锁
  2. 读镜像 mtime（设备可能没有 stat，用 ls --full-time + date -d 兜底）
  3. mtime 距今 < IDLE(600s) → 车还在写，退出
  4. 解绑 UDC（U盘从车机消失）
  5. losetup 显式绑定 loop → mount -t exfat (读写)
  6. 遍历 TeslaCam/{SentryClips,SavedClips,RecentClips}/*/
     目标目录已存在 → 跳过；否则整目录 cp -r 到 /archive/<cat>/<事件名>/
  7. 把 boombox-queue/ 里的音频拷到 <镜像>/Boombox/，成功后移到 boombox-done/
  8. umount → losetup -d → 重新绑定 UDC（U盘回来）
```

**为什么要"短暂断开"**：exFAT 不是集群文件系统，同一时刻只能有一端挂载。Linux 侧要读镜像，就必须让车机先放手。

**为什么只在空闲时做**：行车/哨兵录制期间镜像 mtime 持续更新，永远不会满足"空闲 600 秒"，所以不会打断录制。停车熄火后约 10 分钟开始归档，全程 5–15 秒。

## 4. 断电与数据安全

车内 USB 口在车辆休眠时会**硬断电**，没有优雅关机。

| 风险 | 缓解 |
|---|---|
| 正在写镜像时断电 → exFAT 损坏 | 镜像只由车机写（车机的写入是它自己的事）；Linux 侧只在归档窗口内挂载，且挂载前先解绑 UDC，避免双写 |
| 归档拷贝中断 → 半个事件目录 | 先拷到目标目录，失败则 `rm -rf` 回滚；下次重跑 |
| 归档区断电损坏 | 归档区用 ext4（日志式），掉电后 fsck 自愈 |
| 镜像彻底损坏 | 删掉镜像重建即可（车机会重新格式化，历史事件已在归档区） |

## 5. 网页 U 盘管理模式（状态机）

```
         POST /api/usb/attach                POST /api/usb/detach
IDLE ───────────────────────► MANAGE ─────────────────────────► IDLE
  ▲      unbind UDC              │         umount + detach
  │      losetup + mount rw      │         + rebind UDC
  │                              │
  └──────────────────────────────┘
        5 分钟无操作自动 detach (threading.Timer)
```

- 管理模式期间，cron 归档任务检测到挂载点占用会自动跳过
- 查看器进程启动时会自愈：若发现残留挂载，先 umount、detach loop、重新绑定 UDC
- 所有写操作后立即 `sync`，再恢复导出，避免缓存丢失

## 6. 安全边界

| 入口 | 限制 |
|---|---|
| `/api/browse`、`/dl`、`/api/file` | 路径 realpath 必须以 `/archive` 开头，防 `../` 逃逸 |
| `/api/usb/browse`、`/usbdl`、`/api/usb` | 路径 realpath 必须落在镜像挂载点内 |
| 上传 | 单文件 ≤ 100MB；文件名仅允许 `[\w\-. ]`，自动去除目录部分 |
| 音频 | ≤ 1MB（车机限制）；锁车音必须 WAV |

**没有内置身份认证**。请只在内网使用，公网暴露请自行加反向代理认证或走 VPN。

## 7. 播放器同步策略

六路视频各自独立解码，以第一路为基准，每 250ms 校正一次：任何一路与基准偏差 > 0.35s 就 `currentTime` 对齐。这比"同时 play()"更稳，能消化不同编码器的启动延迟差异。

## 8. 已知限制

- 视频为 H.265/HEVC，浏览器需支持（Edge/Chrome 需系统 HEVC 扩展，Safari 原生支持）
- 单线程 HTTP 服务器（`ThreadingHTTPServer`），并发播放 6 路以上会吃 CPU
- 归档以"事件目录"为单位，未做增量文件级同步
- USB 3.0 总带宽与 eMMC 写入速度决定录制稳定性；实测 eMMC 写 80–130MB/s，哨兵写入峰值仅 20–40MB/s
