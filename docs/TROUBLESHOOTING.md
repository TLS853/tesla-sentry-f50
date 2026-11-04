# 故障排查

先跑这几条诊断命令，多数问题能直接定位：

```sh
# 1. gadget 与 U 盘状态
sh /opt/tesla-sentry/tesla-gadget.sh status

# 2. USB 控制器状态 (configured = 正常)
cat /sys/class/udc/*/state

# 3. 内核能力
grep -w exfat /proc/filesystems
zcat /proc/config.gz 2>/dev/null | grep MASS_STORAGE

# 4. 服务与日志
logread | tail -30
tail -30 /opt/tesla-sentry/tesla-sync.log
tail -20 /opt/tesla-sentry/tesla-gadget.log

# 5. loop 占用
losetup -a
cat /proc/mounts | grep teslamnt
```

---

## 车机不认盘 / 提示"USB 设备无法读取"

1. `tesla-gadget.sh status` 看 `lun` 是否指向镜像、`state` 是否 `configured`
2. 新盘第一次插入，车机通常提示"**格式化 USB 设备**"，点确认即可
3. 若一直提示错误：镜像可能不是 exFAT（`mount -t exfat` 报错），删镜像重建
4. 换一根**数据线**（很多线只供电不传数据）

## 设备插上后电脑/车机完全看不到任何 USB 设备

绑定失败导致 gadget 掉线。configfs 改动不持久，**拔插一次电源**即可恢复默认。之后检查 `tesla-gadget.log` 里的 `BIND FAILED` 记录。

## 归档不触发

日志里会有原因：

| 日志 | 含义 | 处理 |
|---|---|---|
| `busy (last write Ns ago), skip` | 车还在写 | 正常，等停车熄火 |
| `SKIP-MANAGED` | 网页处于 U 盘管理模式 | 点"恢复车机导出"，或等 5 分钟 |
| `WARN: cannot parse mtime` | 设备 `date` 不支持解析 `ls` 时间格式 | 手工执行 `date -d "2026-01-01 12:00:00" +%s` 验证；否则改用 `TSF_IDLE=0` 强制模式 |
| `ERROR: losetup failed` | loop 设备被占 | `losetup -a` 找占用，`losetup -d /dev/loopX` 释放 |
| `ERROR: mount image failed` | 镜像不是 exFAT 或已损坏 | `blkid` 查类型；必要时重建镜像 |

cron 是否在跑：`ps | grep cron`、`cat /etc/crontabs/root`。

## 网页能打开但没有任何事件

- 归档还没跑（默认要等镜像空闲 10 分钟）→ 手工跑一次：`sh /opt/tesla-sentry/tesla-sync.sh`
- 车根本没在写 → 确认车机设置里哨兵模式开启、U 盘被识别为 TeslaCam 盘
- 归档目录为空 → `ls -R /archive | head`

## 播放黑屏 / 只有声音

H.265/HEVC 解码问题：

- Windows：Edge/Chrome 需安装「HEVC 视频扩展」（微软商店）
- iPhone/iPad：Safari 原生支持
- Android：多数浏览器不支持，可装 VLC 或用支持 HEVC 的浏览器
- 也可以直接从「📂 文件」里下载视频到本地用播放器看

## 网页 U 盘管理模式点"连接"失败

```
losetup 失败 / 挂载失败
```

1. `losetup -a` 看 `/dev/loop3` 是否被归档任务占用 → 等归档结束或改 `TSF_LOOP`
2. `grep teslamnt /proc/mounts` 有残留 → `umount /tmp/teslamnt`
3. 镜像损坏 → `fsck.exfat -n $TSF_IMG` 检查（有工具的话）

## 上传锁车音后车机没变

1. 车机端要手动选：**Toybox → Boombox → Lock Sound → USB**
2. 文件必须：WAV / 44.1kHz / 16-bit / < 1MB / 1–5 秒
3. 归档任务还没跑 → 手工执行 `tesla-sync.sh`，然后看 `<镜像>/Boombox/LockChime.wav` 是否存在

## 服务起不来

```sh
# OpenWrt
/etc/init.d/sentry-viewer restart
logread | grep sentry

# systemd
systemctl status sentry-viewer
journalctl -u sentry-viewer -n 50
```

常见原因：`python3` 路径不对（改 `/etc/init.d/sentry-viewer` 里的 command）、端口被占（`netstat -ltn | grep 8080`）。

## 设备发热严重

- 锁 4G（关闭 5G）降低基带功耗
- 加散热片/导热垫，避免车内暴晒
- 减少并发播放路数（同时看六路最吃 CPU）
