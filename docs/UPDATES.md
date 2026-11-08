# 手动 ZIP 更新与 R2 发布

从 **2.9.0** 起，程序不自动检测更新，不查询 GitHub、版本清单或任何默认远端源。打开网页不会产生更新请求；打开「设置 → 更新」仅查询本机运行版本及回滚状态。只有点击「下载 ZIP 并更新」，设备才请求输入的 URL。

## 从旧版切换一次

2.8.x 及更早版本不支持本格式。先通过 [Mac 离线部署](DEPLOYMENT.md) 安装 2.9.0 或更新版本，并刷新网页，使新更新页生效。之后即可用输入 URL 的方式升级到更高版本。

在 Mac 的项目目录执行，替换设备 IP；已有设备先按部署文档退出管理模式并停止旧服务：

```sh
sh tools/deploy_f50.sh root@设备IP
```

旧配置里的 `TSF_OTA_ENABLED`、`TSF_OTA_REPO`、`TSF_OTA_REF`、`TSF_OTA_SOURCES` 已不参与更新。即使保留 `TSF_OTA_ENABLED=1`，也不会启用自动检测；设为 `0` 同样不会阻止手动 URL 更新。

## 生成更新 ZIP

在持有源码的 Mac 或开发电脑上操作，不需要 F50 能访问 GitHub。发新版时先修改源代码并递增版本号，例如：

```sh
python3 tools/make_release.py 2.9.1 "这次更新的说明"
python3 tools/build.py --check
python3 tools/package_update.py
```

版本已正确构建时，只需运行最后一条。默认生成：

```text
dist/tesla-sentry-f50-2.9.1-update.zip
dist/tesla-sentry-f50-2.9.1-update.zip.sha256
```

文件名随版本号变化。可用 `python3 tools/package_update.py --output /目标路径/update.zip` 指定输出位置，旁边会同步生成 `.sha256`。在源码、版本说明和构建产物一致时，生成结果可复现。Python 3.7+ 标准库即可打包，无需 npm 或额外 Python 包。

不要使用 GitHub 的「Download ZIP」，也不要把整个仓库压缩后上传：更新器只接受专用格式。`*-offline.tar.gz` 用于完整安装，不能填入 ZIP 更新框。

## 上传 R2

1. 在 Cloudflare 控制台选择 R2 bucket，上传 `*-update.zip`。可同时保存 `.sha256` 供人工核对，但 F50 不会自动请求这个伴随文件。
2. 使用 F50 可直接下载该对象的 URL：公开 bucket 的可访问地址、配置好的自定义域名，或私有 bucket 的 **GET 预签名 URL**。控制台管理页面、需要交互登录的分享页不是下载直链。
3. 每个版本使用独立的对象名，例如 `releases/tesla-sentry-f50-2.9.1-update.zip`，避免覆盖同名对象后缓存仍返回旧内容。

公开 URL 示例：

```text
https://updates.example.com/releases/tesla-sentry-f50-2.9.1-update.zip
```

私有 bucket 可生成有时效的 GET 预签名链接，把包括查询参数在内的**完整链接**粘贴到更新框。无需向 F50 配置 R2 Access Key / Secret。下载由 F50 服务端发起，浏览器只向本机更新 API 提交地址，不直接跨域请求 R2。签名 URL 不保存到本项目配置、更新历史或浏览器存储，成功后清空输入框；服务器和网络服务本身仍可能记录访问日志。

R2 具体操作见 [上传对象](https://developers.cloudflare.com/r2/objects/upload-objects/) 和 [预签名 URL](https://developers.cloudflare.com/r2/api/s3/presigned-urls/) 官方文档。

## 在 F50 网页更新

1. 停止录制并退出 U 盘管理模式，打开 **设置 → 更新**。
2. 粘贴 ZIP 下载直链。可选填 `.sha256` 文件中第一段的 64 位十六进制校验值，不要复制后面的文件名。
3. 点击 **下载 ZIP 并更新**，保持页面打开。设备只下载指定链接，支持 HTTPS、局域网 HTTP 和正常 HTTP 重定向；HTTPS 不允许降级跳转到 HTTP。
4. 下载后校验 ZIP 文件清单、每个文件的 SHA-256、大小及版本，执行 Python 入口自检和 Shell 语法检查。
5. 全部通过后，暂存的新版本目录成为 `current`，重启查看器和在线同步，页面等待版本变化后刷新。

只接受**高于当前运行版本**的包；同版本重装和直接降级会被拒绝。需要退回上一版时使用「回滚到上一版本」。更新失败会显示原因，下载不完整、哈希不匹配、自检失败或 USB 忙时不切换当前版本；退出管理模式或等待同步结束后可重试。

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

外层 ZIP SHA-256 为可选核对项，内部逐文件校验始终执行。哈希校验核对内容一致性，不能代替对代码发布者的信任，应使用自己控制的更新文件和下载地址。

更新覆盖上述运行程序及其内嵌网页资源。录像、虚拟盘、登录凭据和 QQ / 飞书配置在版本目录外保留。Python、ffmpeg、字体、系统服务定义和安装器不在运行更新 ZIP 内；这些依赖或服务定义需要调整时，仍通过完整离线安装流程处理。

## 局域网与回滚

不使用 R2 时，可只把生成的 ZIP 与校验文件放进单独目录，在 Mac 提供临时下载：

```sh
python3 -m http.server 8765 --bind Mac的局域网IP --directory /存放更新ZIP的目录
```

然后在网页填 `http://Mac的局域网IP:8765/实际文件名.zip`。不需要修改 F50 配置或重启服务来设置更新源。

成功升级会保留上一完整版本位置。网页的回滚按钮根据 `previous-release.json` 及完整文件是否存在显示；回滚也需 USB 不被占用，并通过切换版本目录、重启服务完成。

API：`GET /api/ota/status` 只读本机状态；旧 `GET /api/ota/check` 为本地兼容接口，即使带 `force=1` 也不请求网络。`POST /api/ota/apply` 接收 JSON `{"url":"https://…/update.zip","sha256":"可选64位值"}`；`POST /api/ota/rollback` 回滚上一版。全部接口沿用网页认证。
