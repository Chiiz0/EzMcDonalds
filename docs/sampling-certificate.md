# 复用本机采样证书

需要多次从本人官方 App 取得凭据时，可以在 macOS 安装一次项目专用采样证书，后续使用同一份证书和私钥。证书安装与代理启停分开：结束每次采样仍恢复原代理，但不再反复安装和移除证书。

它减少的是 macOS 证书授权弹窗，不延长麦当劳登录或下单运行字段的有效期，也不授予消费权限。证书管理命令不提供给 MCP，不自动启用代理，不抓取流量，不下单。

## 准备与安装

```sh
ezmcd sampling-cert prepare
ezmcd sampling-cert install
ezmcd sampling-cert status
```

如果账号使用了自定义数据目录，给各条命令使用相同的 `--data-dir /absolute/path/data`，参数放在 `sampling-cert` 前。

`prepare` 仅在本机生成唯一的 RSA 私钥和证书；重复执行不会替换已有材料。`install` 首次需要本人在 macOS 系统窗口授权。证书名以 **EzMcDonalds Local Sampling** 开头，默认有效期5年；到期后显式撤销并重新准备。没有自动或远程续签。

信任只加入当前用户的登录钥匙串，限定 SSL 服务端主机 `api.mcd.cn`。CA 还包含 DNS 名称约束和 IP 地址排除约束。不会加入管理员或系统级全局信任，也不使用忽略主机名或过期错误的设置。Apple 的 [SSL 信任策略字符串](https://developer.apple.com/documentation/security/ksectrustsettingspolicystring)指定的是主机名。

`status` 和安装后的检查会生成临时测试证书，让 macOS 离线验证：

- `api.mcd.cn` 应通过；
- 无关域名和 `child.api.mcd.cn` 应失败。

检查不联网，也不会把 CA 作为强制信任根传给验证器。已有受限信任通过检查后，再执行 `install` 会返回 `reused: true`，不重新要求系统授权。信任被用户撤销、文件缺失或证书过期时仍需处理，不能保证任何系统状态下都免弹窗。

## 在调试工具中复用

证书位于命令返回的 `ca_directory`，默认在项目数据目录下的 `sampling-ca`。目录权限0700；证书、私钥和元数据0600。私钥未另外加密，同一 macOS 用户下的程序仍可能读取，应按账号凭据保护它。不要上传到聊天、Issue、云端服务或公开仓库；每个用户独立生成，项目不分发共用私钥。

该目录包含 mitmproxy 需要的 `mitmproxy-ca.pem` 与 `mitmproxy-ca-cert.pem`。将本人调试工具的 `confdir` 指向这个目录，并把 HTTPS 查看范围限制到 `api.mcd.cn:443`。格式和加载方式见 [mitmproxy 自定义 CA 文档](https://docs.mitmproxy.org/stable/concepts/certificates/#using-a-custom-certificate-authority)。其他工具的 CA 导入方式需独立确认。

证书本身不会阻止订单，也不会完成采样。已有账号可使用[无订单采样命令](guarded-capture.md)，采样核心已完成官方 App 实机验证；也可按[凭据指引](credentials.md)设置发送前断点。不能仅凭安装证书就认为请求已被拦截。取得请求后先终止请求并关闭 App，再恢复代理。

项目内的采样实现可在 `SamplingCertificate.lease()` 内使用此目录：它检查实际信任，并在采样期间阻止并发撤销。外部调试工具需自行先停止采样，CLI 不会检测或关闭它们。

## 撤销与删除私钥

```sh
ezmcd sampling-cert remove
```

先退出使用此证书的采样工具并恢复代理。命令按证书指纹移除当前用户信任和钥匙串证书，确认不再受信任后删除自己的证书、私钥及元数据。macOS 撤销时可能需要一次系统授权。失败会保留本地材料供重试，不会谎报已清理；未知文件或符号链接不会被递归删除。

本机已实测安装和免重复授权复用，以及限定主机的正反向系统信任检查。撤销路径由隔离测试覆盖；本轮按用户要求保留实际证书，没有为验收将其撤销再装。
