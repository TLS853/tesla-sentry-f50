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

## 合成导出报「设备上未安装 ffmpeg」

多路合成用的是静态版 ffmpeg，默认路径 `/opt/mu300/ffmpeg/ffmpeg`（含 `ffprobe`）。

- 用**静态链接**的 aarch64 构建（如 ffbinaries 或 johnvansickle 的 static 版），OpenWrt 是 musl，glibc 动态版跑不起来
- 安装：`mkdir -p /opt/mu300/ffmpeg && cp ffmpeg /opt/mu300/ffmpeg/ && chmod +x /opt/mu300/ffmpeg/*`
- 路径不同可用环境变量 `TSF_FFMPEG` / `TSF_FFPROBE` 覆盖（init 脚本里传给 Python）

## 网页上有视频，但 U 盘里看不到 / 可用空间不变

网页读的是归档区 `/archive`，U 盘是 `teslacam.img` 镜像区，归档不会回写镜像——两个区域本来就是分开的。

- 先确认镜像正常：`cat /sys/kernel/config/usb_gadget/*/functions/mass_storage.0/lun.0/file`
- 需要把归档内容放回 U 盘（演示或恢复）：用 `sh tesla-img.sh exec '<命令>'`，不要直接往导出的镜像里写
- 电脑端按 F5 刷新；仍看不到就弹出后重新插拔

## 播放黑屏 / 只有声音

先确认片段的实际编码（`ffprobe -show_entries stream=codec_name`）：

- **H.264（多数 Model 3/Y）**：主流浏览器原生支持，黑屏通常不是编码问题，看下面「播放黑屏但编码正常」
- **H.265/HEVC（部分新车型/固件）**：Windows 的 Edge/Chrome 需安装「HEVC 视频扩展」（微软商店）；iPhone/iPad Safari 原生支持；多数 Android 浏览器不支持
- 也可以直接从「📂 文件」里下载视频到本地用播放器看

## 播放黑屏但编码正常（H.264）

1. 该片段可能还没同步完整（车机正在写）→ 等十几秒刷新
2. 服务端 Range 请求异常 → `curl -I -H 'Range: bytes=0-100' http://设备/media/<cat>/<ev>/<file>.mp4` 应返回 `206`
3. 浏览器拦截混合内容 → 页面与视频必须同源（都走设备 IP）

## 列表缩略图显示 🎞 占位符

服务端抽帧失败留下的空文件被当成缓存。已在代码中修复（写 `.tmp.jpg` 成功且非空才落盘、命中 0 字节自动删除重建），若仍有：

```sh
python3 -c "import glob,os;[os.remove(p) for p in glob.glob('/opt/mu300/thumbs/*') if os.path.getsize(p)==0]"
```

顺手确认 `ffmpeg` 路径（`TSF_FFMPEG`）正确，且该片段文件大小非 0。

## 遥测面板（车速/油门/刹车）不显示

1. **停车片段本来就没有**：SEI 只在行驶中录制时写入，哨兵片段大多没有 → 属正常
2. 判断某片段有没有：`ffmpeg -loglevel trace -i 片段.mp4 -c copy -bsf:v trace_headers -frames:v 5 -f null - 2>&1 | grep -o 'nal_unit_type = [0-9]*' | sort | uniq -c`，出现 `6` 即有 SEI
3. 有 SEI 但接口返回 `{ok:false}` → 删掉 `/opt/mu300/tel-cache` 对应缓存后重试

## 手机上展开信息卡后被视频/面板遮挡

旧版 iOS Safari 在 CSS Grid 的 `1fr` 行里计算 `aspect-ratio` 会塌陷，导致视频层溢出绘制。当前版本已改为 JS 显式计算像素行高，并把遥测面板移出 `overflow:hidden` 容器。若仍出现，请清 Safari 缓存后强刷（或换 Chrome 验证），并反馈 iOS 版本。

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

## 网页一打开设备就卡死/重启

F50 的 OpenWrt 整个系统跑在一个 loop 镜像上，且内核开了 `softlockup_panic=1 hardlockup_panic=1 panic=5`：
重负载会把某个 CPU 卡死在内核态，5 秒后自动重启，症状是"所有服务失联但还能 ping 通"。

已排查出两类触发源，均已修复：

1. **旧版列表页给每个事件挂 `<video>` 预览**。事件一多（实测 237 个行车记录事件、上千路画面），
   浏览器会同时向设备发起上千个视频请求，设备直接被拖死。
   现在列表页使用服务端 ffmpeg 抽帧的静态缩略图（`/thumb/...`，eMMC 缓存 + 并发限流 2），
   视频流接口也有全局并发上限（默认 8，`TSF_MEDIA_CONN` 可调）。
2. **设备端 ffmpeg 多路 1080p 合成**。实测 2 路、4 路真实素材都会在合成中途触发内核重启。
   现在多路合成默认改为**浏览器本机合成**（canvas + MediaRecorder 实时录制，设备只出文件流），
   设备端拼接接口默认停用，如确需使用设 `TSF_STITCH=1`（风险自负）。

## 列表里一个事件有几十个片段

Tesla 一个事件目录里包含事件前后约 10 分钟的片段（每分钟一组、每组 4-6 路）。
播放器会按片段时间自动分组，默认只加载/播放最新一组，信息卡里可以切换其它片段；
导出（合成/zip）也只针对当前片段组。
