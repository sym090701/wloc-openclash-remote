# WLOC OpenClash Remote

把 WLOC MITM 服务运行在局域网 Docker 服务器上，由 OpenWrt/OpenClash 旁路由负责分流。局域网设备不需要运行 Shadowrocket，也不需要设置 Wi-Fi HTTP 代理。

## 架构

```text
iPhone / LAN client
        |
        v
OpenWrt + OpenClash
        |  gs-loc.apple.com / gs-loc-cn.apple.com
        v
Docker server:18081 (mitmproxy + WLOC addon)
        |
        v
Apple WLOC service
```

服务器和软路由自身会被排除，避免 MITM 回环。其他设备按 OpenClash 原有规则运行。

## 部署

1. 复制环境模板：

```sh
cp .env.example .env
cp config.env.example config.env
```

2. 把示例网段、路由器地址、Docker 服务器地址和初始坐标改成自己的值。`config.env` 不要提交到 Git。

3. 启动服务器：

```sh
mkdir -p data/{config,providers,mitmproxy}
docker compose pull
docker compose run --rm --no-deps --entrypoint python wloc-mitm \
  /opt/wloc/addon/wlocctl.py auth-reset --password-stdin
docker compose up -d
```

密码通过标准输入提供，长度为 10–128 个字符。管理页面默认监听 `18082`，MITM 监听 `18081`。

4. 在 OpenClash 自定义覆盖脚本中调用 `router/openclash-hook.sh`。设置：

```sh
WLOC_REMOTE_SERVER=192.168.50.2
WLOC_ROUTER_IP=192.168.50.1
```

然后重启 OpenClash。钩子会添加一个 HTTP 代理节点和两个精确域名规则，并保留服务器与路由器的直连排除。

5. 在 iPhone 或 iPad 的 Safari 中登录管理页，点击“下载 CA 证书”获取 DER 格式的 `.cer`。在“设置 → 已下载描述文件”中安装，再打开“通用 → 关于本机 → 证书信任设置”，启用该证书的完全信任。

6. 打开 `http://<docker-server>:18082` 选择位置。设备的默认网关必须是运行 OpenClash 的旁路由。

## 运行限制

这是针对 Apple WLOC 网络定位响应的实验性研究工具，不是 GPS 芯片模拟器。它不能改变公网出口 IP、时区或账户地区，也不能绕过证书固定。只在你拥有或获授权管理的设备和网络上使用，并遵守服务条款和当地法律。

WebUI 只应开放给可信 LAN；不要把 `18081`、`18082` 或 mitmproxy CA 私钥暴露到 WAN。真实设备是否成功还取决于 iOS 版本、证书信任状态和 Apple 定位协议变化。
