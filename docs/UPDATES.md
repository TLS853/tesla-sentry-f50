# 离线上传 ZIP 更新

从 **2.10.2** 起，在哨兵云盘的「设置 → 版本更新」选择保存在手机或电脑上的 ZIP，由浏览器直接上传给 F50。打开更新页只读取本机版本及回滚状态。

## 从旧版切换一次

运行 **2.9.0 至 2.10.1** 的设备仍显示 URL 输入框。可以用旧版的 ZIP 直链方式安装一次 2.10.2，再刷新页面。也可以通过 [Mac 离线部署](DEPLOYMENT.md) 安装，适合 F50 无法访问下载地址或运行 2.8.x 及更早版本的情况。

## 生成更新 ZIP

在持有源码的电脑上递增版本并构建，例如：

```sh
python3 tools/make_release.py 2.10.3 "这次更新的说明"
python3 tools/build.py --check
python3 tools/package_update.py
```

版本已构建时，只需运行最后一条。默认生成 `dist/tesla-sentry-f50-版本-update.zip` 和同名 `.sha256` 校验文件。可加 `--output` 指定输出路径。GitHub 源码 ZIP 和完整安装用的 `*-offline.tar.gz` 不适用于更新页。

## 在 F50 网页更新

1. 退出 U 盘管理模式，打开 **设置 → 版本更新**。
2. 选择更新 ZIP。可选填 `.sha256` 文件开头的 64 位校验值。
3. 点击 **上传 ZIP 并更新**，保持页面打开。
4. 校验文件清单、哈希及版本并通过自检后，设备切换完整版本并重启服务。

只接受高于当前运行版本的包。上传不完整、哈希不匹配、自检失败或 USB 忙时不会切换版本。USB 忙时可等同步结束后重试。

## 文件与校验边界

ZIP 压缩大小最多 **16 MiB**，解压总大小最多 **32 MiB**，单个运行文件最多 **8 MiB**。清单固定在根目录，包含：

```text
manifest.json
sentry-viewer.py
tesla-live-sync.py
tesla-gadget.sh
tesla-sync.sh
usb-selfheal.sh
tesla-img.sh
tsf-check.sh
```

清单记录格式、版本、日期、说明及各文件的大小和 SHA-256。更新器只读取这些固定文件，不按归档路径直接解压；拒绝缺失、额外、重复、加密及符号链接条目。

外层 ZIP SHA-256 为可选核对项，内部逐文件校验始终执行。哈希校验核对内容一致性，不能代替对代码发布者的信任，应使用可信发布者提供的更新文件。

更新覆盖上述运行程序及其内嵌网页资源。录像、虚拟盘、登录凭据和 QQ / 飞书配置在版本目录外保留。Python、ffmpeg、字体、系统服务定义和安装器不在运行更新 ZIP 内；这些依赖或服务定义需要调整时，仍通过完整离线安装流程处理。

## 回滚与 API

网页的「回滚到上一版本」按钮仅在上一完整版本可用时出现。回滚也需 USB 空闲，完成后会重启服务。

- `GET /api/ota/status` 和旧 `GET /api/ota/check` 读取本机状态。
- `POST /api/ota/apply` 接收 ZIP 原始字节，使用 `Content-Type: application/zip` 和 `Content-Length`。可选的外层校验值使用 `X-Update-SHA256` 请求头。
- `POST /api/ota/rollback` 回滚上一完整版本。

以上接口沿用网页认证。2.10.2 起不再接受 URL 下载请求。
