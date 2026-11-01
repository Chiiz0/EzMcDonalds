# 账号接入

首次接入通过官方 App 登录及用户明确授权的本地采样取得会话和设备上下文。已有本人官方设备上下文时，也可在本地 CLI 通过真实短信验证登录已注册会员账号；输入一个手机号或展示标签本身不能取得账号权限。

## 环境要求

- 官方 macOS App（iOS App on Mac），已验证版本 7.0.440 / build 202610201841。
- 二进制默认 `/Applications/McdApp.app/WrappedBundle/McdApp`；CLI 可用 `--binary` 指定。
- SHA-256 必须匹配 `745da50c09c44a62eb222f72fda2571b9fa3acec55a2a17a4e5fdee5b7a85d85`。签名材料在用户机器上从该文件读取，仓库不分发官方二进制或提取值。其他构建会停止，需要独立适配。
- 本地 HAR/JSON 采样文件须为普通文件、权限 0600、大小不超过 4 MiB；复制请求文本上限128 KiB。只保留必要请求后再导出。

## 从复制的单条请求接入

可以在本人本地调试工具中复制余额请求为 POSIX cURL 或原始 HTTP 请求，Mac 用户运行 `ezmcd account connect personal --clipboard`，或使用权限0600文本文件的 `--request-file`。不必整理 HAR 或响应正文；项目验证请求签名并重新查询余额，成功才保存账号。复制的命令不会执行。完整操作及运行字段更新见[复制请求导入](request-import.md)。

## 从 HAR 接入

1. 在官方 App 登录自己的账号。验证码、密码及其他验证留在官方入口。
2. 用自己控制的本地网络调试工具，在明确授权范围内导出 HAR。查询麦钱包余额时的 `GET https://api.mcd.cn/bff/member/mwallet/balance` 请求和成功响应须包含在内。HAR 需要保留请求头和响应正文。
3. 结束采样后恢复原代理、移除临时证书及私钥。若显式选择[持久采样证书](sampling-certificate.md)，每轮复用，停用时撤销。账号导入和 MCP 不会自动安装证书、修改系统代理或解除客户端校验；证书安装需要单独运行本机 `sampling-cert install` 并完成系统授权。
4. 在自己的终端导入，不把 HAR 或会话字段发到聊天、Issue 或仓库。

```sh
chmod 600 /absolute/path/account.har
ezmcd account connect personal --har /absolute/path/account.har
ezmcd account check personal
```

命令只从官方域名、精确路径提取必要记录，拒绝混合多个登录会话的余额样本，校验余额请求签名并重新查询余额；成功后才保存账号。导入不会重放 HAR 中的订单或其他请求。

若 HAR 包含同一会话已授权订单的 `POST /bff/order/orders` 及 `x-mcd-sign`，导入会校验并提取 v5 运行字段，不保存订单正文。缺失则可完成只读接入。当前运行字段最大有效期为 24 小时；过期需新的官方证据，不能修改时间戳延长。**不要为了取得运行字段额外创建订单。** 无可用证据时，当前版本不能开启纯脚本下单。运行字段可用 `account runtime` 单独维护；相同官方设备字段还支持显式的只读验证复用，见[凭据维护](credentials.md#单独更新下单运行字段)。

## 从单请求样本或已有 profile 接入

已有账号更新运行字段时，Mac 可使用[无订单采样命令](guarded-capture.md)生成同会话余额与运行样本，再使用原别名及 `--update` 导入。采样期间不会转发订单。

```sh
ezmcd account connect personal \
  --balance-sample /absolute/path/balance.json \
  --runtime-sample /absolute/path/same-session-runtime.json

# 兼容此前已验证的 profile
ezmcd account import personal \
  --profile /absolute/path/session-validated.json \
  --runtime-sample /absolute/path/same-session-runtime.json
```

余额样本格式：`request` 包含 `method`、`host`、`path`、`headers` 和空字符串 `body`；`response` 包含 `status` 和字符串或对象 `body`。请求头保留官方原始签名和会话字段。运行字段样本只需相同格式的 `request`，必须是同一登录与设备的官方 v5 下单请求。项目校验原签名，不使用保存的旧 nonce 或旧签名发新请求。

`account import` 适合已有开发者 profile；仍须执行 `account check`。新用户优先使用会校验采样并在线检查的 `account connect`。

## 更新、标签与撤销

已有本人设备上下文后可执行 `ezmcd account login NEW_ALIAS --device-from EXISTING_ALIAS`。这条流程已真实验证短信发送、验证码登录、号码绑定、钱包只读检查和新账号保存；仍依赖官方 SDK 设备字段，不能从空白环境独立初始化。验证码不传入 MCP，详情见[短信登录](sms-login.md)。

```sh
# 同一账号重新登录，保留策略、身份和交易日志
ezmcd account connect personal --har /absolute/path/new-account.har --update

# 只关闭消费，仍可查询
ezmcd account policy personal --disable-checkout

# 删除本地账号凭据，保留身份和交易日志
ezmcd account revoke personal
```

`--update` 不允许把已有别名改成另一个会员。不同会员使用不同别名；同一会员不能用两个别名绕过预算。撤销后重新接入需 `--update`，消费保持关闭。

`--phone-label` 是导入流程的可选展示标签，只保存脱敏形式，不发送短信、不验证号码归属。短信登录是独立的 `account login` 命令。已有刷新令牌时可通过 `account refresh` 按需维护会话，详见[会话维护](session-maintenance.md)；未提供后台自动刷新或官方远程注销功能。撤销本地凭据不会删除原始导入文件；用户自行管理其保留期限。

## 本地数据

默认 `~/.local/share/ezmcdonalds`，可通过 `--data-dir` 或 `EZMCD_DATA_DIR` 指定。目录 0700，账号文件、数据库 0600。当前是本地文件保护，没有应用层加密；同一操作系统用户下的程序仍可能读取。

凭据与支付日志应一起备份。未知订单或付款结果通过官方 App 核对，不能删除日志或切换数据目录后重试。日常无人值守使用还需解决运行字段续期和账号生命周期，参见[路线](roadmap.md)。
