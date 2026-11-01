# 复制一条请求即可导入

适用于自己已登录的官方 Mac App。项目接收 **POSIX cURL 文本**或**原始 HTTP 请求文本**，只做解析、签名校验和实时余额查询。复制的 cURL 不会被执行，不需要把请求或凭据发给个人 Agent。

## 连接账号

1. 按[凭据指南](credentials.md#从自己的官方-app-导入)配置本人本地调试工具，打开官方 App 的麦钱包。
2. 找到成功的 `GET https://api.mcd.cn/bff/member/mwallet/balance`，复制这一条请求为 cURL 或原始 HTTP 请求。不要只复制 URL、响应或多个请求。具体菜单取决于调试工具；格式可参考 [mitmproxy 官方导出实现](https://github.com/mitmproxy/mitmproxy/blob/main/mitmproxy/addons/export.py)。
3. 在自己的 Mac 终端运行：

```sh
ezmcd account connect personal --clipboard
```

项目读取当时的文本剪贴板，验证官方地址、接口和签名，以新签名查询当前余额，成功才保存必要会话字段。新账号消费默认关闭。不会保存复制的原文、原签名或 Cookie，也不会安装证书或修改代理。

4. 清理剪贴板中的请求及剪贴板管理器历史，结束采样，恢复原代理并移除临时证书和私钥。项目不自动清空剪贴板，以免覆盖其他应用刚复制的内容。失败时也应清理。
5. 运行 `ezmcd account check personal`，再将 `ezmcd mcp-config` 的配置交给个人 Agent。

更新已经连接的同一会员时使用原别名及 `--update`；消费策略和购买日志保留。只有登录会话和设备完全相同时才保留已有刷新令牌及运行字段，不能给新登录凭空补上这些信息。

```sh
ezmcd account connect personal --clipboard --update
```

不使用剪贴板时，可在本机编辑器保存单条请求为文本文件，再运行：

```sh
chmod 600 /absolute/path/request.txt
ezmcd account connect personal --request-file /absolute/path/request.txt
```

不要把复制的命令直接粘到 shell 执行，也不要用含凭据的 `echo` 命令保存文件。项目不支持 PowerShell 导出、`@文件` 引用、额外命令、代理/地址覆盖参数或不明确的重复请求头；遇到这些格式可改用原始请求或原有 `--har` 入口。只接收官方默认 HTTPS 端口和目标接口。

## 更新下单运行字段

先按[发送前断点步骤](credentials.md#在发送订单前的断点取得请求头)取得**已阻断发送**的本人 `POST /bff/order/orders` 请求，或使用自己此前已授权操作留下的样本。确认请求被 Abort 并关闭官方 App 后，可将该条请求复制为 cURL/原始请求，运行：

```sh
ezmcd account runtime personal --clipboard
# 或
ezmcd account runtime personal --request-file /absolute/path/runtime-request.txt
```

项目丢弃订单正文，核对请求头中的账号、设备、v5 签名及真实采样时间，只查询余额后更新运行字段。不会发送订单、刷新原采样时间或增加消费授权。请求可在断点中复制，但必须先 Abort；若工具不能导出暂停请求，仍可使用原有 `--paste-headers` 逐项输入方式。

## 验证范围

已用现有授权账号重建一条签名余额请求，在临时数据目录通过实际 CLI 文件入口导入并查询真实余额；原账号、偏好和购买日志未改变，临时目录已移除。解析、签名拒绝、隐私、账号更新与运行字段更新有离线测试。**这不是新的官方 App 复制/断点操作验收**；剪贴板入口用替代输入测试，尚未读取用户真实剪贴板，也没有据此创建订单或付款。
