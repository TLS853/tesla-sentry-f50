# F50 离线部署

F50 不需要访问 GitHub。Mac 持有完整安装包后，通过 SSH 把程序传过去；设备端只读取包内文件。离线模式不下载字体，也不发起在线版本检查。

## 从这台 Mac 上传

在项目根目录执行，替换实际设备 IP：

```sh
sh tools/deploy_f50.sh root@设备IP
```

脚本生成经过 SHA-256 核对的离线包，通过 SSH 标准输入上传，在设备上使用 `setsid` 后台安装。输入设备的 SSH 密码即可；不需要把密码写进脚本。上传完成只表示后台安装已启动，最终结果请查看它打印的 `INSTALL_LOG`。

安装默认采用 64GiB 虚拟盘和 8080 网页端口。需要使用其它持久化存储时，显式传参：

```sh
sh tools/deploy_f50.sh root@设备IP --dir /大容量分区/tesla-sentry --archive /大容量分区/archive --size 64 --port 8080
```

非默认 SSH 端口通过 `SSH_PORT=2222 sh tools/deploy_f50.sh ...` 指定。上传使用 SSH 数据流，兼容没有 SFTP 服务的固件。脚本不会刷机、格式化磁盘或修改分区。新建镜像的空间不足时会停止安装，不会只打印警告后继续创建。

## 手动上传安装包

如果不使用 Mac 上传脚本，可以先运行 `python3 tools/package_release.py` 生成 `dist/tesla-sentry-f50-版本-offline.tar.gz`，再通过 SSH 或文件传输工具上传到设备的 `/tmp`。解压后在对应目录运行：

```sh
setsid sh install-offline.sh > /tmp/tesla-install.log 2>&1 < /dev/null &
tail -n 80 /tmp/tesla-install.log
```

程序通常位于 `/opt/tesla-sentry`，归档位于 `/archive`；这两个路径必须位于大容量持久化存储。安装器会复用已经导出或配置的镜像，已有镜像不会因 `--size` 参数被自动缩放或格式化。

已有旧版时，先停止车机录制、备份重要录像与配置，并退出网页里的 USB 管理模式。OpenWrt 上在升级前停止旧查看器和同步服务：

```sh
/etc/init.d/sentry-viewer stop
/etc/init.d/tesla-live stop
```

如果当前镜像仍被挂载或其它进程正在占用，安装会拒绝切换版本；先恢复 USB 导出或等待同步完成后重试。不要用卸载或格式化来解决程序安装问题。

## 安装后使用

浏览器打开 `http://设备IP:8080`。首次安装会生成 `admin` 账号和随机密码，显示在安装日志中；升级会保留原账号。也可以在设备本机查看安装目录内的 `auth.conf`，不要把该文件或含密码的日志公开发送。

```sh
python3 /opt/tesla-sentry/sentry-viewer.py --ota-check
sh /opt/tesla-sentry/tesla-gadget.sh status
```

首次创建的空镜像需要接入车机后由车机初始化；已有录像的镜像不需要重新格式化。录制完成并归档后，可以在网页查看视频、筛选事件和导出。

## 没有网络时哪些功能可用

本地录制、归档、网页回放、下载和浏览器合成不依赖 GitHub。离线包包含本项目程序，不包含设备固件、Python、内核模块、ffmpeg 或中文字体。缺少 Python 或 USB/exFAT 支持时必须先补齐；缺少 ffmpeg 会影响缩略图和设备端转码，缺少字体会影响中文烧录水印。

飞书推送需要设备能连接飞书；远程访问需要设备自身的网络或已有的内网穿透。它们与能否连接 GitHub 是不同的网络条件。

## 后续更新

再次在 Mac 上取得新版本、生成离线包并上传安装即可。离线安装会设置 `TSF_OTA_ENABLED=0`，避免网页后台反复请求 GitHub。不要把未知代理网站作为程序更新源。

如需使用 Mac 提供局域网更新源，可以在 Mac 项目根目录运行 `python3 -m http.server 8765 --bind 0.0.0.0`，并在 F50 配置文件中设置：

```ini
TSF_OTA_ENABLED=1
TSF_OTA_SOURCES=http://Mac的局域网IP:8765
```

重启查看器后即可从 Mac 拉取版本清单和完整程序包；设备仍不需要访问 GitHub。这个临时文件服务器会暴露项目目录中的文件，只在可信局域网按需开启，用完按 Ctrl+C 关闭。若直接从 GitHub 更新，默认分支为 `codex/modular-runtime`，不会从保留的旧 `main` 安装程序。
